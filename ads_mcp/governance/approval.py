# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Approval-gate primitives.

A mutation tool is wrapped into a (propose, apply) pair:

- `propose_*` validates args, generates a code from a hash of args, writes
  a human-readable approval block to pending_approvals/{code}.md, and
  returns the block + code to the caller.
- `apply_*` reads pending_approvals/{code}.md, recomputes the hash from
  the embedded YAML spec, refuses if it doesn't match (= file was edited),
  invokes the underlying mutation, appends an audit row, and archives the
  pending block.

The code is short (6 hex chars) and lives in the markdown title so a
human can reference it conversationally ("approve A7K2P3").
"""

import datetime
import hashlib
import json
import os
import pathlib
import re
from typing import Any

import yaml
from fastmcp.exceptions import ToolError

from ads_mcp.governance import audit


PENDING_DIRNAME = "pending_approvals"
APPLIED_DIRNAME = "applied_approvals"
REJECTED_DIRNAME = "rejected_approvals"

# Matches the fenced yaml spec block inside a pending file.
_SPEC_BLOCK_RE = re.compile(
    r"```yaml\n# DO NOT EDIT \(hash-verified\)\n(.*?)\n```",
    re.DOTALL,
)


# Keys that describe a proposal's PLACE IN THE LIFECYCLE rather than the
# operation it performs. They participate in the hash -- that is the whole
# point, it is what gives a legitimate replacement a different code -- but
# they are excluded when asking "is this the same operation?".
LIFECYCLE_KEYS = frozenset(
    {"supersedes", "proposal_revision", "supersedes_evidence"}
)

# Where a code can already exist. Order matters only for message clarity.
_LIFECYCLE_DIRS = (
    ("pending", PENDING_DIRNAME),
    ("applied", APPLIED_DIRNAME),
    ("rejected", REJECTED_DIRNAME),
)


def _code_matches_filename(name: str, code: str) -> bool:
  """True when a filename carries this code.

  Pending files are `{code}.md`; archived files gain a date prefix,
  `{YYYY-MM-DD}_{code}.md`. Both must be found, or a retired code would
  look free simply because it had been archived.
  """
  if not name.endswith(".md"):
    return False
  stem = name[:-3]
  return stem.upper() == code or stem.upper().endswith("_" + code)


def locate_code(
    client_root: pathlib.Path, code: str
) -> list[tuple[str, pathlib.Path]]:
  """Every place this code already exists, across all three lifecycles.

  Returns (lifecycle, path) pairs. An empty list means the code is free.
  """
  code = code.strip().upper()
  found: list[tuple[str, pathlib.Path]] = []
  for lifecycle, dirname in _LIFECYCLE_DIRS:
    directory = client_root / dirname
    if not directory.is_dir():
      continue
    for path in sorted(directory.iterdir()):
      if path.is_file() and _code_matches_filename(path.name, code):
        found.append((lifecycle, path))
  return found


def _parse_spec_block(path: pathlib.Path) -> dict[str, Any] | None:
  """The hash-verified spec inside an artifact, or None if unreadable.

  Unreadable is never treated as absent by callers: a code whose artifact
  cannot be parsed is still occupied, and returning None makes the caller
  refuse rather than assume.
  """
  try:
    text = path.read_text(encoding="utf-8")
  except OSError:
    return None
  match = _SPEC_BLOCK_RE.search(text)
  if not match:
    return None
  try:
    parsed = yaml.safe_load(match.group(1))
  except yaml.YAMLError:
    return None
  return parsed if isinstance(parsed, dict) else None


def _operation_core(spec_for_hash: dict[str, Any]) -> str:
  """Canonical form of the OPERATION, with lifecycle metadata stripped.

  Two proposals with equal cores do the same thing to the account, even
  if one is a sanctioned replacement of the other.
  """
  core = {k: v for k, v in spec_for_hash.items() if k not in LIFECYCLE_KEYS}
  return json.dumps(core, sort_keys=True, separators=(",", ":"))


def _generate_code(spec: dict[str, Any]) -> str:
  """Deterministic 6-hex-char code from a canonical JSON of the spec.

  Same spec -> same code, so propose is idempotent on identical args.
  Any field drift -> different code, so tampering is detectable.
  """
  canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
  digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()
  return digest[:6].upper()


def _pending_path(client_root: pathlib.Path, code: str) -> pathlib.Path:
  return client_root / PENDING_DIRNAME / f"{code}.md"


def _ensure_dir(path: pathlib.Path) -> None:
  path.mkdir(parents=True, exist_ok=True)


def _resolve_existing_code(
    client_root: pathlib.Path, code: str, spec_for_hash: dict[str, Any]
) -> pathlib.Path | None:
  """Decides what an already-occupied code means.

  Returns the pending path when this is a byte-identical retry of a live
  proposal, which stays idempotent. Returns None when the code is free.
  Every other case raises: a historical code is never reused, and an
  existing file is never overwritten.
  """
  matches = locate_code(client_root, code)
  if not matches:
    return None

  archived = [(lc, pth) for lc, pth in matches if lc != "pending"]
  if archived:
    lifecycles = ", ".join(sorted({lc for lc, _ in archived}))
    paths = "; ".join(str(pth) for _, pth in archived)
    raise ToolError(
        f"Approval code {code} is already used by a {lifecycles} proposal "
        f"({paths}). Historical codes are never reused or overwritten. If "
        "this is a deliberate re-issue of a retired operation, call "
        f"propose again with supersedes='{code}' so the replacement earns "
        "its own deterministic code. If the two specs are not the same "
        "operation, this is a truncated-hash collision and the operation "
        "must be re-framed rather than forced through."
    )

  path = matches[0][1]
  existing = _parse_spec_block(path)
  if existing is None:
    raise ToolError(
        f"Approval code {code} is occupied by {path}, whose hash-verified "
        "spec block could not be read. Refusing to overwrite a proposal "
        "that cannot be understood."
    )
  if existing == spec_for_hash:
    return path
  raise ToolError(
      f"Approval code {code} already belongs to a DIFFERENT pending "
      f"proposal at {path}. Two distinct operations produced the same "
      "6-character code (truncated-hash collision). Refusing to "
      "overwrite; re-frame the operation."
  )


def _verify_supersession(
    client_root: pathlib.Path,
    supersedes: str,
    spec_for_hash: dict[str, Any],
    evidence: str | None,
) -> int:
  """Checks a replacement is a sanctioned re-issue, returns its revision.

  The revision is derived from the retired artifact rather than supplied,
  so a replacement's code stays a pure function of its inputs. Re-running
  the same supersession always yields the same code -- no nonce, no clock.
  """
  matches = locate_code(client_root, supersedes)
  if not matches:
    raise ToolError(
        f"supersedes={supersedes} names no artifact under {client_root}. "
        "A replacement may only be issued against a proposal that "
        "actually exists."
    )

  lifecycles = {lc for lc, _ in matches}
  if "pending" in lifecycles:
    raise ToolError(
        f"{supersedes} is still PENDING. Apply or reject it first, then "
        "issue the replacement. Superseding a live proposal would leave "
        "two approvable codes for one operation."
    )
  if "applied" in lifecycles and not (evidence or "").strip():
    raise ToolError(
        f"{supersedes} was APPLIED. Re-proposing an operation that may "
        "already be live requires supersedes_evidence: an explicit, "
        "recorded statement of how the live account was checked and why "
        "the operation is NOT already in effect. Refusing to silently "
        "re-propose an applied operation."
    )

  path = matches[0][1]
  retired = _parse_spec_block(path)
  if retired is None:
    raise ToolError(
        f"The artifact for {supersedes} at {path} has no readable "
        "hash-verified spec block, so byte-equivalence cannot be "
        "established. Refusing to supersede what cannot be compared."
    )

  if _operation_core(retired) != _operation_core(spec_for_hash):
    raise ToolError(
        f"The replacement is not the same operation as {supersedes}. A "
        "supersession may only re-issue a byte-equivalent operation; the "
        "only permitted differences are lifecycle metadata "
        f"({', '.join(sorted(LIFECYCLE_KEYS))}). Propose this as a new "
        "operation instead of a replacement.\n"
        f"  retired:     {_operation_core(retired)}\n"
        f"  replacement: {_operation_core(spec_for_hash)}"
    )

  prior = retired.get("proposal_revision", 0)
  if isinstance(prior, bool) or not isinstance(prior, int) or prior < 0:
    raise ToolError(
        f"The artifact for {supersedes} carries a non-numeric "
        f"proposal_revision ({prior!r}). Refusing to derive a revision "
        "from an unreadable chain."
    )
  return prior + 1


def _create_exclusively(path: pathlib.Path, text: str) -> bool:
  """Writes only if the path does not exist. True when this call wrote it.

  O_EXCL makes the check and the write one atomic step, so two concurrent
  proposals cannot both believe they created the same code.
  """
  try:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
  except FileExistsError:
    return False
  with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
    handle.write(text)
  return True


def write_proposal(
    client_root: pathlib.Path,
    *,
    tool_name: str,
    customer_id: str,
    operations_human: list[str],
    reason_code: str,
    reason_detail: str | None,
    spec: dict[str, Any],
    client_label: str | None = None,
    supersedes: str | None = None,
    supersedes_evidence: str | None = None,
) -> dict[str, str]:
  """Writes a pending-approval block. Returns the code and block text.

  Args:
      client_root: Resolved per-client folder.
      tool_name: Name of the gated mutation tool (e.g., 'pause_keyword').
      customer_id: Google Ads customer ID.
      operations_human: One-line-each plain-English descriptions of each
          op, for the human reading the proposal.
      reason_code: Controlled vocabulary tag.
      reason_detail: Free-text elaboration.
      spec: Canonical machine-readable spec — used to compute the hash
          AND passed to apply_*. Should include every arg the tool needs.
      client_label: Optional human label like 'Spine & Health' shown in
          the proposal header (falls back to client_root.name).
      supersedes: Code of a retired proposal this one re-issues. The
          replacement must be the same operation, and it earns a new
          deterministic code because this reference and the derived
          revision are part of the hash input. Required to re-propose an
          operation whose code was already retired or applied.
      supersedes_evidence: Mandatory when superseding an APPLIED
          proposal: how the live account was checked, and why the
          operation is not already in effect.

  Raises:
      ToolError: If the code is already used by a pending, applied or
          rejected proposal and this is not a byte-identical retry; if a
          supersession names a missing, still-pending or non-equivalent
          artifact; or if an applied proposal is re-proposed without
          evidence. Nothing is ever overwritten.
  """
  audit.validate_reason(reason_code, reason_detail)
  spec_for_hash: dict[str, Any] = {
      "tool": tool_name,
      "customer_id": customer_id,
      "spec": spec,
  }

  lifecycle_line = ""
  if supersedes is not None:
    supersedes = supersedes.strip().upper()
    revision = _verify_supersession(
        client_root, supersedes, spec_for_hash, supersedes_evidence
    )
    # Lifecycle metadata joins the hash input. That is what gives a
    # sanctioned replacement its own code while keeping the code a pure
    # function of its inputs -- no nonce, no clock, same args same code.
    spec_for_hash["supersedes"] = supersedes
    spec_for_hash["proposal_revision"] = revision
    evidence = (supersedes_evidence or "").strip()
    if evidence:
      spec_for_hash["supersedes_evidence"] = evidence
    lifecycle_line = (
        f"- **Supersedes:** `{supersedes}` (revision {revision}) - the "
        "superseded artifact is preserved unchanged and stays "
        "unapprovable.\n"
    )
    if evidence:
      lifecycle_line += (
          f"- **Not-already-live evidence:** {evidence}\n"
      )

  code = _generate_code(spec_for_hash)

  # Fail closed before writing anything. A byte-identical retry of a live
  # proposal returns the existing file untouched; everything else raises.
  identical_retry = _resolve_existing_code(client_root, code, spec_for_hash)
  if identical_retry is not None:
    return {
        "code": code,
        "proposal_path": str(identical_retry),
        "block": identical_retry.read_text(encoding="utf-8"),
    }

  label = client_label or client_root.name
  timestamp = datetime.datetime.now().isoformat(timespec="seconds")
  detail_line = f" — {reason_detail}" if reason_detail else ""

  ops_block = "\n".join(f"  - {op}" for op in operations_human)
  yaml_spec = yaml.safe_dump(
      spec_for_hash, sort_keys=True, default_flow_style=False
  )

  text = (
      f"# PROPOSED MUTATION · {code}\n\n"
      f"- **Client:** {label}\n"
      f"- **Customer ID:** {customer_id}\n"
      f"- **Tool:** `{tool_name}`\n"
      f"- **Reason:** `{reason_code}`{detail_line}\n"
      f"- **Proposed at:** {timestamp}\n"
      f"{lifecycle_line}"
      f"\n"
      f"## Operations\n\n"
      f"{ops_block}\n\n"
      f"## To approve\n\n"
      f"Tell Claude: **approve {code}**\n\n"
      f"## To reject\n\n"
      f"Tell Claude: **reject {code}**\n\n"
      f"---\n\n"
      f"```yaml\n# DO NOT EDIT (hash-verified)\n{yaml_spec.strip()}\n```\n"
  )

  pending_dir = client_root / PENDING_DIRNAME
  _ensure_dir(pending_dir)
  pending_path = pending_dir / f"{code}.md"

  if not _create_exclusively(pending_path, text):
    # Another writer won the race between the gate above and this write.
    # Resolve it exactly as the gate would, so a concurrent duplicate
    # stays idempotent and a concurrent collision still refuses.
    raced = _resolve_existing_code(client_root, code, spec_for_hash)
    if raced is None:
      raise ToolError(
          f"Approval code {code} appeared at {pending_path} during the "
          "write and then vanished. Refusing to guess; re-run propose."
      )
    return {
        "code": code,
        "proposal_path": str(raced),
        "block": raced.read_text(encoding="utf-8"),
    }

  return {"code": code, "proposal_path": str(pending_path), "block": text}


def read_proposal(
    client_root: pathlib.Path, code: str
) -> dict[str, Any]:
  """Reads a pending proposal, verifies its hash, returns the spec.

  Raises:
      ToolError: If the proposal doesn't exist, the YAML block is missing,
          or the recomputed code doesn't match — meaning the file was
          edited and the proposal is no longer trustworthy.
  """
  code = code.strip().upper()
  pending_path = _pending_path(client_root, code)
  if not pending_path.is_file():
    raise ToolError(
        f"No pending approval found with code {code} at "
        f"{pending_path}. Either propose hasn't been called, or it was "
        f"already applied/rejected."
    )

  text = pending_path.read_text(encoding="utf-8")
  match = _SPEC_BLOCK_RE.search(text)
  if not match:
    raise ToolError(
        f"Pending file {pending_path} is missing its hash-verified yaml "
        "spec block. The file may have been edited or corrupted. Re-run "
        "the propose tool to generate a fresh proposal."
    )

  try:
    spec = yaml.safe_load(match.group(1))
  except yaml.YAMLError as e:
    raise ToolError(f"Could not parse yaml spec block: {e}") from e

  recomputed = _generate_code(spec)
  if recomputed != code:
    raise ToolError(
        f"Hash mismatch for {code}: the pending block's spec produces "
        f"code {recomputed}, not {code}. The yaml block was edited after "
        "the proposal was created. Re-run propose with the new args."
    )

  return spec


def archive_proposal(
    client_root: pathlib.Path,
    code: str,
    *,
    outcome: str,
    note: str | None = None,
) -> pathlib.Path:
  """Moves a pending proposal to applied_/ or rejected_/.

  Returns:
      Path of the archived file.
  """
  code = code.strip().upper()
  pending_path = _pending_path(client_root, code)
  if not pending_path.is_file():
    raise ToolError(f"No pending file at {pending_path}")

  target_dir = client_root / (
      APPLIED_DIRNAME if outcome == "applied" else REJECTED_DIRNAME
  )
  _ensure_dir(target_dir)
  date_prefix = datetime.date.today().isoformat()
  target_path = target_dir / f"{date_prefix}_{code}.md"

  body = pending_path.read_text(encoding="utf-8")
  if note:
    body += f"\n---\n\n## {outcome.upper()} note\n\n{note}\n"
  target_path.write_text(body, encoding="utf-8")
  pending_path.unlink()
  return target_path


def list_pending(client_root: pathlib.Path) -> list[dict[str, Any]]:
  """Lists all pending proposals as {code, path, summary}.

  Summary is the first heading line so the caller can see what each
  pending block is about at a glance.
  """
  pending_dir = client_root / PENDING_DIRNAME
  if not pending_dir.is_dir():
    return []
  out = []
  for md in sorted(pending_dir.glob("*.md")):
    text = md.read_text(encoding="utf-8")
    first_line = text.split("\n", 1)[0].lstrip("# ").strip()
    code = md.stem.upper()
    out.append({"code": code, "path": str(md), "summary": first_line})
  return out
