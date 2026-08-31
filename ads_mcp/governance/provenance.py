"""Proves which revision of this code an MCP instance is executing.

The problem this exists to solve, observed live on 2026-08-26/27: the MCP
was launched with `uv run --directory <developer working tree>`, so each
instance imported whatever happened to be checked out at the moment it
spawned. Five concurrent sessions were found running four different
revisions, one of them predating the governance work entirely, and a
session could just as easily have bound to an unreviewed feature branch.
Nothing in the running process could report which revision it was.

THREE INDEPENDENT IDENTITIES, compared rather than trusted:

  PIN     ADS_MCP_PINNED_REVISION -- what the operator intends to run.
  STAMP   RUNTIME_REVISION -- what the deployment was cut from.
  HEAD    git rev-parse HEAD -- what is actually checked out right now.

An earlier revision of this module trusted the stamp and only compared it
to the pin. That accepted two states it should have refused: a plain
developer checkout whose HEAD happened to equal the pin (no deployment at
all), and a stamped deployment someone had since `git checkout`-ed to a
different clean commit -- which reported the stamped revision while
executing a different one. All three identities must agree.

Governed service additionally requires the deployment FORM to be intact:
detached HEAD (so no branch can move it), no modified tracked files, and
no unexpected untracked files that could shadow or extend what Python
imports. Anything unknown -- git missing, a command failing -- is a
refusal, never a pass. A control that cannot see is not a control.

Read-only use may stay unpinned so local development is unaffected. The
governed tier may not: a proposal is only as trustworthy as the code that
framed it.
"""

import os
import pathlib
import re
import subprocess
import time

PINNED_REVISION_VAR = "ADS_MCP_PINNED_REVISION"
STAMP_FILENAME = "RUNTIME_REVISION"

# Untracked paths a deployment is allowed to contain. The stamp is written
# by the deploy script after checkout, so it is expected. Nothing else is:
# an untracked .py inside the package can shadow or extend what is
# imported, which is precisely the class of drift this module exists to
# make impossible. Ignored paths (.venv, __pycache__) never appear here --
# `git status --porcelain` omits them.
ALLOWED_UNTRACKED = frozenset({STAMP_FILENAME})

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_UNKNOWN = "unknown"
_DETACHED = "HEAD"


def is_commit_sha(value: object) -> bool:
  """True only for a full 40-character lowercase hex commit id.

  Abbreviations and refs are rejected on purpose: two identities can only
  be compared for equality if both are the same canonical form.
  """
  return isinstance(value, str) and bool(_SHA_PATTERN.match(value.strip()))


def runtime_root() -> pathlib.Path:
  """Directory containing the `ads_mcp` package that is actually loaded."""
  return pathlib.Path(__file__).resolve().parent.parent.parent


def stamp_path() -> pathlib.Path:
  """Location of the deployment stamp, if one was written."""
  return runtime_root() / STAMP_FILENAME


def read_stamp() -> str | None:
  """Returns the revision recorded when this deployment was cut."""
  try:
    value = stamp_path().read_text(encoding="utf-8").strip()
  except OSError:
    return None
  return value or None


def _git(root: pathlib.Path, *args: str) -> str | None:
  """Runs a read-only git command in `root`, or returns None on failure."""
  try:
    result = subprocess.run(
        ("git", *args),
        cwd=str(root),
        capture_output=True,
        # NEVER inherit stdin. Under the stdio transport the server's stdin
        # is the JSON-RPC pipe; a child that inherits it blocks. Measured
        # 2026-08-31: without this, each call sat until the 10s timeout
        # (~30s per request, git HEAD unresolved); with it, 0.58s.
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return None
  if result.returncode != 0:
    return None
  return result.stdout.strip()


def _git_revision(root: pathlib.Path) -> str | None:
  """The commit actually checked out, independent of any stamp."""
  return _git(root, "rev-parse", "HEAD") or None


def _git_detached(root: pathlib.Path) -> bool | None:
  """True when HEAD follows no branch. None when it cannot be determined.

  `rev-parse --abbrev-ref HEAD` prints the literal "HEAD" when detached
  and the branch name otherwise, and exits zero either way -- so a None
  here means git genuinely failed, not that we are detached.
  """
  ref = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
  if ref is None:
    return None
  return ref == _DETACHED


def _git_status(root: pathlib.Path) -> tuple[bool | None, list[str] | None]:
  """Returns (tracked_files_modified, unexpected_untracked_paths).

  One `git status --porcelain` call answers both questions. Untracked
  entries are the `??` lines; everything else is a tracked modification.
  Ignored paths are not listed at all.
  """
  status = _git(root, "status", "--porcelain")
  if status is None:
    return None, None

  tracked_dirty = False
  untracked: list[str] = []
  for line in status.splitlines():
    if not line.strip():
      continue
    if line.startswith("??"):
      path = line[2:].strip().strip('"')
      if path not in ALLOWED_UNTRACKED:
        untracked.append(path)
    else:
      tracked_dirty = True
  return tracked_dirty, untracked


def runtime_provenance() -> dict[str, object]:
  """Reports all three identities and the deployment form, without judging.

  Nothing here is inferred from anything else: the stamp is not allowed to
  stand in for HEAD, and HEAD is not allowed to stand in for a deployment.
  Values that cannot be determined are None, which is the finding.
  """
  root = runtime_root()
  stamp_revision = read_stamp()
  git_head_revision = _git_revision(root)
  tracked_dirty, unexpected_untracked = _git_status(root)
  detached_head = _git_detached(root)

  pinned = os.getenv(PINNED_REVISION_VAR)
  pinned_revision = pinned.strip() if pinned and pinned.strip() else None

  if stamp_revision:
    revision, revision_source = stamp_revision, "deployment_stamp"
  elif git_head_revision:
    revision, revision_source = git_head_revision, "git_working_tree"
  else:
    revision, revision_source = None, _UNKNOWN

  # Immutable means: cut by a deployment, still on exactly that commit,
  # still detached, and nothing added or edited underneath. Every unknown
  # counts against it.
  immutable = (
      is_commit_sha(stamp_revision)
      and is_commit_sha(git_head_revision)
      and stamp_revision == git_head_revision
      and tracked_dirty is False
      and detached_head is True
      and unexpected_untracked == []
  )

  return {
      "revision": revision,
      "revision_source": revision_source,
      "runtime_root": str(root),
      "pinned_revision": pinned_revision,
      "stamp_revision": stamp_revision,
      "git_head_revision": git_head_revision,
      "pin_configured": bool(pinned_revision),
      "working_tree_dirty": tracked_dirty,
      "unexpected_untracked": unexpected_untracked,
      "detached_head": detached_head,
      "matches_pin": (
          is_commit_sha(pinned_revision)
          and pinned_revision == stamp_revision == git_head_revision
      ),
      "immutable": immutable,
  }


# The verified state this PROCESS started with. Populated once, by the
# entrypoint, at the same moment the gate runs -- before the stdio
# transport owns stdin.
#
# Why a snapshot rather than re-reading on demand: the running process
# imported its code at startup and cannot un-import it. What a session
# needs to know is what THIS process loaded, which a later re-read cannot
# tell it any better and can only report less reliably. Re-reading also
# put a subprocess on the request path, which is what broke.
_STARTUP_SNAPSHOT: dict[str, object] | None = None


class NoStartupSnapshotError(RuntimeError):
  """Raised when provenance is requested before startup recorded it."""


class UnpinnedRuntimeError(RuntimeError):
  """Raised when a governed runtime cannot prove its revision."""


def _refuse(message: str) -> None:
  raise UnpinnedRuntimeError(
      f"Refusing to serve the governed tier: {message}"
  )


def verify_pinned_runtime(*, require_pin: bool) -> dict[str, object]:
  """Fail-closed startup check.

  Args:
      require_pin: True when the governed tier is enabled. A governed
          runtime MUST be an immutable deployment whose pin, stamp and
          HEAD all agree; read-only use need not be.

  Returns:
      The provenance dict when the runtime is acceptable.

  Raises:
      UnpinnedRuntimeError: on any condition that leaves the executing
          revision unproven. Every branch below refuses rather than
          serving from a runtime it cannot vouch for.
  """
  state = runtime_provenance()
  if not require_pin:
    return state

  pin = state["pinned_revision"]
  stamp = state["stamp_revision"]
  head = state["git_head_revision"]

  # --- 1. All three identities must exist and be canonical. -------------
  if not pin:
    _refuse(
        f"{PINNED_REVISION_VAR} is not set. Launch from a deployment "
        "checkout cut by ads_mcp/scripts/deploy_runtime.py and pin the "
        "revision it prints. Read-only use does not require a pin."
    )
  if not is_commit_sha(pin):
    _refuse(
        f"{PINNED_REVISION_VAR}={pin!r} is not a 40-character commit sha. "
        "Abbreviations and branch names are rejected so the pin, the "
        "stamp and HEAD can be compared exactly."
    )
  if not stamp:
    _refuse(
        "no deployment stamp is present, so this is a working tree rather "
        "than a deployment. A checkout can be moved by an ordinary git "
        "command, which is exactly the drift this refuses. Deploy with "
        "ads_mcp/scripts/deploy_runtime.py."
    )
  if not is_commit_sha(stamp):
    _refuse(
        f"the deployment stamp {stamp!r} is not a 40-character commit "
        "sha, so it cannot be trusted to identify anything. Re-deploy."
    )
  if not head:
    _refuse(
        "the checked-out commit could not be read, so what is actually "
        "executing cannot be established. Refusing rather than trusting "
        "the stamp on its own."
    )
  if not is_commit_sha(head):
    _refuse(f"the checked-out commit {head!r} is not a commit sha.")

  # --- 2. They must agree. The stamp is never trusted alone. ------------
  if stamp != head:
    _refuse(
        f"the deployment is stamped {stamp} but is actually on {head}. "
        "The checkout was moved after it was cut, so the runtime would "
        "report one revision while executing another. Re-deploy."
    )
  if pin != stamp:
    _refuse(
        f"runtime revision {stamp} does not match the pinned {pin}. "
        "Something moved the deployment underneath the launch "
        "configuration. Re-deploy the intended revision rather than "
        "overriding this check."
    )

  # --- 3. The deployment form must still be intact. ---------------------
  if state["working_tree_dirty"] is None:
    _refuse(
        "whether tracked files have been modified could not be "
        "determined, so the executing code cannot be shown to match "
        f"{head}. An unverifiable runtime is refused, not assumed clean."
    )
  if state["working_tree_dirty"]:
    _refuse(
        f"tracked files under {state['runtime_root']} differ from {head}, "
        "so the code being imported is not the reviewed code. A "
        "deployment checkout must never be edited in place."
    )
  if state["unexpected_untracked"] is None:
    _refuse(
        "untracked files could not be enumerated, so it cannot be shown "
        "that nothing was added underneath the deployment."
    )
  if state["unexpected_untracked"]:
    listed = ", ".join(sorted(state["unexpected_untracked"])[:5])
    _refuse(
        f"unexpected untracked files are present under "
        f"{state['runtime_root']}: {listed}. Files added after the "
        "deployment was cut can shadow or extend what is imported, so "
        "they are refused rather than ignored."
    )
  if state["detached_head"] is None:
    _refuse(
        "whether the deployment is on a detached HEAD could not be "
        "determined. Refusing rather than assuming it cannot be moved."
    )
  if not state["detached_head"]:
    _refuse(
        "the deployment is on a branch rather than a detached HEAD, so an "
        "ordinary commit or fast-forward elsewhere could move the code "
        "underneath a running configuration. Re-deploy with "
        "ads_mcp/scripts/deploy_runtime.py, which detaches on purpose."
    )

  # --- 4. Belt and braces: the summary flags must agree with all of it. -
  if state["revision_source"] != "deployment_stamp" or not state["immutable"]:
    _refuse(
        "the runtime did not resolve to a verified immutable deployment "
        f"(source={state['revision_source']}, "
        f"immutable={state['immutable']})."
    )

  return state


def record_startup_snapshot(state: dict[str, object]) -> dict[str, object]:
  """Freezes the verified startup state, tagged to this process.

  Called by the entrypoint immediately after the gate passes. The pid and
  the module load time make it obvious if a snapshot were ever somehow
  read from a different process than the one answering.
  """
  global _STARTUP_SNAPSHOT
  snapshot = dict(state)
  snapshot["process_id"] = os.getpid()
  snapshot["verified_at_startup"] = True
  snapshot["snapshot_monotonic"] = time.monotonic()
  _STARTUP_SNAPSHOT = snapshot
  return snapshot


def startup_snapshot() -> dict[str, object]:
  """The verified state recorded at startup.

  Raises:
      NoStartupSnapshotError: when startup never recorded one. That means
          the process did not come up through a verified entrypoint, so
          there is nothing trustworthy to report -- and inventing an
          answer by re-reading now would be exactly the wrong move.
  """
  if _STARTUP_SNAPSHOT is None:
    raise NoStartupSnapshotError(
        "No startup-verified provenance is available in this process. The "
        "server did not record one at launch, so what it is executing "
        "cannot be attested. Restart through ads_mcp.stdio or "
        "ads_mcp.server."
    )
  snapshot = dict(_STARTUP_SNAPSHOT)
  snapshot["reported_by_process_id"] = os.getpid()
  snapshot["process_identity_matches"] = (
      snapshot["process_id"] == os.getpid()
  )
  return snapshot


def describe_provenance(state: dict[str, object]) -> str:
  """One-line startup summary for stderr. stdout is the MCP transport."""
  revision = state["revision"] or _UNKNOWN
  short = revision[:12] if revision != _UNKNOWN else _UNKNOWN
  posture = "PINNED" if state["matches_pin"] else "UNPINNED"
  if not state["immutable"]:
    posture += " MUTABLE"
  if state["working_tree_dirty"]:
    posture += " DIRTY"
  return (
      f"[ads-mcp runtime] revision={short} "
      f"source={state['revision_source']} {posture} "
      f"root={state['runtime_root']}"
  )
