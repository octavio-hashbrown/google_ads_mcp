# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Gated bidding-strategy mutation: Search campaign -> Manual CPC.

Deliberately narrow. This is not a generic "set any bidding strategy"
tool: each strategy has its own preconditions, learning-phase cost and
rollback story, so they earn their own reviewed tools rather than one
switch with a string argument.

Registers into the shared approval gate in `mutations_gated`, so a
bidding change travels the same propose -> approve -> apply -> audit
path as every other spend-affecting mutation.

No campaign-level CPC ceiling is set or invented here. Under Manual CPC
the governing bids are the ad-group `cpc_bid_micros` values, which this
module reads and shows in the proposal but never writes.
"""

from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2

from ads_mcp.coordinator import mcp_server as mcp
from ads_mcp.governance import approval
from ads_mcp.governance import audit
from ads_mcp.tools._ads_api import resource_types
from ads_mcp.tools._ads_api import service_types
from ads_mcp.tools.mutations.common import _get_client
from ads_mcp.tools.mutations.common import _handle_google_ads_error
from ads_mcp.tools import mutations_gated


TARGET_STRATEGY = "MANUAL_CPC"


def _read_campaign_bidding(
    ads_client, customer_id: str, campaign_resource_name: str
) -> dict[str, Any]:
  """Reads the campaign's current bidding posture.

  Raises:
      ToolError: If the campaign does not exist.
  """
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT campaign.name, campaign.status, "
      "campaign.bidding_strategy_type, campaign.bidding_strategy, "
      "campaign.advertising_channel_type, "
      "campaign.maximize_conversions.target_cpa_micros, "
      "campaign.target_cpa.target_cpa_micros, "
      "campaign.target_spend.cpc_bid_ceiling_micros "
      "FROM campaign "
      f"WHERE campaign.resource_name = '{campaign_resource_name}' "
      "LIMIT 1"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  for row in rows:
    return {
        "name": row.campaign.name,
        "status": row.campaign.status.name,
        "bidding_strategy_type": row.campaign.bidding_strategy_type.name,
        "portfolio_strategy": row.campaign.bidding_strategy,
        "channel_type": row.campaign.advertising_channel_type.name,
        "maximize_conversions_target_cpa_micros": (
            row.campaign.maximize_conversions.target_cpa_micros
        ),
        "target_cpa_micros": row.campaign.target_cpa.target_cpa_micros,
    }
  raise ToolError(f"No campaign found at {campaign_resource_name}.")


def _read_ad_group_bids(
    ads_client, customer_id: str, campaign_resource_name: str
) -> list[dict[str, Any]]:
  """Reads ad-group CPC bids — these govern once Manual CPC is active."""
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT ad_group.name, ad_group.status, ad_group.cpc_bid_micros "
      "FROM ad_group "
      f"WHERE ad_group.campaign = '{campaign_resource_name}' "
      "AND ad_group.status != 'REMOVED'"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return [
      {
          "name": row.ad_group.name,
          "status": row.ad_group.status.name,
          "cpc_bid_micros": row.ad_group.cpc_bid_micros,
      }
      for row in rows
  ]


@mcp.tool()
def propose_set_manual_cpc(
    customer_id: str,
    campaign_resource_name: str,
    reason_code: str,
    enhanced_cpc_enabled: bool = False,
    allow_enabled_campaign: bool = False,
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes switching a Search campaign's bidding to Manual CPC.

  Reads the current strategy first and writes an explicit BEFORE -> AFTER
  line into the approval block, so the human approves a transition rather
  than a destination.

  Refuses outright when the campaign is ENABLED unless
  `allow_enabled_campaign=True`. That flag is recorded in the
  hash-verified spec and shown in the proposal, so authorization to touch
  a serving campaign is explicit, visible and tamper-evident.

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_resource_name: Full resource name of the campaign
          (format: customers/X/campaigns/Y).
      enhanced_cpc_enabled: Legacy Enhanced CPC flag. Google has sunset
          eCPC for Search; leave False unless you have a specific reason.
      allow_enabled_campaign: Required to be True before this tool will
          propose a bidding change on a campaign that is currently
          serving. Defaults to False.
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)

  before = _read_campaign_bidding(
      ads_client, customer_id, campaign_resource_name
  )

  if before["bidding_strategy_type"] == TARGET_STRATEGY:
    raise ToolError(
        f"Campaign \"{before['name']}\" is already on {TARGET_STRATEGY}. "
        "Nothing to propose."
    )

  if before["portfolio_strategy"]:
    raise ToolError(
        f"Campaign \"{before['name']}\" uses the portfolio bidding "
        f"strategy {before['portfolio_strategy']}. Detaching a campaign "
        "from a portfolio strategy affects every other campaign sharing "
        "it, so it is out of scope for this tool. Detach it deliberately "
        "first."
    )

  if before["status"] == "ENABLED" and not allow_enabled_campaign:
    raise ToolError(
        f"Campaign \"{before['name']}\" is ENABLED and serving. A bidding "
        "change on a live campaign resets learning and can move spend "
        "immediately. Re-call with allow_enabled_campaign=True only if "
        "that is genuinely intended; the flag is recorded in the approval "
        "block for the record."
    )

  ad_groups = _read_ad_group_bids(
      ads_client, customer_id, campaign_resource_name
  )

  human_lines = [
      f'Campaign "{before["name"]}" ({before["status"]})',
      f"Bidding BEFORE: {before['bidding_strategy_type']}",
      f"Bidding AFTER:  {TARGET_STRATEGY} "
      f"(enhanced_cpc_enabled={enhanced_cpc_enabled})",
  ]

  before_tcpa = before["maximize_conversions_target_cpa_micros"] or (
      before["target_cpa_micros"]
  )
  if before_tcpa:
    human_lines.append(
        f"Target CPA of ${before_tcpa / 1_000_000:.2f} currently in force "
        "will STOP applying once Manual CPC is active."
    )

  if before["status"] == "ENABLED":
    human_lines.append(
        "WARNING: this campaign is ENABLED and serving. Approving this "
        "changes bidding on live traffic."
    )

  if ad_groups:
    human_lines.append(
        "Once Manual CPC is active these ad-group bids govern "
        "(read-only here, not modified by this tool):"
    )
    for ag in ad_groups:
      bid = (
          f"${ag['cpc_bid_micros'] / 1_000_000:.2f}"
          if ag["cpc_bid_micros"]
          else "NOT SET"
      )
      human_lines.append(f"    {ag['name']} [{ag['status']}] -> {bid}")
    if any(not ag["cpc_bid_micros"] for ag in ad_groups):
      human_lines.append(
          "WARNING: at least one ad group has no CPC bid set. Review "
          "those bids before this campaign serves on Manual CPC."
      )

  spec = {
      "op": "set_manual_cpc",
      "campaign_resource_name": campaign_resource_name,
      "enhanced_cpc_enabled": enhanced_cpc_enabled,
      "allow_enabled_campaign": allow_enabled_campaign,
      "before_bidding_strategy_type": before["bidding_strategy_type"],
      "before_status": before["status"],
      "login_customer_id": login_customer_id,
  }
  return approval.write_proposal(
      root,
      tool_name="set_manual_cpc",
      customer_id=customer_id,
      operations_human=human_lines,
      reason_code=reason_code,
      reason_detail=reason_detail,
      spec=spec,
      client_label=client_label,
  )


propose_set_manual_cpc.__doc__ = (
    propose_set_manual_cpc.__doc__
    % mutations_gated._common_propose_args_doc()  # pylint: disable=protected-access
)


def build_manual_cpc_operation(
    campaign_resource_name: str, enhanced_cpc_enabled: bool
):
  """Builds the CampaignOperation that switches a campaign to Manual CPC.

  Split out so the exact request shape can be validated against the live
  API with validate_only, without going near a real mutate.

  The mask names the leaf field, not the `manual_cpc` message. A bare
  "manual_cpc" path is rejected by the API with FieldMaskError, "The
  field mask updated a field with subfields" — verified live. Setting the
  leaf still selects manual_cpc within the bidding-strategy oneof, which
  is what performs the switch.
  """
  campaign = resource_types.Campaign(resource_name=campaign_resource_name)
  campaign.manual_cpc.enhanced_cpc_enabled = enhanced_cpc_enabled
  operation = service_types.CampaignOperation(update=campaign)
  operation.update_mask.CopyFrom(
      field_mask_pb2.FieldMask(paths=["manual_cpc.enhanced_cpc_enabled"])
  )
  return operation


def _execute_set_manual_cpc(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
  """Applies the approved Manual CPC switch, after re-checking for drift."""
  campaign_resource_name = spec["campaign_resource_name"]
  current = _read_campaign_bidding(
      ads_client, customer_id, campaign_resource_name
  )

  # Idempotency: a re-apply of the same approved change is a no-op, not
  # a second mutation.
  if current["bidding_strategy_type"] == TARGET_STRATEGY:
    return {
        "outcome": "no_op",
        "detail": f"Campaign is already on {TARGET_STRATEGY}.",
        "before": current["bidding_strategy_type"],
        "after": current["bidding_strategy_type"],
    }

  # Drift guard: the account must still look like it did at propose time.
  if current["bidding_strategy_type"] != spec["before_bidding_strategy_type"]:
    raise ToolError(
        "Bidding strategy changed since this was proposed: approved a "
        f"switch from {spec['before_bidding_strategy_type']}, but the "
        f"campaign is now on {current['bidding_strategy_type']}. Re-run "
        "propose_set_manual_cpc so the human approves the real transition."
    )

  if current["status"] == "ENABLED" and not spec["allow_enabled_campaign"]:
    raise ToolError(
        f"Campaign \"{current['name']}\" is now ENABLED, but this approval "
        "did not authorize mutating a serving campaign "
        "(allow_enabled_campaign was False when it was approved). "
        "Refusing. Re-propose with the flag set if this is intended."
    )

  service = ads_client.get_service("CampaignService")
  operation = build_manual_cpc_operation(
      campaign_resource_name, spec["enhanced_cpc_enabled"]
  )

  try:
    service.mutate_campaigns(
        customer_id=customer_id, operations=[operation]
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  after = _read_campaign_bidding(
      ads_client, customer_id, campaign_resource_name
  )
  if after["bidding_strategy_type"] != TARGET_STRATEGY:
    raise ToolError(
        "Post-apply verification failed: expected "
        f"{TARGET_STRATEGY}, read back "
        f"{after['bidding_strategy_type']}."
    )
  return {
      "outcome": "applied",
      "campaign": after["name"],
      "before": current["bidding_strategy_type"],
      "after": after["bidding_strategy_type"],
      "verified_by": "GAQL read-back of campaign.bidding_strategy_type",
  }


mutations_gated.register_executor("set_manual_cpc", _execute_set_manual_cpc)
