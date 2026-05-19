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
  """
  audit.validate_reason(reason_code, reason_detail)
  spec_for_hash = {
      "tool": tool_name,
      "customer_id": customer_id,
      "spec": spec,
  }
  code = _generate_code(spec_for_hash)
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
      f"- **Proposed at:** {timestamp}\n\n"
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
  pending_path.write_text(text, encoding="utf-8")

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
