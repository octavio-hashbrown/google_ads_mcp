# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the gated composite campaign migration."""

import types
from unittest import mock

from ads_mcp.tools import gated_migration as gm
from fastmcp.exceptions import ToolError
import pytest


CID = "6922218131"
AG_CORE = f"customers/{CID}/adGroups/202267797514"
AG_MONTVALE = f"customers/{CID}/adGroups/202267799234"
AG_SOURCE = f"customers/{CID}/adGroups/193500738405"
CAMP_MASTER = f"customers/{CID}/campaigns/23737424860"
CAMP_BRAND = f"customers/{CID}/campaigns/24130111127"
CAMP_GROWTH = f"customers/{CID}/campaigns/24130111343"
BUDGET_MASTER = f"customers/{CID}/campaignBudgets/15493940317"
BUDGET_BRAND = f"customers/{CID}/campaignBudgets/15791976151"
CRIT_A = f"customers/{CID}/adGroupCriteria/196678321842~111"
CRIT_B = f"customers/{CID}/adGroupCriteria/196678321842~222"


# -----------------------------------------------------------------------------
# Builders
# -----------------------------------------------------------------------------


def test_ad_group_cpc_builder_masks_only_the_bid_leaf():
  op = gm.build_ad_group_cpc_operation(AG_CORE, 6_500_000)
  assert gm.operation_kind(op) == "ad_group_operation"
  assert list(op.ad_group_operation.update_mask.paths) == ["cpc_bid_micros"]
  assert op.ad_group_operation.update.cpc_bid_micros == 6_500_000
  assert op.ad_group_operation.update.resource_name == AG_CORE


@pytest.mark.parametrize("bad", [0, -1, -6_500_000])
def test_ad_group_cpc_builder_refuses_non_positive_bids(bad):
  with pytest.raises(ToolError, match="must be positive"):
    gm.build_ad_group_cpc_operation(AG_CORE, bad)


def test_budget_builder_updates_amount_only():
  op = gm.build_campaign_budget_amount_operation(BUDGET_MASTER, 115_000_000)
  assert gm.operation_kind(op) == "campaign_budget_operation"
  assert list(op.campaign_budget_operation.update_mask.paths) == [
      "amount_micros"
  ]
  assert op.campaign_budget_operation.update.amount_micros == 115_000_000
  # An update, never a create — the oneof must not carry a create.
  assert op.campaign_budget_operation._pb.WhichOneof("operation") == "update"


def test_budget_builder_refuses_non_positive_amount():
  with pytest.raises(ToolError, match="must be positive"):
    gm.build_campaign_budget_amount_operation(BUDGET_MASTER, 0)


@pytest.mark.parametrize("status", ["ENABLED", "PAUSED"])
def test_campaign_status_builder_accepts_both_allowed_states(status):
  op = gm.build_campaign_status_operation(CAMP_BRAND, status)
  assert gm.operation_kind(op) == "campaign_operation"
  assert list(op.campaign_operation.update_mask.paths) == ["status"]
  assert op.campaign_operation.update.status.name == status


@pytest.mark.parametrize("bad", ["REMOVED", "UNKNOWN", "enabled", ""])
def test_campaign_status_builder_refuses_anything_else(bad):
  with pytest.raises(ToolError, match="must be one of"):
    gm.build_campaign_status_operation(CAMP_BRAND, bad)


@pytest.mark.parametrize("status", ["ENABLED", "PAUSED"])
def test_ad_group_status_builder_accepts_both_allowed_states(status):
  op = gm.build_ad_group_status_operation(AG_SOURCE, status)
  assert gm.operation_kind(op) == "ad_group_operation"
  assert list(op.ad_group_operation.update_mask.paths) == ["status"]
  assert op.ad_group_operation.update.status.name == status


def test_ad_group_status_builder_refuses_removed():
  with pytest.raises(ToolError, match="must be one of"):
    gm.build_ad_group_status_operation(AG_SOURCE, "REMOVED")


def test_language_builder_creates_a_language_criterion():
  op = gm.build_campaign_language_operation(CAMP_BRAND, "languageConstants/1000")
  assert gm.operation_kind(op) == "campaign_criterion_operation"
  create = op.campaign_criterion_operation.create
  assert create.campaign == CAMP_BRAND
  assert create.language.language_constant == "languageConstants/1000"
  # It must not be a negative criterion.
  assert create.negative is False


@pytest.mark.parametrize("bad", ["1000", "geoTargetConstants/1000", "en"])
def test_language_builder_refuses_non_language_constants(bad):
  with pytest.raises(ToolError, match="languageConstants"):
    gm.build_campaign_language_operation(CAMP_BRAND, bad)


def test_criterion_status_builder_masks_only_status():
  op = gm.build_ad_group_criterion_status_operation(CRIT_A, "PAUSED")
  assert gm.operation_kind(op) == "ad_group_criterion_operation"
  assert list(op.ad_group_criterion_operation.update_mask.paths) == ["status"]
  assert op.ad_group_criterion_operation.update.status.name == "PAUSED"


# -----------------------------------------------------------------------------
# Protected state
# -----------------------------------------------------------------------------


def test_growth_is_protected_by_default():
  assert "24130111343" in gm.protected_campaign_ids()


def test_protected_set_is_union_not_replacement(monkeypatch):
  monkeypatch.setenv(gm._PROTECTED_CAMPAIGN_ENV_VAR, "999")
  ids = gm.protected_campaign_ids(["888"])
  assert "24130111343" in ids, "env override must not unprotect Growth"
  assert {"999", "888"} <= ids


def test_scope_check_refuses_protected_campaign_target():
  op = gm.build_campaign_status_operation(CAMP_GROWTH, "ENABLED")
  with pytest.raises(ToolError, match="protected campaign"):
    gm.assert_operations_within_scope(
        [op], {CAMP_GROWTH}, gm.protected_campaign_ids()
    )


def test_scope_check_refuses_target_outside_allowlist():
  op = gm.build_ad_group_cpc_operation(AG_MONTVALE, 9_000_000)
  with pytest.raises(ToolError, match="not in the approved resource"):
    gm.assert_operations_within_scope([op], {AG_CORE}, set())


def test_scope_check_refuses_disallowed_operation_kind():
  from ads_mcp.tools._ads_api import resource_types, service_types

  action = resource_types.ConversionAction(
      resource_name=f"customers/{CID}/conversionActions/7521040525"
  )
  inner = service_types.ConversionActionOperation(update=action)
  op = service_types.MutateOperation(conversion_action_operation=inner)
  with pytest.raises(ToolError, match="not permitted to emit"):
    gm.assert_operations_within_scope([op], set(), set())


def test_scope_check_refuses_non_language_campaign_criterion():
  from ads_mcp.tools._ads_api import common_types, resource_types, service_types

  criterion = resource_types.CampaignCriterion(
      campaign=CAMP_BRAND,
      negative=True,
      keyword=common_types.KeywordInfo(text="free"),
  )
  inner = service_types.CampaignCriterionOperation(create=criterion)
  op = service_types.MutateOperation(campaign_criterion_operation=inner)
  with pytest.raises(ToolError, match="not a LANGUAGE criterion"):
    gm.assert_operations_within_scope([op], {CAMP_BRAND}, set())


def test_scope_check_passes_the_real_migration_shape():
  ops = [
      gm.build_ad_group_cpc_operation(AG_CORE, 6_500_000),
      gm.build_campaign_budget_amount_operation(BUDGET_MASTER, 115_000_000),
      gm.build_ad_group_criterion_status_operation(CRIT_A, "PAUSED"),
      gm.build_campaign_language_operation(
          CAMP_BRAND, "languageConstants/1000"
      ),
      gm.build_ad_group_status_operation(AG_SOURCE, "PAUSED"),
      gm.build_campaign_status_operation(CAMP_BRAND, "ENABLED"),
  ]
  allowed = {AG_CORE, BUDGET_MASTER, CRIT_A, CAMP_BRAND, AG_SOURCE}
  gm.assert_operations_within_scope(ops, allowed, gm.protected_campaign_ids())


# -----------------------------------------------------------------------------
# Rule 4 conflict logic
# -----------------------------------------------------------------------------


def test_phrase_negative_blocks_containing_positive():
  assert gm._phrase_blocks("montvale", "spine and health montvale")


def test_phrase_negative_does_not_block_unrelated_positive():
  assert not gm._phrase_blocks("montvale", "spine and health center nj")


def test_phrase_negative_requires_contiguous_tokens():
  assert not gm._phrase_blocks(
      "spine closter", "spine and health center closter"
  )


def test_exact_negative_matches_only_identical_token_sequence():
  assert gm._exact_blocks("schroth method", "Schroth Method")
  assert not gm._exact_blocks("schroth method", "schroth method near me")


# -----------------------------------------------------------------------------
# Composite assembly
# -----------------------------------------------------------------------------


def _spec(**overrides):
  spec = {
      "op": gm.OP,
      "migration_label": "Brand migration",
      "ad_group_cpc_updates": [{
          "ad_group_resource_name": AG_CORE,
          "label": "Brand - Core",
          "before_micros": 6_000_000,
          "after_micros": 6_500_000,
      }],
      "budget_updates": [{
          "budget_resource_name": BUDGET_MASTER,
          "label": "Master",
          "before_micros": 160_000_000,
          "after_micros": 115_000_000,
      }],
      "budget_verifications": [{
          "budget_resource_name": BUDGET_BRAND,
          "label": "Brand",
          "expect_micros": 45_000_000,
          "expect_reference_count": 1,
          "observed_micros": 45_000_000,
          "observed_reference_count": 1,
          "explicitly_shared": True,
      }],
      "ad_group_status_updates": [{
          "ad_group_resource_name": AG_SOURCE,
          "label": "Brand Campaign",
          "before": "ENABLED",
          "after": "PAUSED",
      }],
      "campaign_status_updates": [{
          "campaign_resource_name": CAMP_BRAND,
          "label": "Brand",
          "before": "PAUSED",
          "after": "ENABLED",
          "expect_budget_micros": 45_000_000,
      }],
      "keyword_pauses": [{
          "criterion_resource_name": CRIT_A,
          "text": "spine and health center closter",
          "match_type": "EXACT",
          "before_status": "ENABLED",
          "ad_group_label": "Closter High Intent",
      }],
      "language_additions": [{
          "campaign_resource_name": CAMP_BRAND,
          "label": "Brand",
          "language_constant": "languageConstants/1000",
          "observed_languages": [],
      }],
      "guard_master_campaign": {
          "resource_name": CAMP_MASTER,
          "expect_status": "ENABLED",
          "expect_bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
          "expect_tcpa_micros": 70_000_000,
      },
      "guard_no_selective_optimization": [CAMP_MASTER, CAMP_BRAND],
      "require_primary_conversion_action_ids": ["7521040525", "7385858877"],
      "rule4_campaign_resource_names": [CAMP_BRAND],
      "protected_campaign_ids": ["24130111343"],
      "login_customer_id": "7788207317",
  }
  spec.update(overrides)
  return spec


def test_build_emits_one_operation_per_change():
  ops, lines = gm.build_migration_operations(_spec(), {CAMP_BRAND: []})
  assert len(ops) == 6
  kinds = [gm.operation_kind(o) for o in ops]
  assert kinds.count("ad_group_operation") == 2  # one CPC, one status
  assert kinds.count("campaign_budget_operation") == 1
  assert kinds.count("ad_group_criterion_operation") == 1
  assert kinds.count("campaign_criterion_operation") == 1
  assert kinds.count("campaign_operation") == 1
  assert any("6.00" in ln and "6.50" in ln for ln in lines)


def test_language_addition_is_idempotent_when_already_present():
  ops, lines = gm.build_migration_operations(
      _spec(), {CAMP_BRAND: ["languageConstants/1000"]}
  )
  assert len(ops) == 5
  assert not any(
      gm.operation_kind(o) == "campaign_criterion_operation" for o in ops
  )
  assert any("already" in ln and "idempotent" in ln for ln in lines)


def test_allowed_resource_names_covers_every_touched_resource():
  names = gm.allowed_resource_names(_spec())
  assert names == {AG_CORE, BUDGET_MASTER, AG_SOURCE, CAMP_BRAND, CRIT_A}
  assert BUDGET_BRAND not in names, "verify-only budget must not be writable"


# -----------------------------------------------------------------------------
# Fake client for drift / execute tests
# -----------------------------------------------------------------------------


class _FakeSearch:
  """Answers GAQL by matching on distinguishing substrings."""

  def __init__(self, state):
    self.state = state

  def __call__(self, customer_id, query):
    s = self.state
    if "FROM campaign " in query and "campaign.resource_name" in query:
      rn = CAMP_MASTER if CAMP_MASTER in query else CAMP_BRAND
      c = s["campaigns"][rn]
      return [
          types.SimpleNamespace(
              campaign=types.SimpleNamespace(
                  name=c["name"],
                  status=types.SimpleNamespace(name=c["status"]),
                  bidding_strategy_type=types.SimpleNamespace(
                      name=c["strategy"]
                  ),
                  maximize_conversions=types.SimpleNamespace(
                      target_cpa_micros=c["tcpa"]
                  ),
                  selective_optimization=types.SimpleNamespace(
                      conversion_actions=c.get("selective", [])
                  ),
              ),
              campaign_budget=types.SimpleNamespace(
                  resource_name=c["budget_rn"],
                  amount_micros=s["budgets"][c["budget_rn"]]["amount"],
                  reference_count=s["budgets"][c["budget_rn"]]["refs"],
                  explicitly_shared=True,
              ),
          )
      ]
    if "FROM campaign_budget" in query:
      rn = next(r for r in s["budgets"] if r in query)
      b = s["budgets"][rn]
      return [
          types.SimpleNamespace(
              campaign_budget=types.SimpleNamespace(
                  amount_micros=b["amount"], reference_count=b["refs"]
              )
          )
      ]
    if "FROM ad_group " in query:
      rn = next(r for r in s["ad_groups"] if r in query)
      a = s["ad_groups"][rn]
      return [
          types.SimpleNamespace(
              ad_group=types.SimpleNamespace(
                  name=a["name"],
                  status=types.SimpleNamespace(name=a["status"]),
                  cpc_bid_micros=a["cpc"],
              )
          )
      ]
    if "FROM ad_group_criterion" in query and "resource_name =" in query:
      rn = next((r for r in s["criteria"] if r in query), None)
      if rn is None or s["criteria"][rn] is None:
        return []
      c = s["criteria"][rn]
      return [
          types.SimpleNamespace(
              ad_group_criterion=types.SimpleNamespace(
                  resource_name=rn,
                  keyword=types.SimpleNamespace(
                      text=c["text"],
                      match_type=types.SimpleNamespace(name=c["match"]),
                  ),
                  status=types.SimpleNamespace(name=c["status"]),
                  negative=False,
              ),
              ad_group=types.SimpleNamespace(name="Closter High Intent"),
          )
      ]
    if "campaign_criterion.type = 'LANGUAGE'" in query:
      return [
          types.SimpleNamespace(
              campaign_criterion=types.SimpleNamespace(
                  language=types.SimpleNamespace(language_constant=lang)
              )
          )
          for lang in s["languages"]
      ]
    if "FROM conversion_action" in query:
      return [
          types.SimpleNamespace(
              conversion_action=types.SimpleNamespace(
                  id=int(k),
                  name=v["name"],
                  status=types.SimpleNamespace(name=v["status"]),
                  primary_for_goal=v["primary"],
              )
          )
          for k, v in s["actions"].items()
      ]
    if "FROM custom_conversion_goal" in query:
      return [
          types.SimpleNamespace(
              custom_conversion_goal=types.SimpleNamespace(
                  id=1, status=types.SimpleNamespace(name="ENABLED")
              )
          )
          for _ in range(s["custom_goals"])
      ]
    if "FROM ad_group_criterion" in query or "FROM keyword_view" in query:
      return []  # Rule 4: no negatives, no positives -> no conflicts
    return []


def _state(**overrides):
  s = {
      "campaigns": {
          CAMP_MASTER: {
              "name": "Master",
              "status": "ENABLED",
              "strategy": "MAXIMIZE_CONVERSIONS",
              "tcpa": 70_000_000,
              "budget_rn": BUDGET_MASTER,
          },
          CAMP_BRAND: {
              "name": "Brand",
              "status": "PAUSED",
              "strategy": "MANUAL_CPC",
              "tcpa": 0,
              "budget_rn": BUDGET_BRAND,
          },
      },
      "budgets": {
          BUDGET_MASTER: {"amount": 160_000_000, "refs": 1},
          BUDGET_BRAND: {"amount": 45_000_000, "refs": 1},
      },
      "ad_groups": {
          AG_CORE: {"name": "Brand - Core", "status": "ENABLED", "cpc": 6_000_000},
          AG_SOURCE: {
              "name": "Brand Campaign",
              "status": "ENABLED",
              "cpc": 10_000,
          },
      },
      "criteria": {
          CRIT_A: {
              "text": "spine and health center closter",
              "match": "EXACT",
              "status": "ENABLED",
          }
      },
      "languages": [],
      "actions": {
          "7521040525": {
              "name": "submit_form",
              "status": "ENABLED",
              "primary": True,
          },
          "7385858877": {
              "name": "Calls from ads",
              "status": "ENABLED",
              "primary": True,
          },
      },
      "custom_goals": 0,
  }
  s.update(overrides)
  return s


def _client(state):
  service = mock.Mock()
  service.search = _FakeSearch(state)
  client = mock.Mock()
  client.get_service.return_value = service
  return client, service


# -----------------------------------------------------------------------------
# Drift guards — one test per refusal condition
# -----------------------------------------------------------------------------


def test_drift_guard_passes_on_the_approved_state():
  client, _ = _client(_state())
  gm.assert_no_drift(client, CID, _spec())  # must not raise


def test_refuses_when_master_not_enabled():
  s = _state()
  s["campaigns"][CAMP_MASTER]["status"] = "PAUSED"
  client, _ = _client(s)
  with pytest.raises(ToolError, match="Master campaign status is PAUSED"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_master_budget_moved():
  s = _state()
  s["budgets"][BUDGET_MASTER]["amount"] = 140_000_000
  client, _ = _client(s)
  with pytest.raises(ToolError, match="approval recorded BEFORE"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_master_tcpa_moved():
  s = _state()
  s["campaigns"][CAMP_MASTER]["tcpa"] = 60_000_000
  client, _ = _client(s)
  with pytest.raises(ToolError, match="Master tCPA"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_master_bidding_strategy_changed():
  s = _state()
  s["campaigns"][CAMP_MASTER]["strategy"] = "MANUAL_CPC"
  client, _ = _client(s)
  with pytest.raises(ToolError, match="bidding strategy"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_source_ad_group_not_enabled():
  s = _state()
  s["ad_groups"][AG_SOURCE]["status"] = "PAUSED"
  client, _ = _client(s)
  # PAUSED equals the target state, so this is idempotent, not drift.
  gm.assert_no_drift(client, CID, _spec())
  s["ad_groups"][AG_SOURCE]["status"] = "REMOVED"
  client, _ = _client(s)
  with pytest.raises(ToolError, match="approval recorded BEFORE"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_a_brand_cpc_differs_from_recorded_before():
  s = _state()
  s["ad_groups"][AG_CORE]["cpc"] = 7_000_000
  client, _ = _client(s)
  with pytest.raises(ToolError, match="CPC is .*approval recorded BEFORE"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_brand_campaign_not_paused():
  s = _state()
  s["campaigns"][CAMP_BRAND]["status"] = "REMOVED"
  client, _ = _client(s)
  with pytest.raises(ToolError, match="approval recorded BEFORE PAUSED"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_brand_budget_amount_wrong():
  s = _state()
  s["budgets"][BUDGET_BRAND]["amount"] = 50_000_000
  client, _ = _client(s)
  with pytest.raises(ToolError, match="expected \\$45.00"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_brand_budget_reference_count_not_one():
  s = _state()
  s["budgets"][BUDGET_BRAND]["refs"] = 2
  client, _ = _client(s)
  with pytest.raises(ToolError, match="reference_count is 2"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_unexpected_language_already_configured():
  s = _state()
  s["languages"] = ["languageConstants/1003"]
  client, _ = _client(s)
  with pytest.raises(ToolError, match="already carries language criteria"):
    gm.assert_no_drift(client, CID, _spec())


def test_accepts_when_intended_language_already_present():
  s = _state()
  s["languages"] = ["languageConstants/1000"]
  client, _ = _client(s)
  gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_closter_keyword_disappeared():
  s = _state()
  s["criteria"][CRIT_A] = None
  client, _ = _client(s)
  with pytest.raises(ToolError, match="no longer exists"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_closter_keyword_identity_changed():
  s = _state()
  s["criteria"][CRIT_A]["text"] = "something else entirely"
  client, _ = _client(s)
  with pytest.raises(ToolError, match="Resource identity changed"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_submit_form_no_longer_primary():
  s = _state()
  s["actions"]["7521040525"]["primary"] = False
  client, _ = _client(s)
  with pytest.raises(ToolError, match="no longer Primary"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_calls_from_ads_no_longer_primary():
  s = _state()
  s["actions"]["7385858877"]["primary"] = False
  client, _ = _client(s)
  with pytest.raises(ToolError, match="no longer Primary"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_a_custom_conversion_goal_appears():
  s = _state()
  s["custom_goals"] = 1
  client, _ = _client(s)
  with pytest.raises(ToolError, match="custom conversion goal"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_selective_optimization_appears():
  s = _state()
  s["campaigns"][CAMP_MASTER]["selective"] = ["a"]
  client, _ = _client(s)
  with pytest.raises(ToolError, match="selective optimization"):
    gm.assert_no_drift(client, CID, _spec())


def test_refuses_when_rule4_conflicts_exist():
  client, _ = _client(_state())
  with mock.patch.object(
      gm,
      "rule4_conflicts",
      return_value=[
          {"ad_group": "Brand - Core", "negative": "montvale", "positive": "x"}
      ],
  ):
    with pytest.raises(ToolError, match="Rule 4 conflict check"):
      gm.assert_no_drift(client, CID, _spec())


def test_drift_refusal_lists_every_problem_at_once():
  s = _state()
  s["campaigns"][CAMP_MASTER]["status"] = "PAUSED"
  s["budgets"][BUDGET_BRAND]["refs"] = 3
  client, _ = _client(s)
  with pytest.raises(ToolError) as excinfo:
    gm.assert_no_drift(client, CID, _spec())
  message = str(excinfo.value)
  assert "Master campaign status" in message
  assert "reference_count is 3" in message
  assert "Nothing was mutated" in message


# -----------------------------------------------------------------------------
# Executor
# -----------------------------------------------------------------------------


def test_executor_sends_one_atomic_request_with_partial_failure_false():
  client, service = _client(_state())
  service.mutate = mock.Mock(
      return_value=types.SimpleNamespace(
          mutate_operation_responses=[object()] * 6
      )
  )
  with mock.patch.object(gm, "_verify_after", return_value={"ok": True}):
    result = gm._execute_campaign_migration(client, CID, _spec())

  assert service.mutate.call_count == 2, "expected validate_only then real"
  dry = service.mutate.call_args_list[0].kwargs["request"]
  real = service.mutate.call_args_list[1].kwargs["request"]
  assert dry.validate_only is True
  assert real.validate_only is False
  assert dry.partial_failure is False
  assert real.partial_failure is False
  assert len(real.mutate_operations) == 6
  assert result["outcome"] == "applied"
  assert result["operations_sent"] == 6


def test_executor_refuses_before_mutating_when_state_drifted():
  s = _state()
  s["campaigns"][CAMP_MASTER]["tcpa"] = 50_000_000
  client, service = _client(s)
  service.mutate = mock.Mock()
  with pytest.raises(ToolError, match="MIGRATION REFUSED"):
    gm._execute_campaign_migration(client, CID, _spec())
  service.mutate.assert_not_called()


def test_executor_is_a_no_op_when_everything_is_already_applied():
  s = _state()
  s["ad_groups"][AG_CORE]["cpc"] = 6_500_000
  s["ad_groups"][AG_SOURCE]["status"] = "PAUSED"
  s["budgets"][BUDGET_MASTER]["amount"] = 115_000_000
  s["campaigns"][CAMP_BRAND]["status"] = "ENABLED"
  s["criteria"][CRIT_A]["status"] = "PAUSED"
  s["languages"] = ["languageConstants/1000"]
  client, service = _client(s)
  service.mutate = mock.Mock()
  spec = _spec()
  spec["ad_group_cpc_updates"] = []
  spec["budget_updates"] = []
  spec["ad_group_status_updates"] = []
  spec["campaign_status_updates"] = []
  spec["keyword_pauses"] = []
  result = gm._execute_campaign_migration(client, CID, spec)
  assert result["outcome"] == "no_op"
  service.mutate.assert_not_called()


def test_executor_raises_when_read_back_disagrees():
  client, service = _client(_state())
  service.mutate = mock.Mock(
      return_value=types.SimpleNamespace(mutate_operation_responses=[])
  )
  # State still reads the pre-migration values, so verification must fail.
  with pytest.raises(ToolError, match="VERIFICATION FAILED"):
    gm._execute_campaign_migration(client, CID, _spec())


def test_executor_is_registered_in_the_shared_gate():
  from ads_mcp.tools import mutations_gated

  assert gm.OP in mutations_gated._DISPATCH
