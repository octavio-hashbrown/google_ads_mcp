# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Criterion mutation tools for Google Ads API."""

from typing import Any

from ads_mcp.coordinator import mcp_server as mcp
from ads_mcp.tools._ads_api import common_types
from ads_mcp.tools._ads_api import enum_types
from ads_mcp.tools._ads_api import resource_types
from ads_mcp.tools._ads_api import service_types
from ads_mcp.tools.mutations import policy
from ads_mcp.tools.mutations.common import _get_client
from ads_mcp.tools.mutations.common import _handle_google_ads_error
from ads_mcp.tools.mutations.common import _resolve_enum
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException


def _existing_keywords(
    ads_client, customer_id: str, ad_group_resource_name: str
) -> set[tuple[str, str]]:
  """Returns {(lowercased text, match_type)} already in the ad group.

  Used to make create_keywords idempotent: re-running after a policy
  authorization must not trip DUPLICATE errors on keywords that landed
  on the first pass.
  """
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT ad_group_criterion.keyword.text, "
      "ad_group_criterion.keyword.match_type "
      "FROM ad_group_criterion "
      f"WHERE ad_group_criterion.ad_group = '{ad_group_resource_name}' "
      "AND ad_group_criterion.type = 'KEYWORD' "
      "AND ad_group_criterion.negative = FALSE "
      "AND ad_group_criterion.status != 'REMOVED'"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return {
      (
          row.ad_group_criterion.keyword.text.strip().lower(),
          row.ad_group_criterion.keyword.match_type.name,
      )
      for row in rows
  }


def _verify_keywords(
    ads_client, customer_id: str, resource_names: list[str]
) -> list[dict[str, str]]:
  """Reads back created criteria so the caller sees what actually landed."""
  if not resource_names:
    return []
  service = ads_client.get_service("GoogleAdsService")
  quoted = ", ".join(f"'{rn}'" for rn in resource_names)
  query = (
      "SELECT ad_group_criterion.resource_name, "
      "ad_group_criterion.keyword.text, "
      "ad_group_criterion.keyword.match_type, "
      "ad_group_criterion.status, "
      "ad_group_criterion.approval_status "
      "FROM ad_group_criterion "
      f"WHERE ad_group_criterion.resource_name IN ({quoted})"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return [
      {
          "resource_name": row.ad_group_criterion.resource_name,
          "text": row.ad_group_criterion.keyword.text,
          "match_type": row.ad_group_criterion.keyword.match_type.name,
          "status": row.ad_group_criterion.status.name,
          "approval_status": row.ad_group_criterion.approval_status.name,
      }
      for row in rows
  ]


def _build_keyword_op(
    ad_group_resource_name: str,
    text: str,
    match_type_name: str,
    exemption_keys: list[dict[str, str]] | None = None,
):
  """Builds one AdGroupCriterionOperation, optionally carrying exemptions.

  `text` and `match_type_name` are passed through verbatim — a resubmit
  for exemption must not alter the keyword the human approved.
  """
  match_type = _resolve_enum(
      enum_types.KeywordMatchTypeEnum.KeywordMatchType,
      match_type_name,
      "match_type",
  )
  criterion = resource_types.AdGroupCriterion(
      ad_group=ad_group_resource_name,
      status=(
          enum_types.AdGroupCriterionStatusEnum.AdGroupCriterionStatus.ENABLED
      ),
      keyword=common_types.KeywordInfo(text=text, match_type=match_type),
  )
  operation = service_types.AdGroupCriterionOperation(create=criterion)
  for key in exemption_keys or []:
    operation.exempt_policy_violation_keys.append(
        common_types.PolicyViolationKey(
            policy_name=key["policy_name"],
            violating_text=key["violating_text"],
        )
    )
  return operation


def _mutate(service, customer_id: str, operations, validate_only: bool):
  """Thin wrapper so every call site sets validate_only the same way."""
  request = service_types.MutateAdGroupCriteriaRequest(
      customer_id=customer_id,
      operations=operations,
      validate_only=validate_only,
  )
  return service.mutate_ad_group_criteria(request=request)


@mcp.tool()
def create_keywords(
    customer_id: str,
    ad_group_resource_name: str,
    keywords: list[dict[str, str]],
    request_policy_exemptions: bool = False,
    validate_only: bool = False,
    login_customer_id: str | None = None,
) -> dict[str, Any]:
  """Creates keywords in an ad group, handling policy exemptions properly.

  Implements Google's documented two-step exemption flow. Step one always
  runs as a server-side dry run, so policy violations are discovered
  before anything is written:

  1. Dry-run the batch with validate_only. Google returns a
     PolicyViolationKey (policy_name + violating_text) per offending
     keyword, each flagged exemptible or not.
  2. Only when `request_policy_exemptions=True`, resubmit each offending
     keyword with the EXACT keys Google returned.

  This is not a blanket "ignore policy" switch. The keys are never
  invented locally, non-exemptible violations are never resubmitted, and
  keyword text and match type are passed through byte-for-byte — a
  keyword is never reworded to slip past a policy check.

  With `request_policy_exemptions=False` (the default) the clean keywords
  are created and the blocked ones are returned with their keys, so a
  human can read the actual policy and authorize a rerun.

  Keywords already present in the ad group are skipped, so rerunning
  after authorization is safe.

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_resource_name: Resource name from create_ad_group.
      keywords: List of keyword dicts, each with:
        - text: The keyword text (e.g., "scoliosis chiropractor nj")
        - match_type: EXACT, PHRASE, or BROAD
      request_policy_exemptions: When True, resubmit exemptible
        violations using the keys Google returned. Requires explicit
        operator intent; defaults to False.
      validate_only: When True, run the dry run and report findings
        without creating anything at all.
      login_customer_id: MCC account ID if customer is managed.

  Returns:
      Dict with resource_names (everything created), plus created,
      created_with_exemption, skipped_already_exists,
      blocked_needs_authorization, blocked_not_exemptible, and verified.
  """
  if not keywords:
    raise ToolError("keywords must contain at least one entry.")

  ads_client = _get_client(login_customer_id)
  service = ads_client.get_service("AdGroupCriterionService")

  requested: list[dict[str, str]] = []
  for kw in keywords:
    if "text" not in kw or "match_type" not in kw:
      raise ToolError(f"Each keyword needs 'text' and 'match_type'. Got: {kw}")
    _resolve_enum(
        enum_types.KeywordMatchTypeEnum.KeywordMatchType,
        kw["match_type"],
        "match_type",
    )
    requested.append(
        {"text": kw["text"], "match_type": kw["match_type"].upper()}
    )

  # Idempotency: drop anything the ad group already carries.
  already = _existing_keywords(ads_client, customer_id, ad_group_resource_name)
  pending, skipped = [], []
  for kw in requested:
    if (kw["text"].strip().lower(), kw["match_type"]) in already:
      skipped.append(kw)
    else:
      pending.append(kw)

  report: dict[str, Any] = {
      "resource_names": [],
      "created": [],
      "created_with_exemption": [],
      "skipped_already_exists": skipped,
      "blocked_needs_authorization": [],
      "blocked_not_exemptible": [],
      "verified": [],
      "validate_only": validate_only,
  }
  if not pending:
    report["note"] = "Every requested keyword already exists in this ad group."
    return report

  operations = [
      _build_keyword_op(ad_group_resource_name, kw["text"], kw["match_type"])
      for kw in pending
  ]

  # ---- Step 1: dry run to discover policy violations, mutating nothing.
  findings_by_index: dict[int, list[dict[str, Any]]] = {}
  try:
    _mutate(service, customer_id, operations, validate_only=True)
  except GoogleAdsException as e:
    findings_by_index, other_errors = policy.parse_policy_errors(e)
    if other_errors:
      # Not a policy problem. Surface it and stop; nothing was written.
      raise ToolError(
          "Dry run failed for reasons other than policy: "
          + "; ".join(other_errors)
      ) from e
    if -1 in findings_by_index:
      raise ToolError(
          "Google returned policy findings that could not be mapped to a "
          "specific keyword, so no safe per-keyword resubmit is possible: "
          + policy.describe(findings_by_index[-1])
      ) from e

  # Partition by what step 1 found.
  clean_ops, clean_kws = [], []
  exempt_ops, exempt_kws = [], []
  for i, kw in enumerate(pending):
    findings = findings_by_index.get(i, [])
    if not findings:
      clean_ops.append(operations[i])
      clean_kws.append(kw)
      continue

    entry = dict(kw)
    entry["findings"] = findings
    blocking = policy.blocking_findings(findings)
    if blocking:
      entry["reason"] = (
          "Not exemptible — an exemption key cannot clear this: "
          + policy.describe(blocking)
      )
      report["blocked_not_exemptible"].append(entry)
      continue

    keys = policy.exemptible_keys(findings)
    entry["exemption_keys"] = keys
    if not request_policy_exemptions:
      entry["reason"] = (
          "Exemptible, but exception requesting was not authorized. "
          "Review the policy, then rerun with "
          "request_policy_exemptions=True to resubmit this exact keyword."
      )
      report["blocked_needs_authorization"].append(entry)
      continue

    exempt_ops.append(
        _build_keyword_op(
            ad_group_resource_name,
            kw["text"],
            kw["match_type"],
            exemption_keys=keys,
        )
    )
    exempt_kws.append(entry)

  if validate_only:
    report["note"] = (
        "Dry run only — nothing was created. "
        f"{len(clean_kws)} would create cleanly, "
        f"{len(exempt_kws)} would create with an exemption request, "
        f"{len(report['blocked_needs_authorization'])} need authorization, "
        f"{len(report['blocked_not_exemptible'])} cannot be exempted."
    )
    report["would_create"] = clean_kws
    return report

  # ---- Step 2a: create the clean keywords as one atomic batch.
  if clean_ops:
    try:
      response = _mutate(service, customer_id, clean_ops, validate_only=False)
    except GoogleAdsException as e:
      _handle_google_ads_error(e)
    for kw, result in zip(clean_kws, response.results):
      report["created"].append({**kw, "resource_name": result.resource_name})
      report["resource_names"].append(result.resource_name)

  # ---- Step 2b: resubmit each exemptible keyword with its own exact keys.
  # One at a time, so a rejection on one does not roll back the others.
  for kw, operation in zip(exempt_kws, exempt_ops):
    try:
      response = _mutate(service, customer_id, [operation], validate_only=False)
    except GoogleAdsException as e:
      failed = dict(kw)
      failed["reason"] = (
          "Exemption request rejected by Google: "
          + "; ".join(err.message for err in e.failure.errors)
      )
      report["blocked_not_exemptible"].append(failed)
      continue
    resource_name = response.results[0].resource_name
    report["created_with_exemption"].append(
        {**kw, "resource_name": resource_name}
    )
    report["resource_names"].append(resource_name)

  report["verified"] = _verify_keywords(
      ads_client, customer_id, report["resource_names"]
  )
  return report


@mcp.tool()
def create_negative_campaign_keywords(
    customer_id: str,
    campaign_resource_name: str,
    keywords: list[str],
    login_customer_id: str | None = None,
) -> dict[str, list[str]]:
  """Creates negative keywords at the campaign level.

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_resource_name: Resource name from create_search_campaign.
      keywords: List of negative keyword strings (e.g., ["free", "fake",
        "replica"]).
      login_customer_id: MCC account ID if customer is managed.

  Returns:
      Dict with list of created criterion resource_names.
  """
  ads_client = _get_client(login_customer_id)
  service = ads_client.get_service("CampaignCriterionService")

  operations = []
  for kw_text in keywords:
    criterion = resource_types.CampaignCriterion(
        campaign=campaign_resource_name,
        negative=True,
        keyword=common_types.KeywordInfo(
            text=kw_text,
            match_type=enum_types.KeywordMatchTypeEnum.KeywordMatchType.BROAD,
        ),
    )
    operations.append(
        service_types.CampaignCriterionOperation(create=criterion)
    )

  try:
    response = service.mutate_campaign_criteria(
        customer_id=customer_id, operations=operations
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  return {"resource_names": [r.resource_name for r in response.results]}


@mcp.tool()
def create_geo_targeting(
    customer_id: str,
    campaign_resource_name: str,
    geo_target_constant_ids: list[int],
    login_customer_id: str | None = None,
) -> dict[str, list[str]]:
  """Adds location targeting to a campaign.

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_resource_name: Resource name from create_search_campaign.
      geo_target_constant_ids: List of geo target constant IDs. Common values:
        2840 (United States), 2124 (Canada).
      login_customer_id: MCC account ID if customer is managed.

  Returns:
      Dict with list of created criterion resource_names.
  """
  ads_client = _get_client(login_customer_id)
  service = ads_client.get_service("CampaignCriterionService")
  geo_svc = ads_client.get_service("GeoTargetConstantService")

  operations = []
  for geo_id in geo_target_constant_ids:
    resource_name = geo_svc.geo_target_constant_path(geo_id)
    criterion = resource_types.CampaignCriterion(
        campaign=campaign_resource_name,
        location=common_types.LocationInfo(
            geo_target_constant=resource_name,
        ),
    )
    operations.append(
        service_types.CampaignCriterionOperation(create=criterion)
    )

  try:
    response = service.mutate_campaign_criteria(
        customer_id=customer_id, operations=operations
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  return {"resource_names": [r.resource_name for r in response.results]}


@mcp.tool()
def remove_campaign_criterion(
    customer_id: str,
    campaign_id: str,
    criterion_id: str,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Removes a campaign criterion (e.g., a geo target).

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_id: Campaign ID (digits only).
      criterion_id: Criterion ID to remove (digits only).
      login_customer_id: MCC account ID if customer is managed.

  Returns:
      Dict with the removed resource_name.
  """
  ads_client = _get_client(login_customer_id)
  service = ads_client.get_service("CampaignCriterionService")

  resource_name = service.campaign_criterion_path(
      customer_id, campaign_id, criterion_id
  )
  operation = service_types.CampaignCriterionOperation(remove=resource_name)

  try:
    response = service.mutate_campaign_criteria(
        customer_id=customer_id, operations=[operation]
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  return {"removed": response.results[0].resource_name}


@mcp.tool()
def exclude_geo_targets(
    customer_id: str,
    campaign_resource_name: str,
    geo_target_constant_ids: list[int],
    login_customer_id: str | None = None,
) -> dict[str, list[str]]:
  """Excludes locations from a campaign (negative geo targeting).

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_resource_name: Resource name from create_search_campaign.
      geo_target_constant_ids: List of geo target constant IDs to exclude.
      login_customer_id: MCC account ID if customer is managed.

  Returns:
      Dict with list of created exclusion resource_names.
  """
  ads_client = _get_client(login_customer_id)
  service = ads_client.get_service("CampaignCriterionService")
  geo_svc = ads_client.get_service("GeoTargetConstantService")

  operations = []
  for geo_id in geo_target_constant_ids:
    resource_name = geo_svc.geo_target_constant_path(geo_id)
    criterion = resource_types.CampaignCriterion(
        campaign=campaign_resource_name,
        negative=True,
        location=common_types.LocationInfo(
            geo_target_constant=resource_name,
        ),
    )
    operations.append(
        service_types.CampaignCriterionOperation(create=criterion)
    )

  try:
    response = service.mutate_campaign_criteria(
        customer_id=customer_id, operations=operations
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  return {"resource_names": [r.resource_name for r in response.results]}
