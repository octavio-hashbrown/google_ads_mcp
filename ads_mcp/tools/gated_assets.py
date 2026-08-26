# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Gated call-asset attachment at ad-group and campaign level.

Call assets are account-level resources linked to campaigns or ad groups.
Creating a fresh asset per link would litter the account with duplicate
phone numbers that report separately, so these tools always search for an
existing CALL asset with the same number and reuse it.

An existing asset is NEVER modified. Call assets are shared, and a
reused one is frequently already serving a live campaign, so the only
writes performed here are creating a new asset (ad-group tool only, and
only when asked) and creating a link.

Reuse safety has two independent halves, both checked at propose time and
re-checked at apply time:

1. Asset configuration — conversion reporting state, resource-level
   conversion action and ad schedule. See assess_reuse_compatibility.
2. Account call reporting — whether call reporting and call conversion
   reporting are on, and whether the account-level call conversion action
   still resolves to an enabled AD_CALL action. See
   verify_account_call_reporting. This matters because when an asset is
   set to USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION, that account-level
   action is what actually counts the calls.

Both register into the shared approval gate in `mutations_gated`.
"""

import re
from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException

from ads_mcp.coordinator import mcp_server as mcp
from ads_mcp.governance import approval
from ads_mcp.governance import audit
from ads_mcp.tools._ads_api import enum_types
from ads_mcp.tools._ads_api import resource_types
from ads_mcp.tools._ads_api import service_types
from ads_mcp.tools.mutations.common import _get_client
from ads_mcp.tools.mutations.common import _handle_google_ads_error
from ads_mcp.tools import mutations_gated


# Reporting states under which the asset-level call_conversion_action is
# NOT operative: the account-level action counts the calls instead.
# Verified empirically — assets carrying an unresolvable asset-level
# action still attributed every counted call to the account-level one.
_ACCOUNT_LEVEL_REPORTING_STATES = frozenset({
    "UNSPECIFIED",
    "UNKNOWN",
    "USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION",
})

_RESOURCE_LEVEL_REPORTING_STATE = "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"

_ACTIVE_LINK_STATUSES = frozenset({"ENABLED", "PAUSED"})


def _digits(phone: str) -> str:
  return re.sub(r"\D", "", phone or "")


def _same_number(a: str, b: str) -> bool:
  """Compares phone numbers ignoring formatting and country prefixes.

  "(201) 746-6577", "2017466577" and "+1 201-746-6577" are the same
  number stored three ways, and matching them literally would create a
  duplicate asset.
  """
  da, db = _digits(a), _digits(b)
  if not da or not db or min(len(da), len(db)) < 7:
    return False
  return da == db or da.endswith(db) or db.endswith(da)


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


def _read_call_assets(ads_client, customer_id: str) -> list[dict[str, Any]]:
  """Reads every CALL asset with the full configuration that governs reuse.

  Phone number and country alone do not make two call assets equivalent.
  Conversion reporting state, the resource-level conversion action and
  any ad schedule all change how a reused asset behaves.
  """
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT asset.resource_name, asset.name, "
      "asset.call_asset.phone_number, asset.call_asset.country_code, "
      "asset.call_asset.call_conversion_reporting_state, "
      "asset.call_asset.call_conversion_action, "
      "asset.call_asset.ad_schedule_targets "
      "FROM asset "
      "WHERE asset.type = 'CALL'"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  assets = []
  for row in rows:
    call = row.asset.call_asset
    schedules = [
        {
            "day_of_week": s.day_of_week.name,
            "start_hour": s.start_hour,
            "start_minute": s.start_minute.name,
            "end_hour": s.end_hour,
            "end_minute": s.end_minute.name,
        }
        for s in call.ad_schedule_targets
    ]
    assets.append({
        "resource_name": row.asset.resource_name,
        "name": row.asset.name,
        "phone_number": call.phone_number,
        "country_code": call.country_code,
        "call_conversion_reporting_state": (
            call.call_conversion_reporting_state.name
        ),
        "call_conversion_action": call.call_conversion_action,
        "ad_schedule_targets": schedules,
    })
  return assets


def _resolve_conversion_action(
    ads_client, customer_id: str, resource_name: str
) -> dict[str, Any] | None:
  """Resolves a conversion action resource name, or None if it doesn't exist."""
  if not resource_name:
    return None
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT conversion_action.resource_name, conversion_action.id, "
      "conversion_action.name, conversion_action.type, "
      "conversion_action.category, conversion_action.status, "
      "conversion_action.primary_for_goal, "
      "conversion_action.include_in_conversions_metric, "
      "conversion_action.counting_type, "
      "conversion_action.phone_call_duration_seconds "
      "FROM conversion_action "
      f"WHERE conversion_action.resource_name = '{resource_name}' "
      "LIMIT 1"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  for row in rows:
    action = row.conversion_action
    return {
        "resource_name": action.resource_name,
        "id": action.id,
        "name": action.name,
        "type": action.type_.name,
        "category": action.category.name,
        "status": action.status.name,
        "primary_for_goal": bool(action.primary_for_goal),
        "include_in_conversions_metric": bool(
            action.include_in_conversions_metric
        ),
        "counting_type": action.counting_type.name,
        "phone_call_duration_seconds": action.phone_call_duration_seconds,
    }
  return None


def _read_account_call_settings(
    ads_client, customer_id: str
) -> dict[str, Any]:
  """Reads the customer-level call reporting settings."""
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT customer.id, "
      "customer.call_reporting_setting.call_reporting_enabled, "
      "customer.call_reporting_setting.call_conversion_reporting_enabled, "
      "customer.call_reporting_setting.call_conversion_action "
      "FROM customer "
      "LIMIT 1"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  for row in rows:
    setting = row.customer.call_reporting_setting
    return {
        "call_reporting_enabled": bool(setting.call_reporting_enabled),
        "call_conversion_reporting_enabled": bool(
            setting.call_conversion_reporting_enabled
        ),
        "call_conversion_action": setting.call_conversion_action,
    }
  raise ToolError(f"Could not read call reporting settings for {customer_id}.")


def _enabled_ad_call_actions(ads_client, customer_id: str) -> list[dict[str, str]]:
  """Returns ENABLED AD_CALL / CALL_FROM_ADS conversion actions."""
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT conversion_action.resource_name, conversion_action.name "
      "FROM conversion_action "
      "WHERE conversion_action.status = 'ENABLED' "
      "AND conversion_action.type = 'AD_CALL' "
      "AND conversion_action.origin = 'CALL_FROM_ADS' "
      "LIMIT 20"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return [
      {
          "resource_name": row.conversion_action.resource_name,
          "name": row.conversion_action.name,
      }
      for row in rows
  ]


def _find_account_level_call_attribution(
    ads_client, customer_id: str
) -> dict[str, Any] | None:
  """Proves the ACCOUNT-LEVEL call path itself is carrying conversions.

  The weaker question -- "does some AD_CALL action have conversions?" --
  is NOT sufficient. A different, perfectly healthy AD_CALL action could
  be converting while the configured account-level reference is genuinely
  stale, and accepting that would let "some call action works" masquerade
  as "the account-level reference is corroborated".

  So this walks the link, not the action. It requires a CALL asset that
  is itself configured for USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION --
  i.e. an asset whose calls can only be counted through the account-level
  reference -- and requires that asset link to carry conversions
  segmented to an ENABLED AD_CALL/CALL_FROM_ADS action. That combination
  cannot occur unless the account-level path is live.

  Both campaign-level and ad-group-level links are checked.

  Returns:
      Evidence dict, or None when the tie cannot be demonstrated.
  """
  actions = _enabled_ad_call_actions(ads_client, customer_id)
  if not actions:
    return None

  service = ads_client.get_service("GoogleAdsService")
  in_list = ", ".join(f"'{a['resource_name']}'" for a in actions)
  names = {a["resource_name"]: a["name"] for a in actions}

  for resource, parent_field in (
      ("campaign_asset", "campaign_asset.asset"),
      ("ad_group_asset", "ad_group_asset.asset"),
  ):
    query = (
        f"SELECT {parent_field}, "
        "asset.call_asset.call_conversion_reporting_state, "
        "segments.conversion_action, metrics.all_conversions "
        f"FROM {resource} "
        f"WHERE {resource}.field_type = 'CALL' "
        "AND segments.date DURING LAST_30_DAYS "
        f"AND segments.conversion_action IN ({in_list})"
    )
    try:
      rows = service.search(customer_id=customer_id, query=query)
    except GoogleAdsException as e:
      _handle_google_ads_error(e)
    for row in rows:
      state = row.asset.call_asset.call_conversion_reporting_state.name
      if state != "USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION":
        continue
      if row.metrics.all_conversions <= 0:
        continue
      action_resource = row.segments.conversion_action
      return {
          "level": resource,
          "asset": getattr(row, resource).asset,
          "reporting_state": state,
          "action_resource": action_resource,
          "action_name": names.get(action_resource, action_resource),
          "all_conversions": row.metrics.all_conversions,
      }
  return None


def verify_account_call_reporting(
    ads_client,
    customer_id: str,
    expected_call_conversion_action: str | None = None,
    *,
    accepted_system_managed_reference: str | None = None,
    require_recorded_exception: bool = False,
) -> tuple[dict[str, Any], list[str]]:
  """Verifies account-level call reporting is intact.

  When an asset uses USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION, the
  account-level action is what counts its calls, so the asset's own
  configuration is only half the picture. This checks the other half.

  Args:
      ads_client: Google Ads client.
      customer_id: Google Ads customer ID.
      expected_call_conversion_action: Resource name the account-level
          call conversion action must still be. Pass the value recorded
          at propose time to detect drift before applying.

  Returns:
      (settings, issues). `settings` carries the raw values plus the
      resolved conversion action under "resolved_action". A non-empty
      `issues` list means call attribution is not in the state that
      justified reuse, and callers treat it as a hard block.

      `settings["warnings"]` is present only when a fault was downgraded
      to a non-blocking warning on positive evidence. Today that applies
      to exactly one case: an account-level action that will not
      enumerate while an ENABLED AD_CALL/CALL_FROM_ADS action is visibly
      counting conversions. Warnings must be surfaced to the approver,
      never silently swallowed.
  """
  settings = _read_account_call_settings(ads_client, customer_id)
  issues = []

  if not settings["call_reporting_enabled"]:
    issues.append(
        "Account call reporting is DISABLED "
        "(customer.call_reporting_setting.call_reporting_enabled = false)."
    )
  if not settings["call_conversion_reporting_enabled"]:
    issues.append(
        "Account call CONVERSION reporting is DISABLED "
        "(call_conversion_reporting_enabled = false). Calls would serve "
        "but not be counted."
    )

  action_resource = settings["call_conversion_action"]
  if not action_resource:
    issues.append(
        "No account-level call conversion action is set, so calls from an "
        "asset using account-level reporting would not be attributed."
    )
    settings["resolved_action"] = None
    return settings, issues

  if (
      expected_call_conversion_action
      and action_resource != expected_call_conversion_action
  ):
    issues.append(
        "Account-level call conversion action changed: expected "
        f"{expected_call_conversion_action}, found {action_resource}."
    )

  resolved = _resolve_conversion_action(ads_client, customer_id, action_resource)
  settings["resolved_action"] = resolved

  if resolved is None:
    # An account-level reference that will not enumerate is ambiguous: it
    # is either a genuinely broken pointer, or a Google system-managed
    # action the customer-scoped conversion_action report does not return.
    # Only positive, call-specific evidence separates the two. Every other
    # account-level fault must already be clean (`not issues`), so a
    # disabled flag or a drifted reference can never be bypassed here.
    corroboration = None
    if (
        settings["call_reporting_enabled"]
        and settings["call_conversion_reporting_enabled"]
        and not issues
    ):
      corroboration = _find_account_level_call_attribution(
          ads_client, customer_id
      )

    if not corroboration:
      issues.append(
          f"Account-level call conversion action {action_resource} does not "
          "resolve to a conversion action in this customer, and no CALL "
          "asset configured for USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION "
          "could be shown carrying AD_CALL/CALL_FROM_ADS conversions in the "
          "last 30 days. The account-level path is therefore unproven."
      )
      return settings, issues

    # Corroborated. At PROPOSE this may proceed as a surfaced warning and
    # the reference is handed back to be recorded in the proposal. At
    # APPLY it is not enough on its own: the approved proposal must carry
    # the same exception, so a human explicitly reviewed and accepted it.
    if require_recorded_exception and (
        accepted_system_managed_reference != action_resource
    ):
      issues.append(
          "Refusing to apply under a system-managed account-level call "
          f"conversion reference ({action_resource}): the approved "
          "proposal does not carry a matching, explicitly accepted "
          "system-managed-reference exception "
          f"(recorded: {accepted_system_managed_reference or 'none'}). "
          "Re-propose so the exception is reviewed and approved."
      )
      return settings, issues

    settings["system_managed_reference"] = action_resource
    settings.setdefault("warnings", []).append(
        "ACCEPTED WITH VERIFIED SYSTEM-MANAGED REFERENCE: account-level "
        f"call conversion action {action_resource} does not enumerate "
        "through the conversion_action report for this customer, which is "
        "consistent with a Google system-managed reference. Its literal "
        "identity is UNKNOWN. The account-level path was proven live "
        "instead: CALL asset "
        f"{corroboration['asset']} ({corroboration['level']}) is configured "
        "USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION and carried "
        f"{corroboration['all_conversions']:g} conversions attributed to "
        f"\"{corroboration['action_name']}\" "
        f"({corroboration['action_resource']}) in the last 30 days."
    )
    return settings, issues

  if resolved["status"] != "ENABLED":
    issues.append(
        f"Account-level call conversion action \"{resolved['name']}\" is "
        f"{resolved['status']}, not ENABLED."
    )
  if resolved["type"] != "AD_CALL":
    issues.append(
        f"Account-level call conversion action \"{resolved['name']}\" is "
        f"type {resolved['type']}, not AD_CALL."
    )

  return settings, issues


def assess_reuse_compatibility(
    existing: dict[str, Any],
    intended_call_conversion_reporting_state: str | None = None,
    asset_action: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
  """Splits an existing asset's configuration into blockers and notes.

  Args:
      existing: Asset config as returned by _read_call_assets.
      intended_call_conversion_reporting_state: The state this attachment
          is meant to have. None means "whatever a newly created asset
          gets", i.e. account-level.
      asset_action: The resolved asset-level conversion action, required
          only when the asset uses resource-level attribution.

  Returns:
      (blocking, informational). An empty `blocking` list means the asset
      is safe to reuse without a human decision.
  """
  blocking: list[str] = []
  informational: list[str] = []
  state = existing["call_conversion_reporting_state"]
  action = existing["call_conversion_action"]

  if state == "DISABLED":
    blocking.append(
        "Call conversion reporting is DISABLED on this asset. Reusing it "
        "would attach a phone number whose calls are not counted as "
        "conversions."
    )
  elif state == _RESOURCE_LEVEL_REPORTING_STATE:
    # Here the asset-level action IS operative, so it has to hold up.
    if not action:
      blocking.append(
          "This asset uses RESOURCE-LEVEL call conversion attribution but "
          "names no conversion action, so its calls have nothing to "
          "report to."
      )
    elif asset_action is None:
      blocking.append(
          "This asset uses RESOURCE-LEVEL call conversion attribution and "
          f"names {action}, which does not resolve to a conversion action "
          "in this customer. Under this reporting state that action IS "
          "operative, so the attribution target is broken."
      )
    elif asset_action["status"] != "ENABLED":
      blocking.append(
          "This asset reports to resource-level conversion action "
          f"\"{asset_action['name']}\", which is "
          f"{asset_action['status']}, not ENABLED."
      )
    elif asset_action["type"] != "AD_CALL":
      blocking.append(
          "This asset reports to resource-level conversion action "
          f"\"{asset_action['name']}\" of type {asset_action['type']}, "
          "not AD_CALL. Confirm this matches the intended call "
          "conversion configuration."
      )
    else:
      informational.append(
          "RESOURCE-LEVEL call attribution to "
          f"\"{asset_action['name']}\" ({action}) — verified ENABLED "
          "AD_CALL."
      )
  elif state in _ACCOUNT_LEVEL_REPORTING_STATES:
    if action:
      # Verified: under account-level reporting this field is inert. It is
      # reported so nobody is surprised by it, but it does not block.
      informational.append(
          f"Asset-level call_conversion_action is populated ({action}) but "
          f"is NOT operative while reporting state is {state}. The "
          "account-level call conversion action counts these calls. "
          "Reported for transparency only."
      )
  else:
    blocking.append(f"Unrecognised call conversion reporting state: {state}.")

  if existing["ad_schedule_targets"]:
    blocking.append(
        f"This asset carries {len(existing['ad_schedule_targets'])} ad "
        "schedule target(s), which constrain when it serves: "
        f"{existing['ad_schedule_targets']}."
    )

  if (
      intended_call_conversion_reporting_state
      and intended_call_conversion_reporting_state.upper() != state
  ):
    blocking.append(
        "Intended reporting state "
        f"{intended_call_conversion_reporting_state.upper()} does not "
        f"match the existing asset's {state}."
    )

  return blocking, informational


def _assess_asset_for_reuse(
    ads_client,
    customer_id: str,
    asset: dict[str, Any],
    intended_state: str | None,
) -> tuple[list[str], list[str]]:
  """Resolves the asset-level action when needed, then assesses reuse."""
  asset_action = None
  if asset["call_conversion_reporting_state"] == _RESOURCE_LEVEL_REPORTING_STATE:
    asset_action = _resolve_conversion_action(
        ads_client, customer_id, asset["call_conversion_action"]
    )
  return assess_reuse_compatibility(asset, intended_state, asset_action)


def _find_call_asset(
    ads_client, customer_id: str, phone_number: str, country_code: str
) -> dict[str, Any] | None:
  """Returns an existing CALL asset matching phone + country, or None.

  Matching on number alone is NOT sufficient to justify reuse — callers
  must assess the result before using it.
  """
  for asset in _read_call_assets(ads_client, customer_id):
    if asset["country_code"].upper() != country_code.upper():
      continue
    if _same_number(asset["phone_number"], phone_number):
      return asset
  return None


def _find_ad_group_call_links(
    ads_client, customer_id: str, ad_group_resource_name: str
) -> list[dict[str, Any]]:
  """Returns CALL asset links on the ad group, every status included."""
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT ad_group_asset.resource_name, ad_group_asset.asset, "
      "ad_group_asset.status, asset.call_asset.phone_number, "
      "asset.call_asset.country_code "
      "FROM ad_group_asset "
      f"WHERE ad_group_asset.ad_group = '{ad_group_resource_name}' "
      "AND ad_group_asset.field_type = 'CALL'"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return [
      {
          "resource_name": row.ad_group_asset.resource_name,
          "asset": row.ad_group_asset.asset,
          "status": row.ad_group_asset.status.name,
          "phone_number": row.asset.call_asset.phone_number,
          "country_code": row.asset.call_asset.country_code,
      }
      for row in rows
  ]


def _find_campaign_call_links(
    ads_client, customer_id: str, campaign_resource_name: str
) -> list[dict[str, Any]]:
  """Returns CALL asset links on the campaign, every status included."""
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT campaign_asset.resource_name, campaign_asset.asset, "
      "campaign_asset.status, asset.call_asset.phone_number, "
      "asset.call_asset.country_code "
      "FROM campaign_asset "
      f"WHERE campaign_asset.campaign = '{campaign_resource_name}' "
      "AND campaign_asset.field_type = 'CALL'"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return [
      {
          "resource_name": row.campaign_asset.resource_name,
          "asset": row.campaign_asset.asset,
          "status": row.campaign_asset.status.name,
          "phone_number": row.asset.call_asset.phone_number,
          "country_code": row.asset.call_asset.country_code,
      }
      for row in rows
  ]


def _existing_link_for_number(
    links: list[dict[str, Any]], phone_number: str, country_code: str
) -> dict[str, Any] | None:
  """Returns an ACTIVE link for this number, ignoring REMOVED ones."""
  for link in links:
    if link["status"] not in _ACTIVE_LINK_STATUSES:
      continue
    if link["country_code"].upper() != country_code.upper():
      continue
    if _same_number(link["phone_number"], phone_number):
      return link
  return None


def _removed_link_for_number(
    links: list[dict[str, Any]], phone_number: str, country_code: str
) -> dict[str, Any] | None:
  """Returns a REMOVED link for this number, so it can be surfaced."""
  for link in links:
    if link["status"] != "REMOVED":
      continue
    if link["country_code"].upper() != country_code.upper():
      continue
    if _same_number(link["phone_number"], phone_number):
      return link
  return None


@mcp.tool()
def list_call_assets(
    customer_id: str,
    login_customer_id: str | None = None,
) -> dict[str, Any]:
  """Lists CALL assets, their config, and account call reporting state.

  Read-only. Run this before proposing an attachment so the conversion
  reporting state, conversion action and ad schedule of any candidate
  asset are visible up front, alongside the account-level settings that
  actually count the calls.

  Args:
      customer_id: Google Ads customer ID (digits only).
      login_customer_id: MCC account ID if customer is managed.
  """
  ads_client = _get_client(login_customer_id)
  account, account_issues = verify_account_call_reporting(
      ads_client, customer_id
  )
  assets = _read_call_assets(ads_client, customer_id)
  for asset in assets:
    blocking, informational = _assess_asset_for_reuse(
        ads_client, customer_id, asset, None
    )
    asset["safe_to_reuse_silently"] = not blocking
    asset["reuse_blocking"] = blocking
    asset["reuse_informational"] = informational
  return {
      "mutations_performed": "none — read only",
      "customer_id": customer_id,
      "account_call_reporting": account,
      "account_call_reporting_issues": account_issues,
      "count": len(assets),
      "assets": assets,
  }


# -----------------------------------------------------------------------------
# Operation builders — split out so request shapes can be validated live
# with validate_only, without going near a real mutate.
# -----------------------------------------------------------------------------


def build_call_asset_operation(phone_number: str, country_code: str):
  """Builds the AssetOperation that CREATES a CALL asset."""
  asset = resource_types.Asset()
  asset.call_asset.phone_number = phone_number
  asset.call_asset.country_code = country_code
  return service_types.AssetOperation(create=asset)


def build_ad_group_asset_operation(
    ad_group_resource_name: str, asset_resource_name: str
):
  """Builds the AdGroupAssetOperation that links a CALL asset."""
  ad_group_asset = resource_types.AdGroupAsset(
      ad_group=ad_group_resource_name,
      asset=asset_resource_name,
      field_type=enum_types.AssetFieldTypeEnum.AssetFieldType.CALL,
      status=enum_types.AssetLinkStatusEnum.AssetLinkStatus.ENABLED,
  )
  return service_types.AdGroupAssetOperation(create=ad_group_asset)


def build_campaign_asset_operation(
    campaign_resource_name: str, asset_resource_name: str
):
  """Builds the CampaignAssetOperation that links a CALL asset."""
  campaign_asset = resource_types.CampaignAsset(
      campaign=campaign_resource_name,
      asset=asset_resource_name,
      field_type=enum_types.AssetFieldTypeEnum.AssetFieldType.CALL,
      status=enum_types.AssetLinkStatusEnum.AssetLinkStatus.ENABLED,
  )
  return service_types.CampaignAssetOperation(create=campaign_asset)


# -----------------------------------------------------------------------------
# Shared propose helper
# -----------------------------------------------------------------------------


def _prepare_attachment(
    ads_client,
    customer_id: str,
    *,
    level: str,
    parent_resource_name: str,
    parent_label: str,
    links: list[dict[str, Any]],
    phone_number: str,
    country_code: str,
    intended_state: str | None,
    expected_account_action: str | None,
    reuse_incompatible_asset: bool,
    reuse_existing_asset_only: bool,
) -> tuple[list[str], dict[str, Any]]:
  """Resolves and validates an attachment plan. Returns (human_lines, spec)."""
  if not _digits(phone_number):
    raise ToolError(f"phone_number has no digits: {phone_number!r}")
  if len(country_code) != 2:
    raise ToolError(
        f"country_code must be a two-letter code, got {country_code!r}"
    )

  duplicate = _existing_link_for_number(links, phone_number, country_code)
  if duplicate:
    raise ToolError(
        f'{level.capitalize()} "{parent_label}" already has {phone_number} '
        f"attached as a call asset ({duplicate['resource_name']}, status "
        f"{duplicate['status']}). Nothing to propose."
    )

  existing_asset = _find_call_asset(
      ads_client, customer_id, phone_number, country_code
  )
  if existing_asset is None and reuse_existing_asset_only:
    raise ToolError(
        f"No existing CALL asset in customer {customer_id} carries "
        f"{phone_number} ({country_code}), and this tool is reuse-only — "
        "it will not create a new asset. Verify the number, or use the "
        "ad-group tool with reuse_existing_asset_only=False if a new "
        "asset is genuinely wanted."
    )

  blocking: list[str] = []
  informational: list[str] = []
  account: dict[str, Any] = {}
  account_issues: list[str] = []

  if existing_asset:
    blocking, informational = _assess_asset_for_reuse(
        ads_client, customer_id, existing_asset, intended_state
    )
    # Account-level attribution is the other half of reuse safety.
    account, account_issues = verify_account_call_reporting(
        ads_client, customer_id, expected_account_action
    )
    if account_issues:
      raise ToolError(
          "STOPPING FOR APPROVAL. Account-level call reporting is not in "
          "the state that justifies reusing an existing call asset:\n"
          + "\n".join(f"  - {issue}" for issue in account_issues)
          + "\n\nCurrent account settings:\n"
          f"  call_reporting_enabled: "
          f"{account.get('call_reporting_enabled')}\n"
          f"  call_conversion_reporting_enabled: "
          f"{account.get('call_conversion_reporting_enabled')}\n"
          f"  call_conversion_action: "
          f"{account.get('call_conversion_action') or '(none)'}\n"
          "Resolve this before attaching call assets. This is not "
          "overridable by a reuse flag, because it governs whether calls "
          "are counted at all."
      )

    if blocking and not reuse_incompatible_asset:
      raise ToolError(
          "STOPPING FOR APPROVAL. An existing call asset carries this "
          f"number ({existing_asset['resource_name']}), but its "
          "configuration is not safe to reuse without a decision:\n"
          + "\n".join(f"  - {issue}" for issue in blocking)
          + "\n\nCurrent configuration:\n"
          f"  phone_number: {existing_asset['phone_number']}\n"
          f"  country_code: {existing_asset['country_code']}\n"
          "  call_conversion_reporting_state: "
          f"{existing_asset['call_conversion_reporting_state']}\n"
          "  call_conversion_action: "
          f"{existing_asset['call_conversion_action'] or '(none)'}\n"
          "  ad_schedule_targets: "
          f"{existing_asset['ad_schedule_targets'] or '(none)'}\n\n"
          "This asset is account-level and may already be linked "
          "elsewhere, so it will not be modified. Either re-call with "
          "reuse_incompatible_asset=True to reuse it as configured, or "
          "resolve the difference outside this tool first."
      )

  human_lines = [f'{level.capitalize()} "{parent_label}"']
  if existing_asset:
    human_lines.append(
        f"Reuse existing call asset {existing_asset['resource_name']} "
        f"({existing_asset['phone_number']} / "
        f"{existing_asset['country_code']}) — no new asset created, and "
        "the existing asset is NOT modified."
    )
    human_lines.append(
        "Existing asset configuration: reporting_state="
        f"{existing_asset['call_conversion_reporting_state']}, "
        "conversion_action="
        f"{existing_asset['call_conversion_action'] or '(none)'}, "
        "ad_schedule_targets="
        f"{existing_asset['ad_schedule_targets'] or '(none)'}"
    )
    resolved = account.get("resolved_action")
    human_lines.append(
        "Account call reporting: enabled="
        f"{account.get('call_reporting_enabled')}, conversion_reporting="
        f"{account.get('call_conversion_reporting_enabled')}, action="
        + (
            f"\"{resolved['name']}\" ({resolved['type']}, "
            f"{resolved['status']}, {resolved['phone_call_duration_seconds']}s "
            f"threshold) {account.get('call_conversion_action')}"
            if resolved
            else str(account.get("call_conversion_action") or "(none)")
        )
    )
    for note in informational:
      human_lines.append(f"    NOTE: {note}")
    if blocking:
      human_lines.append(
          "WARNING: reusing an asset with configuration differences "
          "explicitly accepted via reuse_incompatible_asset=True:"
      )
      human_lines.extend(f"    {issue}" for issue in blocking)
  else:
    human_lines.append(
        f"Create a new call asset for {phone_number} ({country_code}) — "
        "no existing asset in this account carries this number."
    )

  human_lines.append(
      f'Link that asset to "{parent_label}" as field_type CALL.'
  )

  revived = _removed_link_for_number(links, phone_number, country_code)
  if revived:
    human_lines.append(
        f"NOTE: a REMOVED link for this number already exists at this "
        f"level ({revived['resource_name']}). This re-establishes a "
        "previously removed association."
    )

  active = [l for l in links if l["status"] in _ACTIVE_LINK_STATUSES]
  if active:
    human_lines.append(
        f"For context, this {level} already has {len(active)} other call "
        "asset(s) attached: "
        + ", ".join(f"{l['phone_number']} [{l['status']}]" for l in active)
    )

  spec = {
      "phone_number": phone_number,
      "country_code": country_code.upper(),
      "intended_call_conversion_reporting_state": intended_state,
      "expected_account_call_conversion_action": (
          expected_account_action or account.get("call_conversion_action")
      ),
      "reuse_incompatible_asset": reuse_incompatible_asset,
      "reuse_existing_asset_only": reuse_existing_asset_only,
      # Recorded for transparency. Apply re-resolves rather than trusting
      # it, in case the account changed since approval.
      "reuse_asset_resource_name": (
          existing_asset["resource_name"] if existing_asset else None
      ),
      "accepted_incompatibilities": blocking,
      # Set ONLY when the account-level call conversion action could not
      # be enumerated AND the account-level path was independently proven
      # live. Apply refuses to tolerate that state unless this exact
      # reference is carried here, so approving the proposal is what
      # accepts the exception.
      "accepted_system_managed_reference": account.get(
          "system_managed_reference"
      ),
  }
  for warning in account.get("warnings", []):
    human_lines.append(f"WARNING: {warning}")
  return human_lines, spec


# -----------------------------------------------------------------------------
# Propose — ad group level
# -----------------------------------------------------------------------------


@mcp.tool()
def propose_attach_call_asset_to_ad_group(
    customer_id: str,
    ad_group_resource_name: str,
    phone_number: str,
    country_code: str,
    reason_code: str,
    intended_call_conversion_reporting_state: str | None = None,
    expected_account_call_conversion_action: str | None = None,
    reuse_incompatible_asset: bool = False,
    reuse_existing_asset_only: bool = False,
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes attaching a call asset to an AD GROUP, reusing where safe.

  Resolves the whole plan before asking for approval: whether an existing
  CALL asset will be reused or a new one created, the state of account
  level call reporting, and whether the ad group already carries a link.

  A matching phone number is NOT on its own sufficient grounds for reuse.
  The existing asset's conversion reporting state, resource-level
  conversion action and ad schedule are read and assessed first. When the
  asset uses account-level reporting, the account's own call conversion
  action is verified too, since that is what actually counts the calls.

  This tool never modifies an existing asset.

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_resource_name: Full resource name of the target ad group.
      phone_number: Phone number to advertise. Formatting is ignored for
          matching purposes.
      country_code: Two-letter country code, e.g. "US".
      intended_call_conversion_reporting_state: The reporting state this
          attachment is meant to have. Omit for the account-level
          default, which is what a newly created asset receives.
      expected_account_call_conversion_action: Resource name the
          account-level call conversion action must be, e.g.
          "customers/123/conversionActions/456". Pass it to pin
          attribution explicitly; it is recorded and re-checked at apply.
      reuse_incompatible_asset: Required to be True before an existing
          asset with a blocking configuration difference will be reused.
      reuse_existing_asset_only: True refuses to create a new asset when
          no existing one carries the number.
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)

  ad_group_name = mutations_gated._gaql_lookup_ad_group(  # pylint: disable=protected-access
      ads_client, customer_id, ad_group_resource_name
  )
  links = _find_ad_group_call_links(
      ads_client, customer_id, ad_group_resource_name
  )
  human_lines, spec = _prepare_attachment(
      ads_client,
      customer_id,
      level="ad group",
      parent_resource_name=ad_group_resource_name,
      parent_label=ad_group_name,
      links=links,
      phone_number=phone_number,
      country_code=country_code,
      intended_state=intended_call_conversion_reporting_state,
      expected_account_action=expected_account_call_conversion_action,
      reuse_incompatible_asset=reuse_incompatible_asset,
      reuse_existing_asset_only=reuse_existing_asset_only,
  )
  spec["op"] = "attach_call_asset_to_ad_group"
  spec["ad_group_resource_name"] = ad_group_resource_name
  spec["login_customer_id"] = login_customer_id

  return approval.write_proposal(
      root,
      tool_name="attach_call_asset_to_ad_group",
      customer_id=customer_id,
      operations_human=human_lines,
      reason_code=reason_code,
      reason_detail=reason_detail,
      spec=spec,
      client_label=client_label,
  )


propose_attach_call_asset_to_ad_group.__doc__ = (
    propose_attach_call_asset_to_ad_group.__doc__
    % mutations_gated._common_propose_args_doc()  # pylint: disable=protected-access
)


# -----------------------------------------------------------------------------
# Propose — campaign level
# -----------------------------------------------------------------------------


@mcp.tool()
def propose_attach_call_asset_to_campaign(
    customer_id: str,
    campaign_resource_name: str,
    phone_number: str,
    country_code: str,
    reason_code: str,
    intended_call_conversion_reporting_state: str | None = None,
    expected_account_call_conversion_action: str | None = None,
    reuse_incompatible_asset: bool = False,
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes linking an EXISTING call asset to a CAMPAIGN.

  Campaign-level attachment covers every ad group in the campaign, which
  is the right shape when one number serves the whole campaign. Use the
  ad-group tool when different ad groups need different numbers.

  Reuse-only by design: this tool will not create a call asset. If no
  existing asset carries the number it refuses, so a campaign-level link
  can never quietly mint a duplicate phone number.

  The existing asset is never modified — it is frequently already serving
  a live campaign.

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_resource_name: Full resource name of the target campaign.
      phone_number: Phone number to advertise, matching an existing CALL
          asset. Formatting is ignored for matching purposes.
      country_code: Two-letter country code, e.g. "US".
      intended_call_conversion_reporting_state: The reporting state this
          attachment is meant to have. Omit for account-level.
      expected_account_call_conversion_action: Resource name the
          account-level call conversion action must be. Recorded and
          re-checked at apply.
      reuse_incompatible_asset: Required to be True before an existing
          asset with a blocking configuration difference will be reused.
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)

  campaign_name = mutations_gated._gaql_lookup_campaign(  # pylint: disable=protected-access
      ads_client, customer_id, campaign_resource_name
  )
  links = _find_campaign_call_links(
      ads_client, customer_id, campaign_resource_name
  )
  human_lines, spec = _prepare_attachment(
      ads_client,
      customer_id,
      level="campaign",
      parent_resource_name=campaign_resource_name,
      parent_label=campaign_name,
      links=links,
      phone_number=phone_number,
      country_code=country_code,
      intended_state=intended_call_conversion_reporting_state,
      expected_account_action=expected_account_call_conversion_action,
      reuse_incompatible_asset=reuse_incompatible_asset,
      reuse_existing_asset_only=True,
  )
  spec["op"] = "attach_call_asset_to_campaign"
  spec["campaign_resource_name"] = campaign_resource_name
  spec["login_customer_id"] = login_customer_id

  return approval.write_proposal(
      root,
      tool_name="attach_call_asset_to_campaign",
      customer_id=customer_id,
      operations_human=human_lines,
      reason_code=reason_code,
      reason_detail=reason_detail,
      spec=spec,
      client_label=client_label,
  )


propose_attach_call_asset_to_campaign.__doc__ = (
    propose_attach_call_asset_to_campaign.__doc__
    % mutations_gated._common_propose_args_doc()  # pylint: disable=protected-access
)


# -----------------------------------------------------------------------------
# Apply
# -----------------------------------------------------------------------------


def _create_call_asset(
    ads_client, customer_id: str, phone_number: str, country_code: str
) -> str:
  """Creates a CALL asset and returns its resource name."""
  service = ads_client.get_service("AssetService")
  operation = build_call_asset_operation(phone_number, country_code)
  try:
    response = service.mutate_assets(
        customer_id=customer_id, operations=[operation]
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return response.results[0].resource_name


def _resolve_asset_for_apply(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> tuple[str, bool]:
  """Re-resolves the asset at apply time and re-checks reuse safety."""
  phone_number = spec["phone_number"]
  country_code = spec["country_code"]

  existing_asset = _find_call_asset(
      ads_client, customer_id, phone_number, country_code
  )
  if existing_asset is None:
    if spec.get("reuse_existing_asset_only"):
      raise ToolError(
          "Refusing to apply: this proposal is reuse-only, but no existing "
          f"CALL asset carries {phone_number} ({country_code}) any more."
      )
    return (
        _create_call_asset(
            ads_client, customer_id, phone_number, country_code
        ),
        True,
    )

  # Re-assess rather than trusting the proposal: the asset is shared, so
  # someone may have reconfigured it since approval.
  blocking, _ = _assess_asset_for_reuse(
      ads_client,
      customer_id,
      existing_asset,
      spec.get("intended_call_conversion_reporting_state"),
  )
  accepted = spec.get("accepted_incompatibilities") or []
  unreviewed = [i for i in blocking if i not in accepted]
  if unreviewed and not spec.get("reuse_incompatible_asset"):
    raise ToolError(
        f"Refusing to reuse call asset {existing_asset['resource_name']}: "
        "its configuration no longer matches what was approved:\n"
        + "\n".join(f"  - {issue}" for issue in unreviewed)
        + "\nRe-propose so the current configuration is reviewed."
    )

  # Account-level attribution must also still hold. At apply, a
  # system-managed (unenumerable) reference is only tolerated when the
  # approved proposal explicitly recorded that exception.
  _, account_issues = verify_account_call_reporting(
      ads_client,
      customer_id,
      spec.get("expected_account_call_conversion_action"),
      accepted_system_managed_reference=spec.get(
          "accepted_system_managed_reference"
      ),
      require_recorded_exception=True,
  )
  if account_issues:
    raise ToolError(
        "Refusing to apply: account-level call reporting drifted since "
        "approval:\n" + "\n".join(f"  - {i}" for i in account_issues)
    )

  return existing_asset["resource_name"], False


def _execute_attach_call_asset(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
  """Applies an approved AD-GROUP-level attachment."""
  ad_group_resource_name = spec["ad_group_resource_name"]
  links = _find_ad_group_call_links(
      ads_client, customer_id, ad_group_resource_name
  )
  duplicate = _existing_link_for_number(
      links, spec["phone_number"], spec["country_code"]
  )
  if duplicate:
    return {
        "outcome": "no_op",
        "detail": "This number is already linked to the ad group.",
        "ad_group_asset": duplicate["resource_name"],
        "asset": duplicate["asset"],
        "asset_created": False,
    }

  asset_resource_name, asset_created = _resolve_asset_for_apply(
      ads_client, customer_id, spec
  )

  service = ads_client.get_service("AdGroupAssetService")
  operation = build_ad_group_asset_operation(
      ad_group_resource_name, asset_resource_name
  )
  try:
    response = service.mutate_ad_group_assets(
        customer_id=customer_id, operations=[operation]
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  link_resource_name = response.results[0].resource_name

  verified = _find_ad_group_call_links(
      ads_client, customer_id, ad_group_resource_name
  )
  confirmed = next(
      (l for l in verified if l["resource_name"] == link_resource_name), None
  )
  if confirmed is None:
    raise ToolError(
        "Post-apply verification failed: created "
        f"{link_resource_name} but could not read it back."
    )

  return {
      "outcome": "applied",
      "level": "ad_group",
      "ad_group_asset": link_resource_name,
      "asset": asset_resource_name,
      "asset_created": asset_created,
      "asset_reused": not asset_created,
      "verified": confirmed,
      "verified_by": "GAQL read-back of ad_group_asset",
  }


def _execute_attach_call_asset_to_campaign(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
  """Applies an approved CAMPAIGN-level attachment."""
  campaign_resource_name = spec["campaign_resource_name"]
  links = _find_campaign_call_links(
      ads_client, customer_id, campaign_resource_name
  )
  duplicate = _existing_link_for_number(
      links, spec["phone_number"], spec["country_code"]
  )
  if duplicate:
    return {
        "outcome": "no_op",
        "detail": "This number is already linked to the campaign.",
        "campaign_asset": duplicate["resource_name"],
        "asset": duplicate["asset"],
        "asset_created": False,
    }

  asset_resource_name, asset_created = _resolve_asset_for_apply(
      ads_client, customer_id, spec
  )
  if asset_created:
    # Defence in depth: the campaign tool is reuse-only, so reaching here
    # would mean the guards above failed.
    raise ToolError(
        "Internal guard: campaign-level attachment must reuse an existing "
        "asset and must never create one."
    )

  service = ads_client.get_service("CampaignAssetService")
  operation = build_campaign_asset_operation(
      campaign_resource_name, asset_resource_name
  )
  try:
    response = service.mutate_campaign_assets(
        customer_id=customer_id, operations=[operation]
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  link_resource_name = response.results[0].resource_name

  verified = _find_campaign_call_links(
      ads_client, customer_id, campaign_resource_name
  )
  confirmed = next(
      (l for l in verified if l["resource_name"] == link_resource_name), None
  )
  if confirmed is None:
    raise ToolError(
        "Post-apply verification failed: created "
        f"{link_resource_name} but could not read it back."
    )

  return {
      "outcome": "applied",
      "level": "campaign",
      "campaign_asset": link_resource_name,
      "asset": asset_resource_name,
      "asset_created": False,
      "asset_reused": True,
      "verified": confirmed,
      "verified_by": "GAQL read-back of campaign_asset",
  }


mutations_gated.register_executor(
    "attach_call_asset_to_ad_group", _execute_attach_call_asset
)
mutations_gated.register_executor(
    "attach_call_asset_to_campaign", _execute_attach_call_asset_to_campaign
)
