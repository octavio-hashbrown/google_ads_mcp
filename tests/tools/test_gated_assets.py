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
  setattr(row, level, types.SimpleNamespace(asset=asset))
  return row


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
  assert settings["system_managed_reference"] == ACCOUNT_ACTION
  assert any(
      "ACCEPTED WITH VERIFIED SYSTEM-MANAGED REFERENCE" in w
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
  assert "system_managed_reference" not in settings


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
  assert settings["system_managed_reference"] == ACCOUNT_ACTION


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
      accepted_system_managed_reference="customers/123/conversionActions/999",
      require_recorded_exception=True,
  )
  assert any("does not carry a matching" in i for i in issues)


def test_apply_allows_when_matching_exception_was_recorded():
  settings, issues = gated_assets.verify_account_call_reporting(
      _corroborated_client(),
      "123",
      accepted_system_managed_reference=ACCOUNT_ACTION,
      require_recorded_exception=True,
  )
  assert issues == []
  assert settings["system_managed_reference"] == ACCOUNT_ACTION


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
