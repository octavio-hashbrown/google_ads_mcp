# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the gated call-asset attachment tools."""

import types
from unittest import mock

from ads_mcp.tools import gated_assets
from ads_mcp.tools import mutations_gated
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
import pytest


AD_GROUP = "customers/123/adGroups/456"
CAMPAIGN = "customers/123/campaigns/789"
ASSET = "customers/123/assets/309260824477"
ACCOUNT_ACTION = "customers/123/conversionActions/7385858877"
ASSET_ACTION = "customers/123/conversionActions/179"

assess = gated_assets.assess_reuse_compatibility
ACCOUNT_LEVEL = "USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION"
RESOURCE_LEVEL = "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"


# ---------------------------------------------------------------------------
# Row / config builders
# ---------------------------------------------------------------------------


def _schedule(day="MONDAY", start=9, end=17):
  return types.SimpleNamespace(
      day_of_week=types.SimpleNamespace(name=day),
      start_hour=start,
      start_minute=types.SimpleNamespace(name="ZERO"),
      end_hour=end,
      end_minute=types.SimpleNamespace(name="ZERO"),
  )


def _asset_row(
    resource_name=ASSET,
    phone="2017466577",
    country="US",
    reporting_state=ACCOUNT_LEVEL,
    conversion_action="",
    schedules=(),
):
  return types.SimpleNamespace(
      asset=types.SimpleNamespace(
          resource_name=resource_name,
          name="",
          call_asset=types.SimpleNamespace(
              phone_number=phone,
              country_code=country,
              call_conversion_reporting_state=types.SimpleNamespace(
                  name=reporting_state
              ),
              call_conversion_action=conversion_action,
              ad_schedule_targets=list(schedules),
          ),
      )
  )


def _asset_config(**overrides):
  base = {
      "resource_name": ASSET,
      "name": "",
      "phone_number": "2017466577",
      "country_code": "US",
      "call_conversion_reporting_state": ACCOUNT_LEVEL,
      "call_conversion_action": "",
      "ad_schedule_targets": [],
  }
  base.update(overrides)
  return base


def _action(
    resource_name=ACCOUNT_ACTION,
    name="Calls from ads",
    type_="AD_CALL",
    status="ENABLED",
):
  return {
      "resource_name": resource_name,
      "id": 7385858877,
      "name": name,
      "type": type_,
      "category": "PHONE_CALL_LEAD",
      "status": status,
      "primary_for_goal": True,
      "include_in_conversions_metric": True,
      "counting_type": "ONE_PER_CLICK",
      "phone_call_duration_seconds": 30,
  }


def _action_row(
    resource_name=ACCOUNT_ACTION,
    name="Calls from ads",
    type_="AD_CALL",
    status="ENABLED",
):
  return types.SimpleNamespace(
      conversion_action=types.SimpleNamespace(
          resource_name=resource_name,
          id=7385858877,
          name=name,
          type_=types.SimpleNamespace(name=type_),
          category=types.SimpleNamespace(name="PHONE_CALL_LEAD"),
          status=types.SimpleNamespace(name=status),
          primary_for_goal=True,
          include_in_conversions_metric=True,
          counting_type=types.SimpleNamespace(name="ONE_PER_CLICK"),
          phone_call_duration_seconds=30,
      )
  )


def _settings_row(
    reporting=True, conversion_reporting=True, action=ACCOUNT_ACTION
):
  return types.SimpleNamespace(
      customer=types.SimpleNamespace(
          id=123,
          call_reporting_setting=types.SimpleNamespace(
              call_reporting_enabled=reporting,
              call_conversion_reporting_enabled=conversion_reporting,
              call_conversion_action=action,
          ),
      )
  )


def _link_row(
    resource_name="customers/123/campaignAssets/789~309260824477~CALL",
    asset=ASSET,
    phone="2017466577",
    country="US",
    status="ENABLED",
    level="campaign",
):
  link = types.SimpleNamespace(
      resource_name=resource_name,
      asset=asset,
      status=types.SimpleNamespace(name=status),
  )
  row = types.SimpleNamespace(
      asset=types.SimpleNamespace(
          call_asset=types.SimpleNamespace(
              phone_number=phone, country_code=country
          )
      )
  )
  setattr(row, f"{level}_asset", link)
  return row


def _client(search_side_effect):
  """Wires a client whose get_service dispatches by service name."""
  google_ads = mock.Mock()
  google_ads.search.side_effect = search_side_effect
  services = {
      "GoogleAdsService": google_ads,
      "AssetService": mock.Mock(),
      "AdGroupAssetService": mock.Mock(),
      "CampaignAssetService": mock.Mock(),
  }
  client = mock.Mock()
  client.get_service.side_effect = lambda n: services[n]
  return client, services


def _mutate_result(resource_name):
  response = mock.Mock()
  response.results = [mock.Mock(resource_name=resource_name)]
  return response


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_both_ops_registered_in_shared_gate():
  assert "attach_call_asset_to_ad_group" in mutations_gated._DISPATCH
  assert "attach_call_asset_to_campaign" in mutations_gated._DISPATCH


# ---------------------------------------------------------------------------
# Phone matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("2017466577", "(201) 746-6577", True),
        ("2017466577", "+1 201-746-6577", True),
        ("+12017466577", "2017466577", True),
        ("2017466577", "2017466578", False),
        ("2017466577", "", False),
        ("12345", "12345", False),
    ],
)
def test_phone_matching_ignores_formatting(a, b, expected):
  assert gated_assets._same_number(a, b) is expected


# ---------------------------------------------------------------------------
# assess_reuse_compatibility — account-level vs resource-level semantics
# ---------------------------------------------------------------------------


def test_default_configuration_is_reusable():
  blocking, info = assess(_asset_config())
  assert blocking == []
  assert info == []


def test_account_level_populated_action_is_informational_not_blocking():
  """Verified live: the field is inert under account-level reporting."""
  blocking, info = assess(
      _asset_config(call_conversion_action=ASSET_ACTION)
  )
  assert blocking == []
  assert len(info) == 1
  assert "NOT operative" in info[0]
  assert ASSET_ACTION in info[0]


def test_disabled_reporting_blocks_reuse():
  blocking, _ = assess(
      _asset_config(call_conversion_reporting_state="DISABLED")
  )
  assert any("DISABLED" in b for b in blocking)


def test_resource_level_unresolvable_action_blocks_reuse():
  """Under resource-level the action IS operative, so it must resolve."""
  blocking, _ = assess(
      _asset_config(
          call_conversion_reporting_state=RESOURCE_LEVEL,
          call_conversion_action=ASSET_ACTION,
      ),
      asset_action=None,
  )
  assert any("does not resolve" in b for b in blocking)
  assert any("IS operative" in b for b in blocking)


def test_resource_level_missing_action_blocks_reuse():
  blocking, _ = assess(
      _asset_config(call_conversion_reporting_state=RESOURCE_LEVEL)
  )
  assert any("names no conversion action" in b for b in blocking)


def test_resource_level_disabled_action_blocks_reuse():
  blocking, _ = assess(
      _asset_config(
          call_conversion_reporting_state=RESOURCE_LEVEL,
          call_conversion_action=ASSET_ACTION,
      ),
      asset_action=_action(status="REMOVED"),
  )
  assert any("REMOVED, not ENABLED" in b for b in blocking)


def test_resource_level_wrong_type_blocks_reuse():
  blocking, _ = assess(
      _asset_config(
          call_conversion_reporting_state=RESOURCE_LEVEL,
          call_conversion_action=ASSET_ACTION,
      ),
      asset_action=_action(type_="WEBPAGE"),
  )
  assert any("not AD_CALL" in b for b in blocking)


def test_resource_level_valid_action_is_allowed():
  blocking, info = assess(
      _asset_config(
          call_conversion_reporting_state=RESOURCE_LEVEL,
          call_conversion_action=ASSET_ACTION,
      ),
      asset_action=_action(resource_name=ASSET_ACTION),
  )
  assert blocking == []
  assert any("RESOURCE-LEVEL" in i and "verified ENABLED" in i for i in info)


def test_ad_schedule_blocks_reuse():
  blocking, _ = assess(
      _asset_config(ad_schedule_targets=[{"day_of_week": "MONDAY"}])
  )
  assert any("ad schedule" in b for b in blocking)


def test_intended_state_mismatch_blocks_reuse():
  blocking, _ = assess(_asset_config(), RESOURCE_LEVEL)
  assert any("does not" in b and "match" in b for b in blocking)


# ---------------------------------------------------------------------------
# verify_account_call_reporting
# ---------------------------------------------------------------------------


def test_account_reporting_healthy():
  client, _ = _client([[_settings_row()], [_action_row()]])
  settings, issues = gated_assets.verify_account_call_reporting(
      client, "123", ACCOUNT_ACTION
  )
  assert issues == []
  assert settings["resolved_action"]["name"] == "Calls from ads"
  assert settings["resolved_action"]["phone_call_duration_seconds"] == 30


def test_account_call_reporting_disabled_is_flagged():
  client, _ = _client([[_settings_row(reporting=False)], [_action_row()]])
  _, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert any("call reporting is DISABLED" in i for i in issues)


def test_account_conversion_reporting_disabled_is_flagged():
  client, _ = _client(
      [[_settings_row(conversion_reporting=False)], [_action_row()]]
  )
  _, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert any("CONVERSION reporting is DISABLED" in i for i in issues)


def test_account_action_drift_is_flagged():
  client, _ = _client([
      [_settings_row(action="customers/123/conversionActions/999")],
      [_action_row(resource_name="customers/123/conversionActions/999")],
  ])
  _, issues = gated_assets.verify_account_call_reporting(
      client, "123", ACCOUNT_ACTION
  )
  assert any("changed" in i for i in issues)


# ---------------------------------------------------------------------------
# Unresolvable account-level reference.
#
# This is the ONLY account-level fault that may be downgraded, and only on
# positive, call-specific evidence. Everything else stays fail-closed.
# ---------------------------------------------------------------------------


OTHER_ACTION = "customers/123/conversionActions/6734917828"


def _ad_call_action_row(resource_name=ACCOUNT_ACTION, name="Calls from ads"):
  """A row from the ENABLED AD_CALL / CALL_FROM_ADS lookup."""
  return types.SimpleNamespace(
      conversion_action=types.SimpleNamespace(
          resource_name=resource_name, name=name
      )
  )


def _asset_conv_row(
    asset=ASSET,
    reporting_state=ACCOUNT_LEVEL,
    action=ACCOUNT_ACTION,
    all_conversions=3.0,
    level="campaign_asset",
    link_status="ENABLED",
):
  """A CALL asset LINK carrying conversions segmented to an action."""
  row = types.SimpleNamespace(
      asset=types.SimpleNamespace(
          call_asset=types.SimpleNamespace(
              call_conversion_reporting_state=types.SimpleNamespace(
                  name=reporting_state
              )
          )
      ),
      segments=types.SimpleNamespace(conversion_action=action),
      metrics=types.SimpleNamespace(all_conversions=all_conversions),
  )
  setattr(
      row,
      level,
      types.SimpleNamespace(
          asset=asset, status=types.SimpleNamespace(name=link_status)
      ),
  )
  return row



@pytest.mark.parametrize("level", ["campaign_asset", "ad_group_asset"])
@pytest.mark.parametrize("dead_status", ["REMOVED", "PAUSED"])
def test_non_enabled_call_link_with_history_does_not_corroborate(
    level, dead_status
):
  """A detached or paused link must never prove the path is live TODAY.

  Verified live 2026-08-26 on 784-991-4897: a campaign_asset /
  ad_group_asset query with only a field_type predicate returns REMOVED
  links alongside ENABLED ones, and metrics still report their history.
  Without an explicit status gate, a CALL asset detached yesterday would
  satisfy corroboration on last-30-days conversions it earned while it
  was still attached.
  """
  dead = [_asset_conv_row(level=level, link_status=dead_status)]
  # The corroboration loop queries campaign links before ad-group
  # links, so the dead row has to land on its own level's query.
  links = [dead, []] if level == "campaign_asset" else [[], dead]
  client, _ = _client([
      [_settings_row()],
      [],  # account-level reference does not resolve
      [_ad_call_action_row()],
      *links,
  ])
  settings, issues = gated_assets.verify_account_call_reporting(client, "123")

  assert issues, f"a {dead_status} link must not corroborate"
  assert "The account-level path is therefore unproven." in issues[0]
  assert "unenumerable_account_reference" not in settings


def test_corroboration_query_gates_link_status_in_gaql():
  """The status gate must live in the GAQL, not only in Python.

  Filtering in Python alone would still pull every removed link over the
  wire and rely on the loop to discard them. Pin the predicate so a
  future edit cannot quietly drop it.
  """
  client, services = _client([
      [_settings_row()],
      [],
      [_ad_call_action_row()],
      [],
      [],
  ])
  gated_assets.verify_account_call_reporting(client, "123")

  google_ads = services["GoogleAdsService"]
  link_queries = [
      c.kwargs["query"]
      for c in google_ads.search.call_args_list
      if "field_type = 'CALL'" in c.kwargs["query"]
  ]
  assert len(link_queries) == 2, "campaign and ad-group links are both checked"
  for query in link_queries:
    assert "status = 'ENABLED'" in query


def test_enabled_link_still_corroborates_after_the_status_gate():
  """Control: the gate must not break the legitimate Cop Call NY shape."""
  client, _ = _client([
      [_settings_row()],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row(link_status="ENABLED")],
  ])
  settings, issues = gated_assets.verify_account_call_reporting(client, "123")

  assert not issues
  assert settings["unenumerable_account_reference"] == ACCOUNT_ACTION

def test_unresolvable_action_with_account_level_asset_conversions_warns():
  """Cop Call NY shape: the ACCOUNT-LEVEL path itself is proven live."""
  client, _ = _client([
      [_settings_row()],  # account settings healthy
      [],  # account-level reference does not resolve
      [_ad_call_action_row()],  # an ENABLED AD_CALL action exists
      [_asset_conv_row()],  # and an ACCOUNT-LEVEL asset carries its conversions
  ])
  settings, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert issues == []
  assert settings["unenumerable_account_reference"] == ACCOUNT_ACTION
  assert any(
      "ACCEPTED UNENUMERABLE ACCOUNT-LEVEL REFERENCE" in w
      for w in settings["warnings"]
  )
  assert any("UNKNOWN" in w for w in settings["warnings"])
  assert any("USE_ACCOUNT_LEVEL" in w for w in settings["warnings"])


def test_unrelated_converting_ad_call_action_does_not_corroborate():
  """The reviewer's explicit failure case.

  A DIFFERENT healthy AD_CALL action is converting, but nothing ties those
  conversions to any asset using the account-level call-conversion setting.
  "Some call action works" must NOT equal "the account-level reference is
  corroborated".
  """
  client, _ = _client([
      [_settings_row()],
      [],
      [_ad_call_action_row(resource_name=OTHER_ACTION)],
      [],  # no campaign-level account-level asset carries them
      [],  # no ad-group-level one either
  ])
  settings, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert any("account-level path is therefore unproven" in i for i in issues)
  assert "warnings" not in settings
  assert "unenumerable_account_reference" not in settings


def test_resource_level_asset_conversions_do_not_corroborate():
  """An asset that counts calls via its OWN action proves nothing here."""
  client, _ = _client([
      [_settings_row()],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row(reporting_state=RESOURCE_LEVEL)],
      [],
  ])
  _, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert any("account-level path is therefore unproven" in i for i in issues)


def test_zero_conversion_account_level_asset_does_not_corroborate():
  client, _ = _client([
      [_settings_row()],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row(all_conversions=0.0)],
      [],
  ])
  _, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert any("account-level path is therefore unproven" in i for i in issues)


def test_no_enabled_ad_call_actions_blocks():
  client, _ = _client([[_settings_row()], [], []])
  _, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert any("account-level path is therefore unproven" in i for i in issues)


def test_ad_group_level_asset_can_corroborate():
  """Corroboration walks ad-group links too, not only campaign links."""
  client, _ = _client([
      [_settings_row()],
      [],
      [_ad_call_action_row()],
      [],  # nothing at campaign level
      [_asset_conv_row(level="ad_group_asset")],
  ])
  settings, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert issues == []
  assert settings["unenumerable_account_reference"] == ACCOUNT_ACTION


def test_reporting_disabled_is_never_bypassed_by_corroboration():
  client, _ = _client([
      [_settings_row(reporting=False)],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row()],
  ])
  settings, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert any("call reporting is DISABLED" in i for i in issues)
  assert "warnings" not in settings


def test_conversion_reporting_disabled_is_never_bypassed_by_corroboration():
  client, _ = _client([
      [_settings_row(conversion_reporting=False)],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row()],
  ])
  settings, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert any("CONVERSION reporting is DISABLED" in i for i in issues)
  assert "warnings" not in settings


def test_action_drift_is_never_bypassed_by_corroboration():
  client, _ = _client([
      [_settings_row(action="customers/123/conversionActions/999")],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row()],
  ])
  settings, issues = gated_assets.verify_account_call_reporting(
      client, "123", ACCOUNT_ACTION
  )
  assert any("changed" in i for i in issues)
  assert "warnings" not in settings


def test_corroboration_query_is_pinned_to_call_links_and_ad_call_actions():
  """Guards the query itself so it can never be widened silently."""
  client, services = _client([
      [_settings_row()],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row()],
  ])
  gated_assets.verify_account_call_reporting(client, "123")
  queries = [
      c.kwargs["query"]
      for c in services["GoogleAdsService"].search.call_args_list
  ]
  action_query = queries[2]
  assert "conversion_action.status = 'ENABLED'" in action_query
  assert "conversion_action.type = 'AD_CALL'" in action_query
  assert "conversion_action.origin = 'CALL_FROM_ADS'" in action_query
  link_query = queries[3]
  assert "FROM campaign_asset" in link_query
  assert "campaign_asset.field_type = 'CALL'" in link_query
  assert "asset.call_asset.call_conversion_reporting_state" in link_query
  assert "segments.conversion_action IN" in link_query
  assert "LAST_30_DAYS" in link_query


# --- APPLY gate: corroboration alone is not enough ------------------------


def _corroborated_client():
  return _client([
      [_settings_row()],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row()],
  ])[0]


def test_apply_blocks_when_no_exception_was_recorded():
  _, issues = gated_assets.verify_account_call_reporting(
      _corroborated_client(),
      "123",
      require_recorded_exception=True,
  )
  assert any("does not carry a matching" in i for i in issues)


def test_apply_blocks_when_recorded_exception_is_for_another_reference():
  _, issues = gated_assets.verify_account_call_reporting(
      _corroborated_client(),
      "123",
      accepted_unenumerable_account_reference="customers/123/conversionActions/999",
      require_recorded_exception=True,
  )
  assert any("does not carry a matching" in i for i in issues)


def test_apply_allows_when_matching_exception_was_recorded():
  settings, issues = gated_assets.verify_account_call_reporting(
      _corroborated_client(),
      "123",
      accepted_unenumerable_account_reference=ACCOUNT_ACTION,
      require_recorded_exception=True,
  )
  assert issues == []
  assert settings["unenumerable_account_reference"] == ACCOUNT_ACTION


def test_account_action_not_ad_call_is_flagged():
  client, _ = _client([[_settings_row()], [_action_row(type_="WEBPAGE")]])
  _, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert any("not AD_CALL" in i for i in issues)


def test_account_with_no_action_is_flagged():
  client, _ = _client([[_settings_row(action="")]])
  _, issues = gated_assets.verify_account_call_reporting(client, "123")
  assert any("No account-level call conversion action" in i for i in issues)


# ---------------------------------------------------------------------------
# Propose — campaign level
# ---------------------------------------------------------------------------


@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_campaign")
def test_campaign_propose_reuses_and_reports_account_state(
    mock_lookup, mock_root, mock_get_client, tmp_path
):
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Growth-Specialty"
  client, _ = _client([
      [],                                                    # no links
      [_asset_row(conversion_action=ASSET_ACTION)],          # asset exists
      [_settings_row()],                                     # account settings
      [_action_row()],                                       # account action
  ])
  mock_get_client.return_value = client

  result = gated_assets.propose_attach_call_asset_to_campaign(
      customer_id="123",
      campaign_resource_name=CAMPAIGN,
      phone_number="(201) 746-6577",
      country_code="US",
      reason_code="client_request",
      expected_account_call_conversion_action=ACCOUNT_ACTION,
  )
  block = result["block"]
  assert "Reuse existing call asset" in block
  assert "NOT modified" in block
  assert "NOTE:" in block and "NOT operative" in block
  assert "Calls from ads" in block and "30s threshold" in block
  assert "field_type CALL" in block
  # The account action is pinned into the hash-verified spec.
  assert ACCOUNT_ACTION in block


@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_campaign")
def test_campaign_propose_refuses_when_no_existing_asset(
    mock_lookup, mock_root, mock_get_client, tmp_path
):
  """Campaign level is reuse-only and must never mint a duplicate number."""
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Growth-Specialty"
  client, _ = _client([[], []])  # no links, no matching asset
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gated_assets.propose_attach_call_asset_to_campaign(
        customer_id="123",
        campaign_resource_name=CAMPAIGN,
        phone_number="2015550000",
        country_code="US",
        reason_code="client_request",
    )
  assert "reuse-only" in str(exc.value)
  assert not list(tmp_path.glob("pending_approvals/*.md"))


@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_campaign")
def test_campaign_propose_refuses_duplicate_link(
    mock_lookup, mock_root, mock_get_client, tmp_path
):
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Master"
  client, _ = _client([[_link_row(phone="(201) 746-6577")]])
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gated_assets.propose_attach_call_asset_to_campaign(
        customer_id="123",
        campaign_resource_name=CAMPAIGN,
        phone_number="2017466577",
        country_code="US",
        reason_code="client_request",
    )
  assert "already has" in str(exc.value)


@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_campaign")
def test_campaign_propose_surfaces_removed_link(
    mock_lookup, mock_root, mock_get_client, tmp_path
):
  """A REMOVED link is not a duplicate, but the human should see it."""
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Montvale"
  client, _ = _client([
      [_link_row(status="REMOVED")],
      [_asset_row()],
      [_settings_row()],
      [_action_row()],
  ])
  mock_get_client.return_value = client

  result = gated_assets.propose_attach_call_asset_to_campaign(
      customer_id="123",
      campaign_resource_name=CAMPAIGN,
      phone_number="2017466577",
      country_code="US",
      reason_code="client_request",
  )
  assert "previously removed association" in result["block"]


@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_campaign")
def test_propose_refuses_when_account_reporting_broken(
    mock_lookup, mock_root, mock_get_client, tmp_path
):
  """Account attribution is not overridable by a reuse flag."""
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Growth-Specialty"
  client, _ = _client([
      [],
      [_asset_row()],
      [_settings_row(conversion_reporting=False)],
      [_action_row()],
  ])
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gated_assets.propose_attach_call_asset_to_campaign(
        customer_id="123",
        campaign_resource_name=CAMPAIGN,
        phone_number="2017466577",
        country_code="US",
        reason_code="client_request",
        reuse_incompatible_asset=True,   # must NOT override this
    )
  message = str(exc.value)
  assert "Account-level call reporting is not in the state" in message
  assert "not overridable" in message
  assert not list(tmp_path.glob("pending_approvals/*.md"))


# ---------------------------------------------------------------------------
# Propose — ad group level
# ---------------------------------------------------------------------------


@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_ad_group")
def test_ad_group_propose_reuses_existing_asset(
    mock_lookup, mock_root, mock_get_client, tmp_path
):
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Brand - Montvale"
  client, services = _client([
      [],
      [_asset_row(conversion_action=ASSET_ACTION)],
      [_settings_row()],
      [_action_row()],
  ])
  mock_get_client.return_value = client

  result = gated_assets.propose_attach_call_asset_to_ad_group(
      customer_id="123",
      ad_group_resource_name=AD_GROUP,
      phone_number="2017466577",
      country_code="US",
      reason_code="client_request",
      reuse_existing_asset_only=True,
      expected_account_call_conversion_action=ACCOUNT_ACTION,
  )
  assert "Reuse existing call asset" in result["block"]
  assert 'Ad group "Brand - Montvale"' in result["block"]
  services["AssetService"].mutate_assets.assert_not_called()


@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_ad_group")
def test_ad_group_propose_reuse_only_refuses_creation(
    mock_lookup, mock_root, mock_get_client, tmp_path
):
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Brand - Core"
  client, _ = _client([[], []])
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gated_assets.propose_attach_call_asset_to_ad_group(
        customer_id="123",
        ad_group_resource_name=AD_GROUP,
        phone_number="2015550000",
        country_code="US",
        reason_code="client_request",
        reuse_existing_asset_only=True,
    )
  assert "reuse-only" in str(exc.value)


@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_ad_group")
def test_ad_group_propose_can_create_when_allowed(
    mock_lookup, mock_root, mock_get_client, tmp_path
):
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Brand - Core"
  client, _ = _client([[], []])
  mock_get_client.return_value = client

  result = gated_assets.propose_attach_call_asset_to_ad_group(
      customer_id="123",
      ad_group_resource_name=AD_GROUP,
      phone_number="2015550000",
      country_code="US",
      reason_code="client_request",
  )
  assert "Create a new call asset" in result["block"]


@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_ad_group")
def test_propose_stops_on_ad_schedule(
    mock_lookup, mock_root, mock_get_client, tmp_path
):
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Brand - Montvale"
  client, _ = _client([
      [],
      [_asset_row(schedules=[_schedule()])],
      [_settings_row()],
      [_action_row()],
  ])
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gated_assets.propose_attach_call_asset_to_ad_group(
        customer_id="123",
        ad_group_resource_name=AD_GROUP,
        phone_number="2017466577",
        country_code="US",
        reason_code="client_request",
    )
  assert "STOPPING FOR APPROVAL" in str(exc.value)
  assert "ad schedule" in str(exc.value)


@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_ad_group")
def test_propose_rejects_bad_input(
    mock_lookup, mock_root, mock_get_client, tmp_path
):
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Brand - Core"
  client, _ = _client([[], []] * 4)
  mock_get_client.return_value = client

  with pytest.raises(ToolError):
    gated_assets.propose_attach_call_asset_to_ad_group(
        customer_id="123", ad_group_resource_name=AD_GROUP,
        phone_number="no digits", country_code="US",
        reason_code="client_request",
    )
  with pytest.raises(ToolError):
    gated_assets.propose_attach_call_asset_to_ad_group(
        customer_id="123", ad_group_resource_name=AD_GROUP,
        phone_number="2015551234", country_code="USA",
        reason_code="client_request",
    )


# ---------------------------------------------------------------------------
# Apply — campaign level
# ---------------------------------------------------------------------------


def _campaign_spec(**overrides):
  spec = {
      "campaign_resource_name": CAMPAIGN,
      "phone_number": "2017466577",
      "country_code": "US",
      "reuse_asset_resource_name": ASSET,
      "reuse_existing_asset_only": True,
      "reuse_incompatible_asset": False,
      "accepted_incompatibilities": [],
      "expected_account_call_conversion_action": ACCOUNT_ACTION,
  }
  spec.update(overrides)
  return spec


def test_campaign_apply_no_op_when_already_linked():
  client, services = _client([[_link_row()]])
  result = gated_assets._execute_attach_call_asset_to_campaign(
      client, "123", _campaign_spec()
  )
  assert result["outcome"] == "no_op"
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()
  services["AssetService"].mutate_assets.assert_not_called()


def test_campaign_apply_links_and_verifies():
  link = "customers/123/campaignAssets/789~309260824477~CALL"
  client, services = _client([
      [],                                        # no existing link
      [_asset_row(conversion_action=ASSET_ACTION)],  # asset exists
      [_settings_row()],                         # account settings
      [_action_row()],                           # account action
      [_link_row(resource_name=link)],           # verification
  ])
  services["CampaignAssetService"].mutate_campaign_assets.return_value = (
      _mutate_result(link)
  )

  result = gated_assets._execute_attach_call_asset_to_campaign(
      client, "123", _campaign_spec()
  )

  assert result["outcome"] == "applied"
  assert result["level"] == "campaign"
  assert result["campaign_asset"] == link
  assert result["asset"] == ASSET
  assert result["asset_created"] is False
  assert result["asset_reused"] is True
  assert result["verified_by"] == "GAQL read-back of campaign_asset"
  # The shared asset was never written to.
  services["AssetService"].mutate_assets.assert_not_called()

  operation = services[
      "CampaignAssetService"
  ].mutate_campaign_assets.call_args.kwargs["operations"][0]
  assert operation.create.campaign == CAMPAIGN
  assert operation.create.asset == ASSET
  assert operation.create.field_type.name == "CALL"


def test_campaign_apply_refuses_when_asset_gone():
  client, services = _client([[], []])
  with pytest.raises(ToolError) as exc:
    gated_assets._execute_attach_call_asset_to_campaign(
        client, "123", _campaign_spec()
    )
  assert "reuse-only" in str(exc.value)
  services["AssetService"].mutate_assets.assert_not_called()


def test_campaign_apply_refuses_on_account_drift():
  client, services = _client([
      [],
      [_asset_row()],
      [_settings_row(action="customers/123/conversionActions/999")],
      [_action_row(resource_name="customers/123/conversionActions/999")],
  ])
  with pytest.raises(ToolError) as exc:
    gated_assets._execute_attach_call_asset_to_campaign(
        client, "123", _campaign_spec()
    )
  assert "drifted since approval" in str(exc.value)
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_campaign_apply_refuses_on_asset_config_drift():
  client, services = _client([
      [],
      [_asset_row(reporting_state="DISABLED")],
  ])
  with pytest.raises(ToolError) as exc:
    gated_assets._execute_attach_call_asset_to_campaign(
        client, "123", _campaign_spec()
    )
  assert "no longer matches what was approved" in str(exc.value)
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_campaign_apply_fails_loudly_if_link_not_readable():
  link = "customers/123/campaignAssets/789~309260824477~CALL"
  client, services = _client([
      [], [_asset_row()], [_settings_row()], [_action_row()], [],
  ])
  services["CampaignAssetService"].mutate_campaign_assets.return_value = (
      _mutate_result(link)
  )
  with pytest.raises(ToolError) as exc:
    gated_assets._execute_attach_call_asset_to_campaign(
        client, "123", _campaign_spec()
    )
  assert "verification failed" in str(exc.value)


# ---------------------------------------------------------------------------
# Apply — ad group level
# ---------------------------------------------------------------------------


def _ad_group_spec(**overrides):
  spec = {
      "ad_group_resource_name": AD_GROUP,
      "phone_number": "2017466577",
      "country_code": "US",
      "reuse_asset_resource_name": ASSET,
      "reuse_existing_asset_only": True,
      "reuse_incompatible_asset": False,
      "accepted_incompatibilities": [],
      "expected_account_call_conversion_action": ACCOUNT_ACTION,
  }
  spec.update(overrides)
  return spec


def test_ad_group_apply_no_op_when_already_linked():
  client, services = _client([[_link_row(level="ad_group")]])
  result = gated_assets._execute_attach_call_asset(
      client, "123", _ad_group_spec()
  )
  assert result["outcome"] == "no_op"
  services["AdGroupAssetService"].mutate_ad_group_assets.assert_not_called()


def test_ad_group_apply_reuses_and_verifies():
  link = "customers/123/adGroupAssets/456~309260824477~CALL"
  client, services = _client([
      [],
      [_asset_row(conversion_action=ASSET_ACTION)],
      [_settings_row()],
      [_action_row()],
      [_link_row(resource_name=link, level="ad_group")],
  ])
  services["AdGroupAssetService"].mutate_ad_group_assets.return_value = (
      _mutate_result(link)
  )

  result = gated_assets._execute_attach_call_asset(
      client, "123", _ad_group_spec()
  )
  assert result["outcome"] == "applied"
  assert result["level"] == "ad_group"
  assert result["asset_reused"] is True
  services["AssetService"].mutate_assets.assert_not_called()


def test_existing_shared_assets_are_never_modified():
  """The only asset write anywhere in this module is a create."""
  operation = gated_assets.build_call_asset_operation("2015551234", "US")
  assert "update" not in str(operation)
  source = open(gated_assets.__file__, encoding="utf-8").read()
  assert "AssetOperation(update=" not in source
  assert "mutate_assets" in source  # create path exists


# ---------------------------------------------------------------------------
# APPLY-time revalidation (time-of-check / time-of-use).
#
# Every test below simulates: propose ran, corroboration held, the exception
# was recorded, a human approved -- and THEN the live state changed before
# apply. An approved exception is permission to tolerate a verified unusual
# state, never a substitute for verifying that state is still true.
# ---------------------------------------------------------------------------


NEW_REF = "customers/123/conversionActions/424242"


def _apply(client, accepted=ACCOUNT_ACTION, expected=None):
  return gated_assets.verify_account_call_reporting(
      client,
      "123",
      expected,
      accepted_unenumerable_account_reference=accepted,
      require_recorded_exception=True,
  )


def test_apply_fails_when_corroborating_link_has_disappeared():
  client, _ = _client([
      [_settings_row()],
      [],
      [_ad_call_action_row()],
      [],  # campaign link gone
      [],  # ad-group link gone
  ])
  settings, issues = _apply(client)
  assert any("account-level path is therefore unproven" in i for i in issues)
  assert "warnings" not in settings


def test_apply_fails_when_asset_left_account_level_reporting():
  client, _ = _client([
      [_settings_row()],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row(reporting_state=RESOURCE_LEVEL)],
      [],
  ])
  _, issues = _apply(client)
  assert any("account-level path is therefore unproven" in i for i in issues)


def test_apply_fails_when_corroborating_conversions_fell_to_zero():
  client, _ = _client([
      [_settings_row()],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row(all_conversions=0.0)],
      [],
  ])
  _, issues = _apply(client)
  assert any("account-level path is therefore unproven" in i for i in issues)


def test_apply_fails_when_qualifying_ad_call_action_is_gone():
  client, _ = _client([[_settings_row()], [], []])
  _, issues = _apply(client)
  assert any("account-level path is therefore unproven" in i for i in issues)


def test_apply_fails_when_configured_reference_changed_under_us():
  """Still unresolved, still corroborated -- but it is a DIFFERENT reference.

  The approval was for one specific unenumerable reference. It does not
  transfer to whatever the account now points at.
  """
  client, _ = _client([
      [_settings_row(action=NEW_REF)],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row()],
  ])
  settings, issues = _apply(client, accepted=ACCOUNT_ACTION)
  assert any("does not carry a matching" in i for i in issues)
  assert "warnings" not in settings


def test_apply_fails_when_reference_changed_and_expected_drift_also_fires():
  client, _ = _client([
      [_settings_row(action=NEW_REF)],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row()],
  ])
  _, issues = _apply(client, accepted=ACCOUNT_ACTION, expected=ACCOUNT_ACTION)
  assert any("changed" in i for i in issues)


def test_apply_still_succeeds_when_live_state_is_unchanged():
  """Control: the same approved exception passes while conditions hold."""
  client, _ = _client([
      [_settings_row()],
      [],
      [_ad_call_action_row()],
      [_asset_conv_row()],
  ])
  settings, issues = _apply(client)
  assert issues == []
  assert settings["unenumerable_account_reference"] == ACCOUNT_ACTION


def test_apply_path_revalidates_live_and_demands_the_recorded_exception():
  """The apply path must re-run the verifier, not trust the proposal."""
  spec = {
      "phone_number": "2017466577",
      "country_code": "US",
      "expected_account_call_conversion_action": ACCOUNT_ACTION,
      "accepted_unenumerable_account_reference": ACCOUNT_ACTION,
      "accepted_incompatibilities": [],
  }
  client, _ = _client([[_asset_row()]])
  with mock.patch.object(
      gated_assets, "verify_account_call_reporting", return_value=({}, [])
  ) as verifier:
    gated_assets._resolve_asset_for_apply(client, "123", spec)

  kwargs = verifier.call_args.kwargs
  assert kwargs["require_recorded_exception"] is True
  assert kwargs["accepted_unenumerable_account_reference"] == ACCOUNT_ACTION
# validate_only pre-flight on the asset services
#
# The asset paths previously committed with no dry run at all. Every one of
# them must now validate the EXACT operation server-side first, and must not
# commit if that validation fails.
# ---------------------------------------------------------------------------


ASSET_METHODS = [
    "mutate_assets",
    "mutate_campaign_assets",
    "mutate_ad_group_assets",
]


@pytest.mark.parametrize("method", ASSET_METHODS)
def test_preflight_validates_then_commits_the_same_operation(method):
  service = mock.Mock()
  operation = object()
  gated_assets._mutate_with_preflight(service, method, "123", operation)

  calls = getattr(service, method).call_args_list
  assert len(calls) == 2
  assert calls[0].kwargs["validate_only"] is True
  assert calls[1].kwargs["validate_only"] is False
  # The dry run must exercise the identical operation, or it proves nothing.
  assert calls[0].kwargs["operations"] == [operation]
  assert calls[1].kwargs["operations"] == [operation]
  assert calls[0].kwargs["customer_id"] == "123"


@pytest.mark.parametrize("method", ASSET_METHODS)
def test_failed_validation_never_commits_zero_live_delta(method):
  """Invalid resource/link must fail validation and write nothing."""
  service = mock.Mock()
  getattr(service, method).side_effect = GoogleAdsException(
      None, None, None, None
  )
  with mock.patch.object(
      gated_assets, "_handle_google_ads_error", side_effect=ToolError("invalid")
  ):
    with pytest.raises(ToolError):
      gated_assets._mutate_with_preflight(service, method, "123", object())

  calls = getattr(service, method).call_args_list
  assert len(calls) == 1, "the real mutate must never run after a failed dry run"
  assert calls[0].kwargs["validate_only"] is True


def test_create_call_asset_uses_preflight():
  service = mock.Mock()
  service.mutate_assets.return_value = _mutate_result(ASSET)
  client, _ = _client([])
  client.get_service.side_effect = lambda n: service
  assert gated_assets._create_call_asset(client, "123", "2017466577", "US") == ASSET
  assert service.mutate_assets.call_args_list[0].kwargs["validate_only"] is True


# ---------------------------------------------------------------------------
# Governed campaign-asset DETACH
# ---------------------------------------------------------------------------


LINK = "customers/123/campaignAssets/789~309260824477~CALL"


def test_campaign_asset_remove_operation_targets_the_link_only():
  operation = gated_assets.build_campaign_asset_remove_operation(LINK)
  assert operation.remove == LINK


def test_campaign_asset_remove_runs_through_preflight():
  service = mock.Mock()
  operation = gated_assets.build_campaign_asset_remove_operation(LINK)
  gated_assets._mutate_with_preflight(
      service, "mutate_campaign_assets", "123", operation
  )
  calls = service.mutate_campaign_assets.call_args_list
  assert len(calls) == 2
  assert calls[0].kwargs["validate_only"] is True
  assert calls[1].kwargs["validate_only"] is False
  assert calls[0].kwargs["operations"][0].remove == LINK


OTHER_LINK = "customers/123/campaignAssets/789~999~CALL"


STRAY_LINK = "customers/123/campaignAssets/789~444555666~CALL"
REPLACEMENT_ASSET = "customers/123/assets/914914914"


def _remaining(
    resource_name=OTHER_LINK,
    asset=ASSET,
    phone_number="9144404316",
    country_code="US",
):
  """One entry of the approved post-detach state."""
  return {
      "resource_name": resource_name,
      "asset": asset,
      "phone_number": phone_number,
      "country_code": country_code,
  }


def _detach_spec(
    link=LINK,
    phone="2017466577",
    asset=ASSET,
    remaining=None,
    country="US",
    target_status="ENABLED",
):
  return {
      "campaign_resource_name": CAMPAIGN,
      "campaign_asset_resource_name": link,
      "asset_resource_name": asset,
      "phone_number": phone,
      "country_code": country,
      "expected_target_status": target_status,
      "expected_remaining_serving_links": (
          [_remaining()] if remaining is None else remaining
      ),
  }


def test_detach_registered_in_shared_gate():
  assert "detach_call_asset_from_campaign" in mutations_gated._DISPATCH


def test_detach_executor_no_ops_when_link_already_gone():
  client, _ = _client([[]])
  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec()
  )
  assert out["outcome"] == "no_op"


def test_detach_executor_refuses_to_strand_the_campaign():
  """Never leave a campaign with zero call assets.

  Propose refuses to record an empty approved-remaining set, so the
  identity gate below subsumes the old non-empty check and is strictly
  stronger: stranding the campaign now shows up as the approved
  replacement having vanished.
  """
  client, _ = _client([[_link_row(resource_name=LINK)]])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec()
    )
  assert "no longer present on this campaign" in str(excinfo.value)


def test_detach_executor_removes_verifies_and_keeps_the_asset():
  before = [
      _link_row(resource_name=LINK),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  after = [_link_row(resource_name=OTHER_LINK, phone="9144404316")]
  client, services = _client([before, after])

  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec()
  )
  assert out["outcome"] == "applied"
  assert out["detached_campaign_asset"] == LINK
  assert out["asset_deleted"] is False
  assert out["remaining_serving_links"][0]["phone_number"] == "9144404316"

  calls = services[
      "CampaignAssetService"
  ].mutate_campaign_assets.call_args_list
  assert len(calls) == 2
  assert calls[0].kwargs["validate_only"] is True
  assert calls[1].kwargs["validate_only"] is False
  assert calls[0].kwargs["operations"][0].remove == LINK


def test_detach_executor_fails_when_link_still_reads_back():
  before = [
      _link_row(resource_name=LINK),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  client, _ = _client([before, before])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec()
    )
  assert "still reads back" in str(excinfo.value)


# --- detach idempotency / stale proposal ----------------------------------


def test_detach_no_ops_when_intended_link_absent_but_others_exist():
  """A different current state is NOT equivalent to the approved one."""
  client, services = _client([
      [_link_row(resource_name=OTHER_LINK, phone="9144404316")]
  ])
  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec(LINK)
  )
  assert out["outcome"] == "no_op"
  assert out["campaign_asset"] == LINK
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_detach_never_removes_a_different_link_carrying_the_same_number():
  """Stale proposal: the number is still linked, but via a NEW link id.

  Matching on the phone number here would silently detach a link nobody
  approved. The approved resource name is the only thing that counts.
  """
  recreated = "customers/123/campaignAssets/789~555~CALL"
  client, services = _client([
      [
          _link_row(resource_name=recreated, phone="2017466577"),
          _link_row(resource_name=OTHER_LINK, phone="9144404316"),
      ]
  ])
  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec(LINK)
  )
  assert out["outcome"] == "no_op"
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


# ---------------------------------------------------------------------------
# The approval covers the exact replacement state that was reviewed, not the
# much weaker claim that some phone number will still be attached.
# ---------------------------------------------------------------------------


def test_detach_hard_fails_when_approved_replacement_gone_but_stray_remains():
  """917 target, approved 914 replacement vanished, unrelated 201 present.

  The old gate saw a non-empty remaining list and would have detached 917
  against a state nobody approved.
  """
  before = [
      _link_row(resource_name=LINK, phone="9174404316"),
      _link_row(resource_name=STRAY_LINK, phone="2013452124"),
  ]
  client, services = _client([before])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )

  message = str(excinfo.value)
  assert "no longer present on this campaign" in message
  assert "9144404316" in message, "name the replacement that justified it"
  assert "Re-propose" in message
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_detach_hard_fails_when_replacement_reappears_under_a_new_id():
  """Same number, new resource ID, is still a different approved state."""
  before = [
      _link_row(resource_name=LINK, phone="9174404316"),
      _link_row(
          resource_name=STRAY_LINK,
          phone="9144404316",
          asset=REPLACEMENT_ASSET,
      ),
  ]
  client, services = _client([before])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )

  assert "no longer present on this campaign" in str(excinfo.value)
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_detach_proceeds_when_approved_replacement_is_still_exactly_active():
  """Control: the reviewed state is intact, so the detach may proceed."""
  before = [
      _link_row(resource_name=LINK, phone="9174404316"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  after = [_link_row(resource_name=OTHER_LINK, phone="9144404316")]
  client, services = _client([before, after])

  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec(phone="9174404316")
  )
  assert out["outcome"] == "applied"
  assert out["detached_campaign_asset"] == LINK
  assert out["approved_remaining_links_reverified"] == [OTHER_LINK]
  assert out["unapproved_additional_links_present_at_apply"] == []
  services["CampaignAssetService"].mutate_campaign_assets.assert_called()


def test_detach_no_ops_before_any_identity_check_when_target_already_gone():
  """917 already detached by someone else: no-op, never touch another link."""
  before = [
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
      _link_row(resource_name=STRAY_LINK, phone="2013452124"),
  ]
  client, services = _client([before])

  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec(phone="9174404316")
  )
  assert out["outcome"] == "no_op"
  assert out["campaign_asset"] == LINK
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_detach_allows_a_superset_but_surfaces_the_unapproved_link():
  """A number attached since approval cannot strand the campaign.

  It is not what is being removed and it cannot make the outcome unsafe,
  so it does not force a re-proposal -- but it IS drift, so it is recorded
  in the applied result rather than passed over in silence.
  """
  before = [
      _link_row(resource_name=LINK, phone="9174404316"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
      _link_row(resource_name=STRAY_LINK, phone="2013452124"),
  ]
  after = [
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
      _link_row(resource_name=STRAY_LINK, phone="2013452124"),
  ]
  client, _ = _client([before, after])

  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec(phone="9174404316")
  )
  assert out["outcome"] == "applied"
  assert out["approved_remaining_links_reverified"] == [OTHER_LINK]
  assert out["unapproved_additional_links_present_at_apply"] == [STRAY_LINK]


def test_detach_hard_fails_when_the_target_asset_was_edited_after_approval():
  """Same link, number swapped underneath it.

  Asset phone numbers are editable in place, so a link approved for
  removal because it carried 917 can be carrying 914 by apply time.
  Removing it then destroys a good number under a stale approval.
  """
  before = [
      _link_row(resource_name=LINK, phone="9144404316"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  client, services = _client([before])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )

  assert "now carries" in str(excinfo.value)
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_detach_refuses_a_proposal_without_recorded_remaining_identities():
  """A spec that cannot prove the approved state must never apply."""
  spec = _detach_spec()
  spec["expected_remaining_serving_links"] = [OTHER_LINK]  # legacy bare names
  before = [
      _link_row(resource_name=LINK),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  client, services = _client([before])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(client, "123", spec)

  assert "full identities" in str(excinfo.value)
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


# ---------------------------------------------------------------------------
# SERVING (ENABLED) is not the same thing as ACTIVE (non-removed). A paused
# call asset advertises nothing, so it can never justify or survive a detach.
# ---------------------------------------------------------------------------


@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_campaign")
def test_detach_propose_refuses_when_only_replacement_is_paused(
    mock_lookup, mock_root, mock_get_client, tmp_path
):
  """917 ENABLED, 914 PAUSED. Detaching would advertise no number at all."""
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Search Campaign May 2025"
  client, _ = _client([[
      _link_row(resource_name=LINK, phone="9174404316"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316", status="PAUSED"),
  ]])
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as excinfo:
    gated_assets.propose_detach_call_asset_from_campaign(
        customer_id="123",
        campaign_resource_name=CAMPAIGN,
        phone_number="9174404316",
        country_code="US",
        reason_code="client_request",
        reason_detail="Unapproved number.",
        client_root=str(tmp_path),
    )

  message = str(excinfo.value)
  assert "no ENABLED CALL asset" in message
  assert "PAUSED" in message
  assert "do not count as replacement coverage" in message


@mock.patch("ads_mcp.tools.gated_assets.approval.write_proposal")
@mock.patch("ads_mcp.tools.gated_assets._get_client")
@mock.patch("ads_mcp.tools.gated_assets.audit.resolve_client_root")
@mock.patch("ads_mcp.tools.gated_assets.mutations_gated._gaql_lookup_campaign")
def test_detach_propose_records_target_status_and_serving_links_only(
    mock_lookup, mock_root, mock_get_client, mock_write, tmp_path
):
  """Only ENABLED links are recorded as the approved replacement state."""
  mock_root.return_value = tmp_path
  mock_lookup.return_value = "Search Campaign May 2025"
  mock_write.return_value = {"approval_code": "TEST01"}
  client, _ = _client([[
      _link_row(resource_name=LINK, phone="9174404316"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
      _link_row(resource_name=STRAY_LINK, phone="2013452124", status="PAUSED"),
  ]])
  mock_get_client.return_value = client

  gated_assets.propose_detach_call_asset_from_campaign(
      customer_id="123",
      campaign_resource_name=CAMPAIGN,
      phone_number="9174404316",
      country_code="US",
      reason_code="client_request",
      reason_detail="Unapproved number.",
      client_root=str(tmp_path),
  )

  spec = mock_write.call_args.kwargs["spec"]
  assert spec["expected_target_status"] == "ENABLED"
  recorded = spec["expected_remaining_serving_links"]
  assert [r["resource_name"] for r in recorded] == [OTHER_LINK]
  assert recorded[0]["country_code"] == "US"
  assert STRAY_LINK not in str(recorded), "paused links are not coverage"

  human = mock_write.call_args.kwargs["operations_human"]
  assert any("NOT counted as coverage" in line for line in human)


def test_detach_hard_fails_when_approved_replacement_is_paused_at_apply():
  """914 was ENABLED at approval and is PAUSED now. That is material."""
  before = [
      _link_row(resource_name=LINK, phone="9174404316"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316", status="PAUSED"),
  ]
  client, services = _client([before])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )

  message = str(excinfo.value)
  assert "is now PAUSED, not ENABLED" in message
  assert "no serving replacement coverage" in message
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_detach_unrelated_paused_link_cannot_substitute_for_replacement():
  """Approved 914 gone, only a PAUSED 201 left. Doubly unacceptable."""
  before = [
      _link_row(resource_name=LINK, phone="9174404316"),
      _link_row(resource_name=STRAY_LINK, phone="2013452124", status="PAUSED"),
  ]
  client, services = _client([before])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )

  assert "no longer present on this campaign" in str(excinfo.value)
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


# ---------------------------------------------------------------------------
# Country code. _same_number() ignores country prefixes by design, so the
# country has to be compared on its own or a different number looks equal.
# ---------------------------------------------------------------------------


def test_detach_hard_fails_when_replacement_country_code_changed():
  """Same local digits, different country, is a different number."""
  before = [
      _link_row(resource_name=LINK, phone="9174404316"),
      _link_row(
          resource_name=OTHER_LINK, phone="9144404316", country="CA"
      ),
  ]
  client, services = _client([before])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )

  message = str(excinfo.value)
  assert "country code CA" in message
  assert "different number" in message
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_detach_hard_fails_when_target_country_code_changed():
  before = [
      _link_row(resource_name=LINK, phone="9174404316", country="CA"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  client, services = _client([before])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )

  assert "country code CA" in str(excinfo.value)
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_detach_country_code_comparison_is_case_insensitive():
  """"us" and "US" are the same country, not drift."""
  before = [
      _link_row(resource_name=LINK, phone="9174404316", country="us"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316", country="us"),
  ]
  after = [_link_row(resource_name=OTHER_LINK, phone="9144404316")]
  client, _ = _client([before, after])

  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec(phone="9174404316")
  )
  assert out["outcome"] == "applied"


# ---------------------------------------------------------------------------
# An already-detached target comes back as a REMOVED row, not an absent one.
# ---------------------------------------------------------------------------


def test_detach_no_ops_when_approved_target_row_is_removed():
  """Someone detached 917 after approval. Nothing left to do."""
  before = [
      _link_row(resource_name=LINK, phone="9174404316", status="REMOVED"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  client, services = _client([before])

  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec(phone="9174404316")
  )
  assert out["outcome"] == "no_op"
  assert out["status"] == "REMOVED"
  assert out["campaign_asset"] == LINK
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_detach_no_ops_on_removed_target_even_if_same_number_exists_elsewhere():
  """Never fall through and detach a different link carrying 917."""
  before = [
      _link_row(resource_name=LINK, phone="9174404316", status="REMOVED"),
      _link_row(resource_name=STRAY_LINK, phone="9174404316"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  client, services = _client([before])

  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec(phone="9174404316")
  )
  assert out["outcome"] == "no_op"
  assert out["campaign_asset"] == LINK
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_detach_hard_fails_when_target_status_drifted_to_paused():
  before = [
      _link_row(resource_name=LINK, phone="9174404316", status="PAUSED"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  client, services = _client([before])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )

  assert "was ENABLED when approved and is now PAUSED" in str(excinfo.value)
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


def test_detach_refuses_a_spec_without_a_frozen_target_status():
  spec = _detach_spec(phone="9174404316")
  del spec["expected_target_status"]
  before = [
      _link_row(resource_name=LINK, phone="9174404316"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  client, services = _client([before])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(client, "123", spec)

  assert "does not record the approved status of the target link" in str(
      excinfo.value
  )
  services["CampaignAssetService"].mutate_campaign_assets.assert_not_called()


# ---------------------------------------------------------------------------
# AFTER verification. The pre-apply gate stops an unsafe mutation from being
# attempted. This stops us claiming a destination state that does not exist.
# The remove has already happened by the time these fire, so a failure here
# is an incident report, not a refusal.
# ---------------------------------------------------------------------------


def _detach_before():
  return [
      _link_row(resource_name=LINK, phone="9174404316"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]


def _assert_incident(excinfo, services):
  """A post-apply failure must read as an incident, not a refusal."""
  message = str(excinfo.value)
  assert "POST-APPLY VERIFICATION FAILURE" in message
  assert "WAS APPLIED" in message
  assert "NO AUTOMATIC REMEDIATION WAS PERFORMED" in message
  assert "Governed incident review is required" in message
  # The mutation really did go through; that is why this is an incident.
  services["CampaignAssetService"].mutate_campaign_assets.assert_called()


def test_post_apply_fails_when_approved_replacement_missing_afterwards():
  after = []
  client, services = _client([_detach_before(), after])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )
  assert "is missing from the campaign" in str(excinfo.value)
  _assert_incident(excinfo, services)


def test_post_apply_fails_when_approved_replacement_removed_afterwards():
  after = [
      _link_row(resource_name=OTHER_LINK, phone="9144404316", status="REMOVED")
  ]
  client, services = _client([_detach_before(), after])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )
  assert "reads back as REMOVED" in str(excinfo.value)
  _assert_incident(excinfo, services)


def test_post_apply_fails_when_approved_replacement_paused_afterwards():
  after = [
      _link_row(resource_name=OTHER_LINK, phone="9144404316", status="PAUSED")
  ]
  client, services = _client([_detach_before(), after])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )
  assert "reads back as PAUSED, not ENABLED" in str(excinfo.value)
  _assert_incident(excinfo, services)


@pytest.mark.parametrize(
    "after_row, expected",
    [
        (
            {"asset": REPLACEMENT_ASSET},
            "now points at asset",
        ),
        (
            {"phone": "2013452124"},
            "now carries 2013452124",
        ),
        (
            {"country": "CA"},
            "now carries country code CA",
        ),
    ],
)
def test_post_apply_fails_when_approved_replacement_edited_afterwards(
    after_row, expected
):
  """Asset, number or country swapped underneath the approved link."""
  kwargs = {"resource_name": OTHER_LINK, "phone": "9144404316"}
  kwargs.update(after_row)
  client, services = _client([_detach_before(), [_link_row(**kwargs)]])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )
  assert expected in str(excinfo.value)
  _assert_incident(excinfo, services)


@pytest.mark.parametrize("lingering", ["ENABLED", "PAUSED"])
def test_post_apply_fails_when_target_still_active_afterwards(lingering):
  """The remove did not take. Still a failure, and not the incident path."""
  after = [
      _link_row(resource_name=LINK, phone="9174404316", status=lingering),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  client, _ = _client([_detach_before(), after])
  with pytest.raises(ToolError) as excinfo:
    gated_assets._execute_detach_call_asset_from_campaign(
        client, "123", _detach_spec(phone="9174404316")
    )
  message = str(excinfo.value)
  assert "still reads back as an active link" in message
  assert f"status {lingering}" in message


def test_post_apply_succeeds_when_target_reads_back_removed():
  """Target present but REMOVED, replacement intact. That is success."""
  after = [
      _link_row(resource_name=LINK, phone="9174404316", status="REMOVED"),
      _link_row(resource_name=OTHER_LINK, phone="9144404316"),
  ]
  client, _ = _client([_detach_before(), after])

  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec(phone="9174404316")
  )
  assert out["outcome"] == "applied"
  assert out["approved_remaining_links_reverified"] == [OTHER_LINK]
  assert [l["resource_name"] for l in out["remaining_serving_links"]] == [
      OTHER_LINK
  ]


def test_post_apply_succeeds_when_target_absent_and_replacement_intact():
  """Control: the normal happy path still returns applied."""
  after = [_link_row(resource_name=OTHER_LINK, phone="9144404316")]
  client, _ = _client([_detach_before(), after])

  out = gated_assets._execute_detach_call_asset_from_campaign(
      client, "123", _detach_spec(phone="9174404316")
  )
  assert out["outcome"] == "applied"
  assert out["remaining_serving_links"][0]["status"] == "ENABLED"
