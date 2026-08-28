"""Proves which revision of this code an MCP instance is executing.

The problem this exists to solve, observed live on 2026-08-26/27: the MCP
was launched with `uv run --directory <developer working tree>`, so each
instance imported whatever happened to be checked out at the moment it
spawned. Five concurrent sessions were found running four different
revisions, one of them predating the governance work entirely, and a
session could just as easily have bound to an unreviewed feature branch.
Nothing in the running process could report which revision it was.

Two mechanisms, both fail-closed:

  STAMP     A deployment writes RUNTIME_REVISION next to the package. It
            records the exact commit the deployment was cut from. This is
            authoritative, because a deployment tree is never edited.

  PIN       ADS_MCP_PINNED_REVISION states the commit the operator
            intends to be running. Startup refuses if the runtime cannot
            prove it matches.

The governed tier requires a pinned, verified runtime. That is the whole
point: a proposal is only as trustworthy as the code that framed it, so
propose/approve/apply must not run from a tree someone can `git checkout`
underneath. Read-only use stays available unpinned for local development.
"""

import os
import pathlib
import subprocess

PINNED_REVISION_VAR = "ADS_MCP_PINNED_REVISION"
STAMP_FILENAME = "RUNTIME_REVISION"

_UNKNOWN = "unknown"


def runtime_root() -> pathlib.Path:
  """Directory containing the `ads_mcp` package that is actually loaded."""
  return pathlib.Path(__file__).resolve().parent.parent.parent


def stamp_path() -> pathlib.Path:
  """Location of the deployment stamp, if one was written."""
  return runtime_root() / STAMP_FILENAME


def read_stamp() -> str | None:
  """Returns the deployed revision recorded at deploy time, if any."""
  path = stamp_path()
  try:
    value = path.read_text(encoding="utf-8").strip()
  except OSError:
    return None
  return value or None


def _git(root: pathlib.Path, *args: str) -> str | None:
  """Runs a read-only git command in `root`, or returns None."""
  try:
    result = subprocess.run(
        ("git", *args),
        cwd=str(root),
        capture_output=True,
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
  return _git(root, "rev-parse", "HEAD") or None


def _git_tree_dirty(root: pathlib.Path) -> bool | None:
  """True when TRACKED files differ from HEAD.

  Untracked files are ignored on purpose: the deployment stamp itself is
  untracked, and an untracked scratch file does not change what Python
  imports. A modified tracked file does.
  """
  status = _git(root, "status", "--porcelain", "--untracked-files=no")
  if status is None:
    return None
  return bool(status.strip())


def runtime_provenance() -> dict[str, object]:
  """Describes the revision this process is executing, and how it knows.

  Returns:
      A dict safe to surface to an operator. `revision` is None when the
      runtime cannot prove what it is running, which is itself the
      finding -- it is never guessed.
  """
  root = runtime_root()
  stamped = read_stamp()
  if stamped:
    revision, source = stamped, "deployment_stamp"
  else:
    git_revision = _git_revision(root)
    revision = git_revision
    source = "git_working_tree" if git_revision else _UNKNOWN

  dirty = _git_tree_dirty(root)
  pinned = os.getenv(PINNED_REVISION_VAR)
  pinned = pinned.strip() if pinned else None

  return {
      "revision": revision,
      "revision_source": source,
      "runtime_root": str(root),
      "pinned_revision": pinned,
      "pin_configured": bool(pinned),
      "working_tree_dirty": dirty,
      "matches_pin": bool(pinned) and revision == pinned,
      # A runtime is only immutable if it was cut by a deployment AND no
      # tracked file has been edited underneath it since.
      "immutable": source == "deployment_stamp" and dirty is not True,
  }


class UnpinnedRuntimeError(RuntimeError):
  """Raised when a governed runtime cannot prove its revision."""


def verify_pinned_runtime(*, require_pin: bool) -> dict[str, object]:
  """Fail-closed startup check.

  Args:
      require_pin: True when the governed tier is enabled. A governed
          runtime MUST be pinned and verified; read-only use need not be.

  Returns:
      The provenance dict when the runtime is acceptable.

  Raises:
      UnpinnedRuntimeError: when a governed runtime cannot prove it is
          running the revision the operator pinned. Every failure mode
          below refuses to start rather than serving from an unknown
          revision.
  """
  provenance = runtime_provenance()

  if not require_pin:
    return provenance

  pinned = provenance["pinned_revision"]
  if not pinned:
    raise UnpinnedRuntimeError(
        "Refusing to start the governed tier from an unpinned runtime. "
        f"Set {PINNED_REVISION_VAR} to the exact commit this deployment "
        "should be running, and launch from a deployment checkout rather "
        "than a developer working tree. Read-only use does not require a "
        "pin. See ads_mcp/scripts/deploy_runtime.py."
    )

  revision = provenance["revision"]
  if not revision:
    raise UnpinnedRuntimeError(
        "Refusing to start the governed tier: this runtime cannot prove "
        f"which revision it is executing, but {PINNED_REVISION_VAR} "
        f"expects {pinned}. Deploy with ads_mcp/scripts/deploy_runtime.py "
        "so a RUNTIME_REVISION stamp is written."
    )

  if revision != pinned:
    raise UnpinnedRuntimeError(
        "Refusing to start the governed tier: runtime revision "
        f"{revision} does not match the pinned {pinned}. Something moved "
        "the deployment underneath the launch configuration. Re-deploy "
        "the intended revision rather than overriding this check."
    )

  if provenance["working_tree_dirty"] is True:
    raise UnpinnedRuntimeError(
        "Refusing to start the governed tier: tracked files under "
        f"{provenance['runtime_root']} differ from {revision}, so the "
        "code being imported is not the reviewed code. A deployment "
        "checkout must never be edited in place."
    )

  return provenance


def describe_provenance(provenance: dict[str, object]) -> str:
  """One-line startup summary for stderr. stdout is the MCP transport."""
  revision = provenance["revision"] or _UNKNOWN
  short = revision[:12] if revision != _UNKNOWN else _UNKNOWN
  state = "PINNED" if provenance["matches_pin"] else "UNPINNED"
  if provenance["working_tree_dirty"] is True:
    state += " DIRTY"
  return (
      f"[ads-mcp runtime] revision={short} source={provenance['revision_source']} "
      f"{state} root={provenance['runtime_root']}"
  )
