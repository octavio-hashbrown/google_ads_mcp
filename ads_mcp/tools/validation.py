"""Validate-only capability probing. Cannot commit a change, by construction.

Gating raw mutation tools removed the only MCP-exposed path that could send a
`validate_only=true` request (`create_keywords(validate_only=True)`). That
capability is needed independently of raw mutations -- for verifying API
authorization and for checking a request shape before proposing it -- so it
lives here instead, in a form that cannot write.

Two independent safety properties, either of which alone prevents a write:

  1. `validate_only` is the module constant `_VALIDATE_ONLY = True`. It is
     never read from an argument and there is no code path that sets False.
     Every request asserts it immediately before the call.
  2. Every operation is ZERO-DELTA: the probe reads the entity's current
     value and writes back that same value. If Google ever ignored
     `validate_only`, the resulting change would still be nil.

Targets prefer PAUSED campaigns, so probes stay away from spending entities.

This tool loads with the GOVERNED tier (`ADS_MCP_ENABLE_MUTATIONS=true`). It
does NOT require `ADS_MCP_ENABLE_RAW_MUTATIONS`.
"""

from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers

from ads_mcp.coordinator import mcp_server as mcp
from ads_mcp.tools.mutations.common import _get_client

# Hardcoded. Never parameterised, never overridden.
_VALIDATE_ONLY = True


def _rows(ads_client, customer_id: str, query: str) -> list[Any]:
  """Runs a GAQL read and returns the result rows."""
  service = ads_client.get_service("GoogleAdsService")
  out = []
  for batch in service.search_stream(customer_id=customer_id, query=query):
    out.extend(batch.results)
  return out


def _first_row(ads_client, customer_id: str, queries: list[str]) -> Any | None:
  """Returns the first row from the first query that yields one.

  Probe targets are looked up narrowest-first (inside the chosen campaign),
  then widened. Without the widening step a campaign type that has no
  queryable ad groups -- Performance Max, for instance -- would leave the
  AdGroup and AdGroupCriterion probes permanently SKIPPED, which reads as a
  capability gap when it is only an unlucky probe target.
  """
  for query in queries:
    rows = _rows(ads_client, customer_id, query)
    if rows:
      return rows[0]
  return None


def _send(ads_client, service_name: str, request_type: str, method: str,
          customer_id: str, operation) -> dict[str, Any]:
  """Sends a single-operation mutate with validate_only pinned True."""
  service = ads_client.get_service(service_name)
  request = ads_client.get_type(request_type)
  request.customer_id = customer_id
  request.operations.append(operation)

  assert _VALIDATE_ONLY is True, "validate_only constant was altered"
  request.validate_only = _VALIDATE_ONLY
  assert request.validate_only is True, "validate_only not set on request"

  response = getattr(service, method)(request=request)
  return {
      "status": "ACCEPTED",
      "validate_only": True,
      "results_returned": len(response.results),
      "note": "0 results is the correct validate_only response; nothing was written.",
  }


def _probe(fn) -> dict[str, Any]:
  """Runs one probe, converting API failures into structured findings."""
  try:
    return fn()
  except GoogleAdsException as e:
    codes = []
    message = ""
    for err in e.failure.errors:
      field = err.error_code._pb.WhichOneof("error_code")  # pylint: disable=protected-access
      codes.append("%s=%s" % (field, getattr(err.error_code, field, None)))
      message = err.message
    return {"status": "REJECTED", "error_codes": codes, "message": message}
  except Exception as e:  # pylint: disable=broad-except
    return {"status": "ERROR", "error_codes": [type(e).__name__],
            "message": str(e)[:300]}


@mcp.tool()
def validate_only_capability_check(
    customer_id: str,
    login_customer_id: str | None = None,
) -> dict[str, Any]:
  """Verifies mutation authorization without changing anything.

  Sends one `validate_only=true` request per core service and reports
  whether Google accepted it. An ACCEPTED result means the credentials,
  developer token, and OAuth scope are authorized to submit that mutation
  and the request shape passed full server-side validation -- while nothing
  was written.

  Cannot modify the account: `validate_only` is hardcoded True, and every
  operation writes back the value it just read (zero-delta), so the net
  change is nil even if the flag were ignored. Probe targets prefer PAUSED
  campaigns.

  Use this to confirm API access, or to sanity-check authorization before
  preparing a proposal. It is not a substitute for the approval gate: it
  proves a mutation *could* be submitted, never that one *should* be.

  Args:
      customer_id: Google Ads customer ID (digits only).
      login_customer_id: (Optional) MCC account ID if the customer is
        managed. Usually the MCC above the target account.

  Returns:
      Per-service probe results, plus the resolved targets and an explicit
      statement that no mutation was committed.
  """
  ads_client = _get_client(login_customer_id)
  results: dict[str, Any] = {}
  targets: dict[str, Any] = {}

  # --- Resolve targets, preferring PAUSED campaigns. --------------------
  campaign_rows = _rows(
      ads_client, customer_id,
      "SELECT campaign.resource_name, campaign.name, campaign.status, "
      "campaign_budget.resource_name, campaign_budget.amount_micros "
      "FROM campaign WHERE campaign.status = 'PAUSED' LIMIT 1")
  if not campaign_rows:
    campaign_rows = _rows(
        ads_client, customer_id,
        "SELECT campaign.resource_name, campaign.name, campaign.status, "
        "campaign_budget.resource_name, campaign_budget.amount_micros "
        "FROM campaign WHERE campaign.status != 'REMOVED' LIMIT 1")
  if not campaign_rows:
    raise ToolError(
        "No non-removed campaign found in customer %s to probe against."
        % customer_id)

  row = campaign_rows[0]
  campaign_rn = row.campaign.resource_name
  campaign_name = row.campaign.name
  budget_rn = row.campaign_budget.resource_name
  budget_amount = row.campaign_budget.amount_micros
  targets["campaign"] = {"resource_name": campaign_rn,
                         "status": row.campaign.status.name}
  targets["campaign_budget"] = {"resource_name": budget_rn,
                                "amount_micros": budget_amount}

  # --- CampaignBudgetService: rewrite the identical amount. -------------
  def _budget():
    op = ads_client.get_type("CampaignBudgetOperation")
    budget = op.update
    budget.resource_name = budget_rn
    budget.amount_micros = budget_amount
    op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, budget._pb))  # pylint: disable=protected-access
    return _send(ads_client, "CampaignBudgetService",
                 "MutateCampaignBudgetsRequest", "mutate_campaign_budgets",
                 customer_id, op)

  results["CampaignBudgetService"] = _probe(_budget)

  # --- CampaignService: rewrite the identical name. ---------------------
  def _campaign():
    op = ads_client.get_type("CampaignOperation")
    campaign = op.update
    campaign.resource_name = campaign_rn
    campaign.name = campaign_name
    op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, campaign._pb))  # pylint: disable=protected-access
    return _send(ads_client, "CampaignService", "MutateCampaignsRequest",
                 "mutate_campaigns", customer_id, op)

  results["CampaignService"] = _probe(_campaign)

  # --- AdGroupService: rewrite the identical name. ----------------------
  ad_group_row = _first_row(ads_client, customer_id, [
      "SELECT ad_group.resource_name, ad_group.name FROM ad_group "
      "WHERE ad_group.status != 'REMOVED' AND campaign.resource_name = '%s' "
      "LIMIT 1" % campaign_rn,
      "SELECT ad_group.resource_name, ad_group.name FROM ad_group "
      "WHERE ad_group.status != 'REMOVED' AND campaign.status = 'PAUSED' "
      "LIMIT 1",
      "SELECT ad_group.resource_name, ad_group.name FROM ad_group "
      "WHERE ad_group.status != 'REMOVED' LIMIT 1",
  ])
  if ad_group_row:
    ad_group_rn = ad_group_row.ad_group.resource_name
    ad_group_name = ad_group_row.ad_group.name
    targets["ad_group"] = {"resource_name": ad_group_rn}

    def _ad_group():
      op = ads_client.get_type("AdGroupOperation")
      group = op.update
      group.resource_name = ad_group_rn
      group.name = ad_group_name
      op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, group._pb))  # pylint: disable=protected-access
      return _send(ads_client, "AdGroupService", "MutateAdGroupsRequest",
                   "mutate_ad_groups", customer_id, op)

    results["AdGroupService"] = _probe(_ad_group)
  else:
    results["AdGroupService"] = {"status": "SKIPPED",
                                 "message": "No ad group in the probe campaign."}

  # --- AdGroupCriterionService: rewrite the identical status. -----------
  criterion_row = _first_row(ads_client, customer_id, [
      "SELECT ad_group_criterion.resource_name, ad_group_criterion.status "
      "FROM keyword_view WHERE ad_group_criterion.status != 'REMOVED' "
      "AND campaign.resource_name = '%s' LIMIT 1" % campaign_rn,
      "SELECT ad_group_criterion.resource_name, ad_group_criterion.status "
      "FROM keyword_view WHERE ad_group_criterion.status != 'REMOVED' "
      "AND campaign.status = 'PAUSED' LIMIT 1",
      "SELECT ad_group_criterion.resource_name, ad_group_criterion.status "
      "FROM keyword_view WHERE ad_group_criterion.status != 'REMOVED' LIMIT 1",
  ])
  if criterion_row:
    criterion_rn = criterion_row.ad_group_criterion.resource_name
    criterion_status = criterion_row.ad_group_criterion.status
    targets["ad_group_criterion"] = {"resource_name": criterion_rn,
                                     "status": criterion_status.name}

    def _criterion():
      op = ads_client.get_type("AdGroupCriterionOperation")
      criterion = op.update
      criterion.resource_name = criterion_rn
      criterion.status = criterion_status
      op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, criterion._pb))  # pylint: disable=protected-access
      return _send(ads_client, "AdGroupCriterionService",
                   "MutateAdGroupCriteriaRequest", "mutate_ad_group_criteria",
                   customer_id, op)

    results["AdGroupCriterionService"] = _probe(_criterion)
  else:
    results["AdGroupCriterionService"] = {
        "status": "SKIPPED",
        "message": "No non-removed keyword in the probe campaign."}

  accepted = [k for k, v in results.items() if v.get("status") == "ACCEPTED"]
  return {
      "customer_id": customer_id,
      "mutations_performed": "none -- every request sent validate_only=true",
      "zero_delta": "every operation rewrote the value it had just read",
      "probe_targets": targets,
      "services_accepted": sorted(accepted),
      "results": results,
  }
