# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Gated keyword mutation tools for Google Ads.

Every keyword mutation goes through propose -> approve -> apply:

1. Caller invokes propose_* with the desired operation + reason.
2. The propose tool validates the target via GAQL (verifies the criterion
   exists, fetches its text for the human-readable block) and writes a
   pending file with a 6-char code.
3. Caller shows the human-readable block to the user. User says
   "approve <CODE>".
4. Caller invokes apply_keyword_mutation(code). The apply tool re-reads
   the pending file, verifies the hash hasn't been tampered with,
   executes the right service call, appends an audit row, and archives
   the pending block.

V1 scope: 9 keyword-flavored operations (5 wrap existing upstream
mutation tools, 4 are new).
"""

from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException

from ads_mcp.coordinator import mcp_server as mcp
from ads_mcp.governance import approval
from ads_mcp.governance import audit
from ads_mcp.tools._ads_api import common_types
from ads_mcp.tools._ads_api import enum_types
from ads_mcp.tools._ads_api import resource_types
from ads_mcp.tools._ads_api import service_types
from ads_mcp.tools.mutations.common import _get_client
from ads_mcp.tools.mutations.common import _handle_google_ads_error
from ads_mcp.tools.mutations.common import _resolve_enum


# -----------------------------------------------------------------------------
# GAQL validation helpers — confirm targets exist before generating a code
# -----------------------------------------------------------------------------


def _gaql_lookup_keyword(
    ads_client, customer_id: str, criterion_resource_name: str
) -> dict[str, str]:
  """Looks up an ad-group keyword by resource name. Returns text + status.

  Raises:
      ToolError: If no matching criterion is found.
  """
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT ad_group_criterion.resource_name, "
      "ad_group_criterion.keyword.text, "
      "ad_group_criterion.keyword.match_type, "
      "ad_group_criterion.status, "
      "ad_group.name "
      "FROM ad_group_criterion "
      f"WHERE ad_group_criterion.resource_name = "
      f"'{criterion_resource_name}' "
      "AND ad_group_criterion.type = 'KEYWORD' "
      "LIMIT 1"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  for row in rows:
    return {
        "text": row.ad_group_criterion.keyword.text,
        "match_type": row.ad_group_criterion.keyword.match_type.name,
        "status": row.ad_group_criterion.status.name,
        "ad_group_name": row.ad_group.name,
    }
  raise ToolError(
      f"No KEYWORD criterion found at {criterion_resource_name}. Verify "
      "the resource name (format: customers/X/adGroupCriteria/Y~Z) and "
      "that the customer_id matches."
  )


def _gaql_lookup_ad_group(
    ads_client, customer_id: str, ad_group_resource_name: str
) -> str:
  """Returns the ad group's name, or raises if it doesn't exist."""
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT ad_group.name "
      "FROM ad_group "
      f"WHERE ad_group.resource_name = '{ad_group_resource_name}' "
      "LIMIT 1"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  for row in rows:
    return row.ad_group.name
  raise ToolError(f"No ad group found at {ad_group_resource_name}.")


def _gaql_lookup_campaign(
    ads_client, customer_id: str, campaign_resource_name: str
) -> str:
  """Returns the campaign's name, or raises if it doesn't exist."""
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT campaign.name "
      "FROM campaign "
      f"WHERE campaign.resource_name = '{campaign_resource_name}' "
      "LIMIT 1"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  for row in rows:
    return row.campaign.name
  raise ToolError(f"No campaign found at {campaign_resource_name}.")


def _gaql_lookup_shared_set(
    ads_client, customer_id: str, shared_set_resource_name: str
) -> str:
  """Returns the shared set's name, or raises if it doesn't exist."""
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT shared_set.name "
      "FROM shared_set "
      f"WHERE shared_set.resource_name = '{shared_set_resource_name}' "
      "LIMIT 1"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  for row in rows:
    return row.shared_set.name
  raise ToolError(
      f"No shared set found at {shared_set_resource_name}. Master "
      "Negative Lists live in shared_set with type=NEGATIVE_KEYWORDS."
  )


# -----------------------------------------------------------------------------
# Propose tools — one per operation type
# -----------------------------------------------------------------------------


def _common_propose_args_doc() -> str:
  return (
      "      reason_code: Why this mutation is being proposed. Must be one\n"
      "          of: underperforming, irrelevant_intent, competitor_term,\n"
      "          low_quality_score, budget_protection,\n"
      "          negative_consolidation, client_request, other.\n"
      "      reason_detail: Free-text elaboration. Required when\n"
      "          reason_code='other'.\n"
      "      client_root: Per-client folder where pending_approvals/ and\n"
      "          client_audit_log.md live. Falls back to env\n"
      "          LO_AGENCY_CLIENT_ROOT.\n"
      "      client_label: Human label shown in the proposal header\n"
      "          (e.g., 'Spine & Health')."
  )


@mcp.tool()
def propose_pause_keyword(
    customer_id: str,
    criterion_resource_name: str,
    reason_code: str,
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes pausing an existing ad-group keyword.

  Validates the criterion exists via GAQL, then writes a human-readable
  approval block to pending_approvals/<code>.md. Returns the block and
  code. Caller should show the block to the user and ask for approval.

  Args:
      customer_id: Google Ads customer ID (digits only).
      criterion_resource_name: Resource name of the keyword to pause
          (format: customers/X/adGroupCriteria/Y~Z).
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)
  info = _gaql_lookup_keyword(
      ads_client, customer_id, criterion_resource_name
  )

  if info["status"] == "PAUSED":
    raise ToolError(
        f"Keyword {info['text']!r} is already PAUSED. No action needed."
    )

  human_line = (
      f"Pause keyword \"{info['text']}\" [{info['match_type']}] in ad "
      f"group \"{info['ad_group_name']}\" (currently {info['status']})"
  )
  spec = {
      "op": "pause_keyword",
      "criterion_resource_name": criterion_resource_name,
      "login_customer_id": login_customer_id,
  }
  return approval.write_proposal(
      root,
      tool_name="pause_keyword",
      customer_id=customer_id,
      operations_human=[human_line],
      reason_code=reason_code,
      reason_detail=reason_detail,
      spec=spec,
      client_label=client_label,
  )


propose_pause_keyword.__doc__ = propose_pause_keyword.__doc__ % (
    _common_propose_args_doc()
)


@mcp.tool()
def propose_enable_keyword(
    customer_id: str,
    criterion_resource_name: str,
    reason_code: str,
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes re-enabling a paused ad-group keyword.

  Same validation flow as propose_pause_keyword.

  Args:
      customer_id: Google Ads customer ID (digits only).
      criterion_resource_name: Resource name of the keyword to enable.
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)
  info = _gaql_lookup_keyword(
      ads_client, customer_id, criterion_resource_name
  )
  if info["status"] == "ENABLED":
    raise ToolError(
        f"Keyword {info['text']!r} is already ENABLED. No action needed."
    )
  human_line = (
      f"Enable keyword \"{info['text']}\" [{info['match_type']}] in ad "
      f"group \"{info['ad_group_name']}\" (currently {info['status']})"
  )
  spec = {
      "op": "enable_keyword",
      "criterion_resource_name": criterion_resource_name,
      "login_customer_id": login_customer_id,
  }
  return approval.write_proposal(
      root,
      tool_name="enable_keyword",
      customer_id=customer_id,
      operations_human=[human_line],
      reason_code=reason_code,
      reason_detail=reason_detail,
      spec=spec,
      client_label=client_label,
  )


propose_enable_keyword.__doc__ = propose_enable_keyword.__doc__ % (
    _common_propose_args_doc()
)


@mcp.tool()
def propose_remove_keyword(
    customer_id: str,
    criterion_resource_name: str,
    reason_code: str,
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes removing (hard-deleting) an ad-group keyword.

  This is destructive — historical performance data on the criterion is
  preserved in reports, but the keyword cannot be re-enabled. Prefer
  propose_pause_keyword unless you're sure.

  Args:
      customer_id: Google Ads customer ID (digits only).
      criterion_resource_name: Resource name of the keyword to remove.
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)
  info = _gaql_lookup_keyword(
      ads_client, customer_id, criterion_resource_name
  )
  human_line = (
      f"REMOVE keyword \"{info['text']}\" [{info['match_type']}] from ad "
      f"group \"{info['ad_group_name']}\" (currently {info['status']}) — "
      "this cannot be undone"
  )
  spec = {
      "op": "remove_keyword",
      "criterion_resource_name": criterion_resource_name,
      "login_customer_id": login_customer_id,
  }
  return approval.write_proposal(
      root,
      tool_name="remove_keyword",
      customer_id=customer_id,
      operations_human=[human_line],
      reason_code=reason_code,
      reason_detail=reason_detail,
      spec=spec,
      client_label=client_label,
  )


propose_remove_keyword.__doc__ = propose_remove_keyword.__doc__ % (
    _common_propose_args_doc()
)


@mcp.tool()
def propose_add_ad_group_negative(
    customer_id: str,
    ad_group_resource_name: str,
    keywords: list[dict[str, str]],
    reason_code: str,
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes adding one or more negative keywords at the ad-group level.

  Validates the ad group exists before generating a code.

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_resource_name: Resource name of the target ad group.
      keywords: List of {'text': str, 'match_type': 'EXACT'|'PHRASE'|'BROAD'}.
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)
  ag_name = _gaql_lookup_ad_group(
      ads_client, customer_id, ad_group_resource_name
  )

  # Validate match types up front so we don't accept bad input.
  normalized: list[dict[str, str]] = []
  for kw in keywords:
    if "text" not in kw or "match_type" not in kw:
      raise ToolError(
          f"Each keyword needs 'text' and 'match_type'. Got: {kw}"
      )
    _resolve_enum(
        enum_types.KeywordMatchTypeEnum.KeywordMatchType,
        kw["match_type"],
        "match_type",
    )
    normalized.append(
        {"text": kw["text"], "match_type": kw["match_type"].upper()}
    )

  human_lines = [
      f'Add negative "{kw["text"]}" [{kw["match_type"]}] to ad group '
      f'"{ag_name}"'
      for kw in normalized
  ]
  spec = {
      "op": "add_ad_group_negative",
      "ad_group_resource_name": ad_group_resource_name,
      "keywords": normalized,
      "login_customer_id": login_customer_id,
  }
  return approval.write_proposal(
      root,
      tool_name="add_ad_group_negative",
      customer_id=customer_id,
      operations_human=human_lines,
      reason_code=reason_code,
      reason_detail=reason_detail,
      spec=spec,
      client_label=client_label,
  )


propose_add_ad_group_negative.__doc__ = (
    propose_add_ad_group_negative.__doc__ % _common_propose_args_doc()
)


@mcp.tool()
def propose_add_campaign_negative(
    customer_id: str,
    campaign_resource_name: str,
    keywords: list[dict[str, str]],
    reason_code: str,
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes adding negative keywords at the campaign level.

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_resource_name: Resource name of the target campaign.
      keywords: List of {'text': str, 'match_type': 'EXACT'|'PHRASE'|'BROAD'}.
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)
  cmp_name = _gaql_lookup_campaign(
      ads_client, customer_id, campaign_resource_name
  )

  normalized: list[dict[str, str]] = []
  for kw in keywords:
    if "text" not in kw or "match_type" not in kw:
      raise ToolError(
          f"Each keyword needs 'text' and 'match_type'. Got: {kw}"
      )
    _resolve_enum(
        enum_types.KeywordMatchTypeEnum.KeywordMatchType,
        kw["match_type"],
        "match_type",
    )
    normalized.append(
        {"text": kw["text"], "match_type": kw["match_type"].upper()}
    )

  human_lines = [
      f'Add negative "{kw["text"]}" [{kw["match_type"]}] to campaign '
      f'"{cmp_name}"'
      for kw in normalized
  ]
  spec = {
      "op": "add_campaign_negative",
      "campaign_resource_name": campaign_resource_name,
      "keywords": normalized,
      "login_customer_id": login_customer_id,
  }
  return approval.write_proposal(
      root,
      tool_name="add_campaign_negative",
      customer_id=customer_id,
      operations_human=human_lines,
      reason_code=reason_code,
      reason_detail=reason_detail,
      spec=spec,
      client_label=client_label,
  )


propose_add_campaign_negative.__doc__ = (
    propose_add_campaign_negative.__doc__ % _common_propose_args_doc()
)


@mcp.tool()
def propose_add_to_shared_negative_list(
    customer_id: str,
    shared_set_resource_name: str,
    keywords: list[dict[str, str]],
    reason_code: str,
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes adding negatives to a shared set (Master Negative List).

  Args:
      customer_id: Google Ads customer ID (digits only).
      shared_set_resource_name: Resource name of the shared set (type
          NEGATIVE_KEYWORDS).
      keywords: List of {'text': str, 'match_type': 'EXACT'|'PHRASE'|'BROAD'}.
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)
  set_name = _gaql_lookup_shared_set(
      ads_client, customer_id, shared_set_resource_name
  )

  normalized: list[dict[str, str]] = []
  for kw in keywords:
    if "text" not in kw or "match_type" not in kw:
      raise ToolError(
          f"Each keyword needs 'text' and 'match_type'. Got: {kw}"
      )
    _resolve_enum(
        enum_types.KeywordMatchTypeEnum.KeywordMatchType,
        kw["match_type"],
        "match_type",
    )
    normalized.append(
        {"text": kw["text"], "match_type": kw["match_type"].upper()}
    )

  human_lines = [
      f'Add "{kw["text"]}" [{kw["match_type"]}] to shared list '
      f'"{set_name}"'
      for kw in normalized
  ]
  spec = {
      "op": "add_to_shared_negative_list",
      "shared_set_resource_name": shared_set_resource_name,
      "keywords": normalized,
      "login_customer_id": login_customer_id,
  }
  return approval.write_proposal(
      root,
      tool_name="add_to_shared_negative_list",
      customer_id=customer_id,
      operations_human=human_lines,
      reason_code=reason_code,
      reason_detail=reason_detail,
      spec=spec,
      client_label=client_label,
  )


propose_add_to_shared_negative_list.__doc__ = (
    propose_add_to_shared_negative_list.__doc__ % _common_propose_args_doc()
)


# -----------------------------------------------------------------------------
# Apply / reject / list — operate on existing proposals
# -----------------------------------------------------------------------------


def _execute_pause_or_enable(
    ads_client, customer_id: str, spec: dict[str, Any], target_status: str
) -> dict[str, Any]:
  service = ads_client.get_service("AdGroupCriterionService")
  status_enum = (
      enum_types.AdGroupCriterionStatusEnum.AdGroupCriterionStatus
  )
  criterion = resource_types.AdGroupCriterion(
      resource_name=spec["criterion_resource_name"],
      status=status_enum[target_status],
  )
  operation = service_types.AdGroupCriterionOperation(update=criterion)
  operation.update_mask.paths.append("status")
  try:
    response = service.mutate_ad_group_criteria(
        customer_id=customer_id, operations=[operation]
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return {"updated": [r.resource_name for r in response.results]}


def _execute_remove_keyword(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
  service = ads_client.get_service("AdGroupCriterionService")
  operation = service_types.AdGroupCriterionOperation(
      remove=spec["criterion_resource_name"]
  )
  try:
    response = service.mutate_ad_group_criteria(
        customer_id=customer_id, operations=[operation]
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return {"removed": [r.resource_name for r in response.results]}


def _execute_add_ad_group_negative(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
  service = ads_client.get_service("AdGroupCriterionService")
  operations = []
  for kw in spec["keywords"]:
    match_type = _resolve_enum(
        enum_types.KeywordMatchTypeEnum.KeywordMatchType,
        kw["match_type"],
        "match_type",
    )
    criterion = resource_types.AdGroupCriterion(
        ad_group=spec["ad_group_resource_name"],
        negative=True,
        keyword=common_types.KeywordInfo(
            text=kw["text"], match_type=match_type
        ),
    )
    operations.append(
        service_types.AdGroupCriterionOperation(create=criterion)
    )
  try:
    response = service.mutate_ad_group_criteria(
        customer_id=customer_id, operations=operations
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return {"created": [r.resource_name for r in response.results]}


def _execute_add_campaign_negative(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
  service = ads_client.get_service("CampaignCriterionService")
  operations = []
  for kw in spec["keywords"]:
    match_type = _resolve_enum(
        enum_types.KeywordMatchTypeEnum.KeywordMatchType,
        kw["match_type"],
        "match_type",
    )
    criterion = resource_types.CampaignCriterion(
        campaign=spec["campaign_resource_name"],
        negative=True,
        keyword=common_types.KeywordInfo(
            text=kw["text"], match_type=match_type
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
  return {"created": [r.resource_name for r in response.results]}


def _execute_add_to_shared_negative_list(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
  service = ads_client.get_service("SharedCriterionService")
  operations = []
  for kw in spec["keywords"]:
    match_type = _resolve_enum(
        enum_types.KeywordMatchTypeEnum.KeywordMatchType,
        kw["match_type"],
        "match_type",
    )
    criterion = resource_types.SharedCriterion(
        shared_set=spec["shared_set_resource_name"],
        keyword=common_types.KeywordInfo(
            text=kw["text"], match_type=match_type
        ),
    )
    operations.append(
        service_types.SharedCriterionOperation(create=criterion)
    )
  try:
    response = service.mutate_shared_criteria(
        customer_id=customer_id, operations=operations
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return {"created": [r.resource_name for r in response.results]}


_DISPATCH = {
    "pause_keyword": lambda c, cid, s: _execute_pause_or_enable(
        c, cid, s, "PAUSED"
    ),
    "enable_keyword": lambda c, cid, s: _execute_pause_or_enable(
        c, cid, s, "ENABLED"
    ),
    "remove_keyword": _execute_remove_keyword,
    "add_ad_group_negative": _execute_add_ad_group_negative,
    "add_campaign_negative": _execute_add_campaign_negative,
    "add_to_shared_negative_list": _execute_add_to_shared_negative_list,
}


def register_executor(op: str, executor) -> None:
  """Registers an executor for a new gated operation type.

  Lets sibling modules add capabilities (bidding, assets) that flow
  through this exact same propose -> approve -> apply -> audit path,
  rather than standing up a second, weaker gate beside it.

  Args:
      op: The `spec["op"]` discriminator written by the propose tool.
      executor: Callable (ads_client, customer_id, spec) -> dict.

  Raises:
      ValueError: If `op` is already registered. Silent replacement would
          let one module hijack another's approved proposals.
  """
  if op in _DISPATCH:
    raise ValueError(f"Executor for op {op!r} is already registered.")
  _DISPATCH[op] = executor


def _apply_proposal(code: str, client_root: str | None) -> dict[str, Any]:
  """Shared body for the apply tools. See apply_mutation for behavior."""
  root = audit.resolve_client_root(client_root)
  proposal = approval.read_proposal(root, code)
  tool_name = proposal["tool"]
  customer_id = proposal["customer_id"]
  spec = proposal["spec"]
  op = spec["op"]

  if op not in _DISPATCH:
    raise ToolError(
        f"Unknown operation in spec: {op}. Pending file may be from a "
        "newer version of the MCP than is currently running."
    )

  ads_client = _get_client(spec.get("login_customer_id"))
  try:
    api_result = _DISPATCH[op](ads_client, customer_id, spec)
    outcome = "applied"
  except Exception as e:  # pylint: disable=broad-except
    # Still write an audit row for the failure, then re-raise.
    audit.append_audit_row(
        root,
        code=code.upper(),
        customer_id=customer_id,
        tool_name=tool_name,
        reason_code="(applied-failure)",
        reason_detail=str(e)[:200],
        operations_summary=op,
        api_result="FAILED",
        outcome="failed",
    )
    raise

  # Successful path — pull reason_code/detail from the archived file
  # for the audit row. The hash already verified the spec, but the
  # human-readable header isn't in the spec, so we re-read the file.
  pending_text = (root / approval.PENDING_DIRNAME / f"{code.upper()}.md").read_text(
      encoding="utf-8"
  )
  reason_code, reason_detail = _extract_reason(pending_text)
  ops_summary = _extract_ops_summary(pending_text)

  archived_path = approval.archive_proposal(
      root, code, outcome="applied"
  )
  audit.append_audit_row(
      root,
      code=code.upper(),
      customer_id=customer_id,
      tool_name=tool_name,
      reason_code=reason_code,
      reason_detail=reason_detail,
      operations_summary=ops_summary,
      api_result=str(api_result),
      outcome=outcome,
  )
  return {
      "outcome": outcome,
      "archived_to": str(archived_path),
      "api_result": api_result,
  }


@mcp.tool()
def apply_mutation(
    code: str,
    client_root: str | None = None,
) -> dict[str, Any]:
  """Applies any previously proposed and approved mutation.

  Reads pending_approvals/<code>.md, verifies its hash, executes the
  Google Ads API call, appends an audit row, and archives the pending
  file to applied_approvals/. Handles every registered operation type,
  including bidding and asset operations.

  Args:
      code: The 6-char code from a propose_* tool's output (e.g.,
          'A7K2P3'). User normally provides this by saying
          'approve A7K2P3'.
      client_root: Per-client folder. Falls back to env
          LO_AGENCY_CLIENT_ROOT.
  """
  return _apply_proposal(code, client_root)


@mcp.tool()
def apply_keyword_mutation(
    code: str,
    client_root: str | None = None,
) -> dict[str, Any]:
  """Applies a previously proposed keyword mutation.

  Retained for continuity with existing runbooks. Identical to
  apply_mutation, which is the preferred name now that the gate also
  covers bidding and asset operations.

  Args:
      code: The 6-char code from a propose_* tool's output.
      client_root: Per-client folder. Falls back to env
          LO_AGENCY_CLIENT_ROOT.
  """
  return _apply_proposal(code, client_root)


@mcp.tool()
def reject_keyword_mutation(
    code: str,
    note: str,
    client_root: str | None = None,
) -> dict[str, str]:
  """Rejects a pending proposal — moves it to rejected_approvals/.

  Args:
      code: The 6-char proposal code to reject.
      note: Required free-text reason for rejection (becomes part of the
          archived file).
      client_root: Per-client folder. Falls back to env
          LO_AGENCY_CLIENT_ROOT.
  """
  if not note.strip():
    raise ToolError("Rejection note is required — explain why.")
  root = audit.resolve_client_root(client_root)
  # We deliberately don't verify the hash here — a user might reject a
  # tampered/stale block, and that should still archive it cleanly.
  archived = approval.archive_proposal(
      root, code, outcome="rejected", note=note
  )
  return {"outcome": "rejected", "archived_to": str(archived)}


@mcp.tool()
def list_pending_approvals(
    client_root: str | None = None,
) -> dict[str, Any]:
  """Lists every pending proposal in the client's queue.

  Args:
      client_root: Per-client folder. Falls back to env
          LO_AGENCY_CLIENT_ROOT.
  """
  root = audit.resolve_client_root(client_root)
  return {"pending": approval.list_pending(root)}


# -----------------------------------------------------------------------------
# Small helpers for extracting reason/ops from the proposal markdown
# (only used at apply time for the audit row).
# -----------------------------------------------------------------------------


def _extract_reason(text: str) -> tuple[str, str | None]:
  """Pulls reason_code and reason_detail back out of the proposal text."""
  for line in text.splitlines():
    if line.startswith("- **Reason:**"):
      body = line.split("**Reason:**", 1)[1].strip()
      # Format: `code` — detail   OR just `code`
      if " — " in body:
        code_part, detail = body.split(" — ", 1)
        return code_part.strip().strip("`"), detail.strip()
      return body.strip().strip("`"), None
  return "(unknown)", None


def _extract_ops_summary(text: str) -> str:
  """First operation line, used as a compact summary for the audit row."""
  in_ops = False
  for line in text.splitlines():
    if line.startswith("## Operations"):
      in_ops = True
      continue
    if in_ops and line.strip().startswith("- "):
      return line.strip()[2:]
  return "(no ops summary)"
