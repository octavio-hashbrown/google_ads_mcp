# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the gated conversion-action primary_for_goal tool."""

import types
from unittest import mock

from ads_mcp.tools import gated_conversions as gc
from ads_mcp.tools import mutations_gated
from fastmcp.exceptions import ToolError
import pytest


CID = "6922218131"
SIX = [
    "7520930858", "7521055810", "7521059674",
    "7521239184", "7521231816", "7521244788",
]
SUBMIT_FORM = "7521040525"
CALLS_FROM_ADS = "7385858877"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _action_row(
    action_id, name="Action", status="ENABLED", primary=True,
    include=True, category="BOOK_APPOINTMENT", type_="GOOGLE_ANALYTICS_4_CUSTOM",
):
  return types.SimpleNamespace(
      conversion_action=types.SimpleNamespace(
          id=int(action_id),
          name=name,
          status=types.SimpleNamespace(name=status),
          category=types.SimpleNamespace(name=category),
          type_=types.SimpleNamespace(name=type_),
          origin=types.SimpleNamespace(name="WEBSITE"),
          primary_for_goal=primary,
          include_in_conversions_metric=include,
      )
  )


def _volume_row(action_id, conversions=0.0, all_conversions=0.0):
  return types.SimpleNamespace(
      segments=types.SimpleNamespace(
          conversion_action=f"customers/{CID}/conversionActions/{action_id}"
      ),
      metrics=types.SimpleNamespace(
          conversions=conversions, all_conversions=all_conversions
      ),
  )


def _custom_goal_row(name="Goal", goal_id=1, actions=(), status="ENABLED"):
  return types.SimpleNamespace(
      custom_conversion_goal=types.SimpleNamespace(
          id=goal_id,
          name=name,
          status=types.SimpleNamespace(name=status),
          conversion_actions=list(actions),
      )
  )


def _campaign_row(cid=1, name="Camp", status="ENABLED", actions=()):
  return types.SimpleNamespace(
      campaign=types.SimpleNamespace(
          id=cid,
          name=name,
          status=types.SimpleNamespace(name=status),
          selective_optimization=types.SimpleNamespace(
              conversion_actions=list(actions)
          ),
      )
  )


def _client(search_side_effect):
  google_ads = mock.Mock()
  google_ads.search.side_effect = search_side_effect
  conv_service = mock.Mock()
  services = {
      "GoogleAdsService": google_ads,
      "ConversionActionService": conv_service,
  }
  client = mock.Mock()
  client.get_service.side_effect = lambda n: services[n]
  return client, services


def _clean_reads(ids=SIX, primary=True, recent=0.0, extended=0.0, hist=0.0):
  """Standard propose read sequence: actions, custom goals, campaigns, volumes."""
  return [
      [_action_row(i, name=f"Action {i}", primary=primary) for i in ids],
      [],                       # custom_conversion_goal
      [_campaign_row()],        # campaigns, no selective_optimization
      [_volume_row(i, recent) for i in ids],    # recent window
      [_volume_row(i, hist) for i in ids],      # lifetime window
      [_volume_row(i, extended) for i in ids],  # extended window
  ]


# ---------------------------------------------------------------------------
# Registration + field mask
# ---------------------------------------------------------------------------


def test_registered_in_shared_gate():
  assert "set_conversion_actions_primary_for_goal" in mutations_gated._DISPATCH


def test_field_mask_is_exactly_primary_for_goal():
  ops = gc.build_primary_for_goal_operations(CID, SIX, False)
  assert len(ops) == 6
  for op in ops:
    assert list(op.update_mask.paths) == ["primary_for_goal"]
    assert op.update.primary_for_goal is False


def test_operation_touches_no_other_field():
  """Only resource_name and primary_for_goal may appear on the update."""
  op = gc.build_primary_for_goal_operations(CID, ["7520930858"], False)[0]
  set_fields = {f.name for f, _ in op.update._pb.ListFields()}
  assert set_fields <= {"resource_name", "primary_for_goal"}
  assert "include_in_conversions_metric" not in set_fields
  assert "status" not in set_fields
  assert "category" not in set_fields


def test_module_never_writes_include_in_conversions_metric():
  source = open(gc.__file__, encoding="utf-8").read()
  assert "include_in_conversions_metric =" not in source
  assert "FIELD_MASK_PATHS = [\"primary_for_goal\"]" in source


# ---------------------------------------------------------------------------
# Input normalization
# ---------------------------------------------------------------------------


def test_accepts_ids_or_resource_names():
  assert gc._normalize_ids(
      ["7520930858", f"customers/{CID}/conversionActions/7521055810"]
  ) == ["7520930858", "7521055810"]


def test_rejects_duplicates_and_empty():
  with pytest.raises(ToolError):
    gc._normalize_ids(["7520930858", "7520930858"])
  with pytest.raises(ToolError):
    gc._normalize_ids([])


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------


@mock.patch("ads_mcp.tools.gated_conversions._get_client")
@mock.patch("ads_mcp.tools.gated_conversions.audit.resolve_client_root")
def test_six_action_batch_proposal(mock_root, mock_get_client, tmp_path):
  """The whole batch lands in ONE immutable proposal."""
  mock_root.return_value = tmp_path
  client, services = _client(_clean_reads())
  mock_get_client.return_value = client

  result = gc.propose_set_conversion_actions_primary_for_goal(
      customer_id=CID,
      conversion_action_ids=SIX,
      primary_for_goal=False,
      reason_code="client_request",
      protected_conversion_action_ids=[SUBMIT_FORM, CALLS_FROM_ADS],
      extended_window_start="2026-05-01",
      extended_window_end="2026-08-12",
      client_label="Spine & Health",
  )

  block = result["block"]
  for i in SIX:
    assert i in block
  assert "primary_for_goal: True -> False" in block
  assert 'Field mask is exactly ["primary_for_goal"]' in block
  assert "READ ONLY, not changed" in block
  assert "NONE FOUND" in block
  assert SUBMIT_FORM in block and CALLS_FROM_ADS in block
  # Exactly one proposal file for all six.
  assert len(list(tmp_path.glob("pending_approvals/*.md"))) == 1
  # Nothing mutated at propose time.
  services["ConversionActionService"].mutate_conversion_actions.assert_not_called()


@mock.patch("ads_mcp.tools.gated_conversions._get_client")
@mock.patch("ads_mcp.tools.gated_conversions.audit.resolve_client_root")
def test_protected_action_hard_refusal(mock_root, mock_get_client, tmp_path):
  mock_root.return_value = tmp_path
  client, _ = _client(_clean_reads())
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gc.propose_set_conversion_actions_primary_for_goal(
        customer_id=CID,
        conversion_action_ids=SIX + [SUBMIT_FORM],
        primary_for_goal=False,
        reason_code="client_request",
        protected_conversion_action_ids=[SUBMIT_FORM, CALLS_FROM_ADS],
    )
  assert "HARD REFUSAL" in str(exc.value)
  assert SUBMIT_FORM in str(exc.value)
  assert not list(tmp_path.glob("pending_approvals/*.md"))


@mock.patch.dict("os.environ", {"LO_PROTECTED_CONVERSION_ACTIONS": CALLS_FROM_ADS})
@mock.patch("ads_mcp.tools.gated_conversions._get_client")
@mock.patch("ads_mcp.tools.gated_conversions.audit.resolve_client_root")
def test_protected_via_env_var(mock_root, mock_get_client, tmp_path):
  mock_root.return_value = tmp_path
  client, _ = _client(_clean_reads())
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gc.propose_set_conversion_actions_primary_for_goal(
        customer_id=CID,
        conversion_action_ids=[CALLS_FROM_ADS],
        primary_for_goal=False,
        reason_code="client_request",
    )
  assert "HARD REFUSAL" in str(exc.value)


@mock.patch("ads_mcp.tools.gated_conversions._get_client")
@mock.patch("ads_mcp.tools.gated_conversions.audit.resolve_client_root")
def test_nonzero_conversions_refusal(mock_root, mock_get_client, tmp_path):
  mock_root.return_value = tmp_path
  client, _ = _client(_clean_reads(recent=12.0))
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gc.propose_set_conversion_actions_primary_for_goal(
        customer_id=CID,
        conversion_action_ids=SIX,
        primary_for_goal=False,
        reason_code="client_request",
    )
  assert "non-zero Conversions" in str(exc.value)
  assert not list(tmp_path.glob("pending_approvals/*.md"))


@mock.patch("ads_mcp.tools.gated_conversions._get_client")
@mock.patch("ads_mcp.tools.gated_conversions.audit.resolve_client_root")
def test_nonzero_conversions_explicit_override(
    mock_root, mock_get_client, tmp_path
):
  mock_root.return_value = tmp_path
  client, _ = _client(_clean_reads(recent=12.0))
  mock_get_client.return_value = client

  result = gc.propose_set_conversion_actions_primary_for_goal(
      customer_id=CID,
      conversion_action_ids=SIX,
      primary_for_goal=False,
      reason_code="client_request",
      allow_nonzero_recent_conversions=True,
  )
  assert "allow_nonzero_recent_conversions: true" in result["block"].lower()


@mock.patch("ads_mcp.tools.gated_conversions._get_client")
@mock.patch("ads_mcp.tools.gated_conversions.audit.resolve_client_root")
def test_custom_goal_refusal(mock_root, mock_get_client, tmp_path):
  mock_root.return_value = tmp_path
  reads = _clean_reads()
  reads[1] = [_custom_goal_row(
      name="Bookings", goal_id=99,
      actions=[f"customers/{CID}/conversionActions/{SIX[0]}"],
  )]
  client, _ = _client(reads)
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gc.propose_set_conversion_actions_primary_for_goal(
        customer_id=CID,
        conversion_action_ids=SIX,
        primary_for_goal=False,
        reason_code="client_request",
    )
  msg = str(exc.value)
  assert "custom conversion goal" in msg.lower()
  assert "may NOT remove them from bidding" in msg
  assert not list(tmp_path.glob("pending_approvals/*.md"))


@mock.patch("ads_mcp.tools.gated_conversions._get_client")
@mock.patch("ads_mcp.tools.gated_conversions.audit.resolve_client_root")
def test_selective_optimization_refusal(mock_root, mock_get_client, tmp_path):
  mock_root.return_value = tmp_path
  reads = _clean_reads()
  reads[2] = [_campaign_row(
      cid=555, name="Master",
      actions=[f"customers/{CID}/conversionActions/{SIX[2]}"],
  )]
  client, _ = _client(reads)
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gc.propose_set_conversion_actions_primary_for_goal(
        customer_id=CID,
        conversion_action_ids=SIX,
        primary_for_goal=False,
        reason_code="client_request",
    )
  assert "selective_optimization" in str(exc.value)


@mock.patch("ads_mcp.tools.gated_conversions._get_client")
@mock.patch("ads_mcp.tools.gated_conversions.audit.resolve_client_root")
def test_all_already_secondary_refuses_proposal(
    mock_root, mock_get_client, tmp_path
):
  mock_root.return_value = tmp_path
  client, _ = _client(_clean_reads(primary=False))
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gc.propose_set_conversion_actions_primary_for_goal(
        customer_id=CID,
        conversion_action_ids=SIX,
        primary_for_goal=False,
        reason_code="client_request",
    )
  assert "already have" in str(exc.value)


@mock.patch("ads_mcp.tools.gated_conversions._get_client")
@mock.patch("ads_mcp.tools.gated_conversions.audit.resolve_client_root")
def test_missing_action_refused(mock_root, mock_get_client, tmp_path):
  mock_root.return_value = tmp_path
  reads = _clean_reads()
  reads[0] = [_action_row(i) for i in SIX[:-1]]  # one missing
  client, _ = _client(reads)
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gc.propose_set_conversion_actions_primary_for_goal(
        customer_id=CID,
        conversion_action_ids=SIX,
        primary_for_goal=False,
        reason_code="client_request",
    )
  assert "not found" in str(exc.value)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _spec(**overrides):
  spec = {
      "op": "set_conversion_actions_primary_for_goal",
      "conversion_action_ids": SIX,
      "primary_for_goal": False,
      "before_primary_for_goal": {i: True for i in SIX},
      "protected_conversion_action_ids": [CALLS_FROM_ADS, SUBMIT_FORM],
      "allow_nonzero_recent_conversions": False,
      "windows": {},
  }
  spec.update(overrides)
  return spec


def _apply_reads(before_primary=True, after_primary=False, goals=None,
                 campaigns=None):
  return [
      [_action_row(i, primary=before_primary) for i in SIX],   # current
      goals if goals is not None else [],                      # custom goals
      campaigns if campaigns is not None else [_campaign_row()],
      [_action_row(i, primary=after_primary) for i in SIX],    # verification
  ]


def test_apply_batches_all_six_atomically():
  client, services = _client(_apply_reads())
  result = gc._execute_set_conversion_actions_primary_for_goal(
      client, CID, _spec()
  )

  assert result["outcome"] == "applied"
  assert result["mutated"] == SIX
  assert result["field_mask"] == ["primary_for_goal"]
  assert result["partial_failure"] is False

  svc = services["ConversionActionService"]
  svc.mutate_conversion_actions.assert_called_once()
  req = svc.mutate_conversion_actions.call_args.kwargs["request"]
  assert len(req.operations) == 6          # ONE request, six operations
  assert req.partial_failure is False      # all-or-nothing
  assert req.validate_only is False
  for op in req.operations:
    assert list(op.update_mask.paths) == ["primary_for_goal"]


def test_apply_reports_but_never_sets_include_in_conversions_metric():
  client, _ = _client(_apply_reads())
  result = gc._execute_set_conversion_actions_primary_for_goal(
      client, CID, _spec()
  )
  for row in result["verified"]:
    assert "include_in_conversions_metric" in row
  # And the operation itself never carries the field.
  ops = gc.build_primary_for_goal_operations(CID, SIX, False)
  for op in ops:
    assert not op.update._pb.HasField("include_in_conversions_metric")


def test_apply_no_op_when_already_secondary():
  client, services = _client([
      [_action_row(i, primary=False) for i in SIX],
      [], [_campaign_row()],
  ])
  result = gc._execute_set_conversion_actions_primary_for_goal(
      client, CID, _spec()
  )
  assert result["outcome"] == "no_op"
  services["ConversionActionService"].mutate_conversion_actions.assert_not_called()


def test_apply_refuses_on_primary_state_drift():
  """Approved BEFORE=False, but the account now reads True."""
  client, services = _client([
      [_action_row(SIX[0], primary=True)],   # account state now
      [], [_campaign_row()],
  ])
  spec = _spec(
      conversion_action_ids=[SIX[0]],
      before_primary_for_goal={SIX[0]: False},   # what was approved
  )
  with pytest.raises(ToolError) as exc:
    gc._execute_set_conversion_actions_primary_for_goal(client, CID, spec)
  assert "changed since" in str(exc.value)
  services["ConversionActionService"].mutate_conversion_actions.assert_not_called()


def test_apply_refuses_on_custom_goal_drift():
  client, services = _client(_apply_reads(goals=[
      _custom_goal_row(actions=[f"customers/{CID}/conversionActions/{SIX[1]}"])
  ]))
  with pytest.raises(ToolError) as exc:
    gc._execute_set_conversion_actions_primary_for_goal(client, CID, _spec())
  assert "not the case when this was approved" in str(exc.value)
  services["ConversionActionService"].mutate_conversion_actions.assert_not_called()


def test_apply_refuses_protected_action():
  ids = SIX + [SUBMIT_FORM]
  client, services = _client([])
  with pytest.raises(ToolError) as exc:
    gc._execute_set_conversion_actions_primary_for_goal(
        client, CID, _spec(conversion_action_ids=ids)
    )
  assert "HARD REFUSAL at apply" in str(exc.value)
  services["ConversionActionService"].mutate_conversion_actions.assert_not_called()


def test_apply_fails_loudly_if_verification_disagrees():
  """A silent partial apply is worse than a loud failure."""
  client, _ = _client(_apply_reads(after_primary=True))  # never took effect
  with pytest.raises(ToolError) as exc:
    gc._execute_set_conversion_actions_primary_for_goal(client, CID, _spec())
  assert "verification FAILED" in str(exc.value)
