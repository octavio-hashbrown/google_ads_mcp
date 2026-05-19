# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the Lo Media governance layer (audit + approval gate).

These tests cover the file-based primitives (no Google Ads API mocking).
Mutation-tool tests would require mocking the full client and are
deferred to a v2 pass.
"""

import pathlib

import pytest
from fastmcp.exceptions import ToolError

from ads_mcp.governance import approval
from ads_mcp.governance import audit


@pytest.fixture
def client_root(tmp_path: pathlib.Path) -> pathlib.Path:
  """A throwaway client root directory."""
  return tmp_path


def _example_spec() -> dict:
  return {
      "tool": "pause_keyword",
      "customer_id": "1234567890",
      "spec": {
          "op": "pause_keyword",
          "criterion_resource_name": "customers/1234567890/adGroupCriteria/1~2",
          "login_customer_id": None,
      },
  }


# -----------------------------------------------------------------------------
# Code generation
# -----------------------------------------------------------------------------


def test_code_is_deterministic():
  spec = _example_spec()
  assert approval._generate_code(spec) == approval._generate_code(spec)


def test_code_changes_when_spec_changes():
  base = _example_spec()
  modified = _example_spec()
  modified["spec"]["criterion_resource_name"] = "customers/1234567890/adGroupCriteria/9~9"
  assert approval._generate_code(base) != approval._generate_code(modified)


def test_code_is_six_hex_chars():
  code = approval._generate_code(_example_spec())
  assert len(code) == 6
  assert all(c in "0123456789ABCDEF" for c in code)


# -----------------------------------------------------------------------------
# Reason validation
# -----------------------------------------------------------------------------


def test_valid_reason_code_passes():
  audit.validate_reason("underperforming", None)
  audit.validate_reason("other", "manual override for ad group cleanup")


def test_invalid_reason_code_raises():
  with pytest.raises(ToolError, match="Invalid reason_code"):
    audit.validate_reason("bogus_reason", None)


def test_other_requires_detail():
  with pytest.raises(ToolError, match="reason_detail is required"):
    audit.validate_reason("other", None)
  with pytest.raises(ToolError, match="reason_detail is required"):
    audit.validate_reason("other", "")


# -----------------------------------------------------------------------------
# Client root resolution
# -----------------------------------------------------------------------------


def test_resolve_client_root_explicit_arg(client_root: pathlib.Path):
  resolved = audit.resolve_client_root(str(client_root))
  assert resolved == client_root


def test_resolve_client_root_missing_raises(monkeypatch):
  monkeypatch.delenv("LO_AGENCY_CLIENT_ROOT", raising=False)
  with pytest.raises(ToolError, match="Client root not set"):
    audit.resolve_client_root(None)


def test_resolve_client_root_nonexistent_raises(tmp_path: pathlib.Path):
  with pytest.raises(ToolError, match="does not exist"):
    audit.resolve_client_root(str(tmp_path / "does_not_exist"))


# -----------------------------------------------------------------------------
# Propose / read / archive round-trip
# -----------------------------------------------------------------------------


def test_write_then_read_round_trip(client_root: pathlib.Path):
  result = approval.write_proposal(
      client_root,
      tool_name="pause_keyword",
      customer_id="1234567890",
      operations_human=["Pause keyword \"foo\" [EXACT] in ad group \"X\""],
      reason_code="underperforming",
      reason_detail=None,
      spec={
          "op": "pause_keyword",
          "criterion_resource_name": "customers/1234567890/adGroupCriteria/1~2",
          "login_customer_id": None,
      },
  )
  code = result["code"]
  spec = approval.read_proposal(client_root, code)
  assert spec["tool"] == "pause_keyword"
  assert spec["customer_id"] == "1234567890"
  assert spec["spec"]["op"] == "pause_keyword"


def test_read_proposal_missing_raises(client_root: pathlib.Path):
  with pytest.raises(ToolError, match="No pending approval"):
    approval.read_proposal(client_root, "ABCDEF")


def test_tampered_proposal_fails_hash_check(client_root: pathlib.Path):
  """Editing the yaml block invalidates the code and apply must refuse."""
  result = approval.write_proposal(
      client_root,
      tool_name="pause_keyword",
      customer_id="1234567890",
      operations_human=["Pause keyword \"foo\" [EXACT] in ad group \"X\""],
      reason_code="underperforming",
      reason_detail=None,
      spec={
          "op": "pause_keyword",
          "criterion_resource_name": "customers/1234567890/adGroupCriteria/1~2",
          "login_customer_id": None,
      },
  )
  code = result["code"]
  pending_path = client_root / approval.PENDING_DIRNAME / f"{code}.md"
  text = pending_path.read_text(encoding="utf-8")
  tampered = text.replace(
      "adGroupCriteria/1~2", "adGroupCriteria/999~999"
  )
  pending_path.write_text(tampered, encoding="utf-8")

  with pytest.raises(ToolError, match="Hash mismatch"):
    approval.read_proposal(client_root, code)


def test_archive_proposal_moves_file(client_root: pathlib.Path):
  result = approval.write_proposal(
      client_root,
      tool_name="pause_keyword",
      customer_id="1234567890",
      operations_human=["Pause keyword \"foo\" [EXACT] in ad group \"X\""],
      reason_code="underperforming",
      reason_detail=None,
      spec={
          "op": "pause_keyword",
          "criterion_resource_name": "customers/1234567890/adGroupCriteria/1~2",
          "login_customer_id": None,
      },
  )
  code = result["code"]
  archived = approval.archive_proposal(
      client_root, code, outcome="applied"
  )
  assert archived.is_file()
  assert "applied_approvals" in str(archived)
  # Pending file is gone
  assert not (
      client_root / approval.PENDING_DIRNAME / f"{code}.md"
  ).exists()


def test_archive_rejected_includes_note(client_root: pathlib.Path):
  result = approval.write_proposal(
      client_root,
      tool_name="pause_keyword",
      customer_id="1234567890",
      operations_human=["Pause keyword \"foo\" [EXACT] in ad group \"X\""],
      reason_code="underperforming",
      reason_detail=None,
      spec={
          "op": "pause_keyword",
          "criterion_resource_name": "customers/1234567890/adGroupCriteria/1~2",
          "login_customer_id": None,
      },
  )
  code = result["code"]
  archived = approval.archive_proposal(
      client_root,
      code,
      outcome="rejected",
      note="changed my mind — keyword is still converting",
  )
  body = archived.read_text(encoding="utf-8")
  assert "REJECTED note" in body
  assert "changed my mind" in body


# -----------------------------------------------------------------------------
# Listing pending
# -----------------------------------------------------------------------------


def test_list_pending_empty(client_root: pathlib.Path):
  assert approval.list_pending(client_root) == []


def test_list_pending_returns_summaries(client_root: pathlib.Path):
  for crit_id in ("1~2", "3~4"):
    approval.write_proposal(
        client_root,
        tool_name="pause_keyword",
        customer_id="1234567890",
        operations_human=[f"Pause keyword in ad group at {crit_id}"],
        reason_code="underperforming",
        reason_detail=None,
        spec={
            "op": "pause_keyword",
            "criterion_resource_name": f"customers/1234567890/adGroupCriteria/{crit_id}",
            "login_customer_id": None,
        },
    )
  pending = approval.list_pending(client_root)
  assert len(pending) == 2
  for entry in pending:
    assert entry["code"].startswith(("A", "B", "C", "D", "E", "F")) or entry["code"][0].isdigit()
    assert "PROPOSED MUTATION" in entry["summary"]


# -----------------------------------------------------------------------------
# Audit row format
# -----------------------------------------------------------------------------


def test_append_audit_row_creates_log(client_root: pathlib.Path):
  row = audit.append_audit_row(
      client_root,
      code="ABC123",
      customer_id="1234567890",
      tool_name="pause_keyword",
      reason_code="underperforming",
      reason_detail=None,
      operations_summary="Pause keyword \"foo\" [EXACT]",
      api_result={"updated": ["customers/1234567890/adGroupCriteria/1~2"]},
      outcome="applied",
  )
  log = client_root / audit.AUDIT_LOG_FILENAME
  assert log.is_file()
  body = log.read_text(encoding="utf-8")
  assert "# Client Audit Log" in body
  assert "ABC123" in body
  assert "pause_keyword" in body
  assert "applied" in body
  assert row in body


def test_audit_row_appends_not_overwrites(client_root: pathlib.Path):
  for i in range(3):
    audit.append_audit_row(
        client_root,
        code=f"COD{i:03d}",
        customer_id="1234567890",
        tool_name="pause_keyword",
        reason_code="underperforming",
        reason_detail=None,
        operations_summary=f"row {i}",
        api_result="ok",
        outcome="applied",
    )
  body = (client_root / audit.AUDIT_LOG_FILENAME).read_text(encoding="utf-8")
  for i in range(3):
    assert f"COD{i:03d}" in body
