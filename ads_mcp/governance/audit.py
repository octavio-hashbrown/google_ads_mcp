# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Audit log writer for applied mutations.

Appends a structured row to {CLIENT_ROOT}/client_audit_log.md for every
applied mutation. The log is markdown so a human can read it; each row is
also self-contained for grep-based queries.
"""

import datetime
import os
import pathlib
from typing import Any

from fastmcp.exceptions import ToolError


AUDIT_LOG_FILENAME = "client_audit_log.md"

# Controlled vocabulary for reason_code. `other` requires reason_detail.
VALID_REASON_CODES = frozenset(
    [
        "underperforming",
        "irrelevant_intent",
        "competitor_term",
        "low_quality_score",
        "budget_protection",
        "negative_consolidation",
        "client_request",
        "other",
    ]
)


def resolve_client_root(client_root: str | None) -> pathlib.Path:
  """Resolves the per-client root directory.

  Args:
      client_root: Explicit path passed by the caller, or None.

  Returns:
      pathlib.Path pointing at the client's working folder.

  Raises:
      ToolError: If neither arg nor env var is set, or path doesn't exist.
  """
  root = client_root or os.environ.get("LO_AGENCY_CLIENT_ROOT")
  if not root:
    raise ToolError(
        "Client root not set. Pass client_root= or set "
        "LO_AGENCY_CLIENT_ROOT env var. This is required so the approval "
        "gate writes pending blocks and audit rows to the right client "
        "folder."
    )
  path = pathlib.Path(root)
  if not path.is_dir():
    raise ToolError(
        f"Client root does not exist or is not a directory: {path}"
    )
  return path


def validate_reason(reason_code: str, reason_detail: str | None) -> None:
  """Checks reason_code is in vocabulary and detail is present when needed.

  Args:
      reason_code: Controlled vocabulary tag.
      reason_detail: Free-text elaboration (required when code is 'other').

  Raises:
      ToolError: On invalid combinations.
  """
  if reason_code not in VALID_REASON_CODES:
    valid = ", ".join(sorted(VALID_REASON_CODES))
    raise ToolError(
        f"Invalid reason_code: {reason_code!r}. Valid codes: {valid}"
    )
  if reason_code == "other" and not (reason_detail and reason_detail.strip()):
    raise ToolError(
        "reason_detail is required when reason_code='other'. Explain why "
        "this mutation doesn't fit a standard reason category."
    )


def append_audit_row(
    client_root: pathlib.Path,
    *,
    code: str,
    customer_id: str,
    tool_name: str,
    reason_code: str,
    reason_detail: str | None,
    operations_summary: str,
    api_result: dict[str, Any] | str,
    outcome: str,
) -> str:
  """Appends a row to the client's audit log. Returns the row written.

  Creates the file with a header if it doesn't exist. Each entry is a
  fenced markdown block — readable in a viewer, parseable by grep.
  """
  log_path = client_root / AUDIT_LOG_FILENAME
  timestamp = datetime.datetime.now().isoformat(timespec="seconds")

  if not log_path.exists():
    log_path.write_text(
        "# Client Audit Log\n\n"
        "Every applied Google Ads mutation through the gated MCP "
        "writes a row here. Append-only by convention.\n\n",
        encoding="utf-8",
    )

  detail_line = f" — {reason_detail}" if reason_detail else ""
  row = (
      f"## {timestamp} · {tool_name} · {outcome}\n\n"
      f"- **Code:** `{code}`\n"
      f"- **Customer ID:** {customer_id}\n"
      f"- **Reason:** `{reason_code}`{detail_line}\n"
      f"- **Operations:** {operations_summary}\n"
      f"- **API result:** `{api_result}`\n\n"
  )

  with log_path.open("a", encoding="utf-8") as f:
    f.write(row)

  return row
