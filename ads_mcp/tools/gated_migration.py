# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Gated composite campaign migration.

A campaign migration is not a list of independent edits. Moving traffic
out of one ad group and into a new campaign only makes sense if the bids,
the budgets, the pause and the enable all land together. Applied
piecemeal the same operations produce states nobody approved: a campaign
enabled at placeholder bids, or a source ad group paused while the
replacement is still dark.

So this module deliberately exposes ONE gated entry point that carries
the whole package, and executes it through `GoogleAdsService.Mutate` as a
single heterogeneous request with `partial_failure=False`. Google applies
all operations or none. There is no code path here that applies part of a
migration, and no standalone tool that could be used to apply one leg of
it outside the approved package.

The five underlying capabilities (ad-group CPC, campaign budget amount,
campaign status, ad-group status, campaign language criterion) live as
narrow, individually tested builders. They are pure functions returning
one `MutateOperation` each, which is what makes the composite auditable:
the operation list can be inspected and scope-checked before it is sent.
"""

import os
from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2

from ads_mcp.coordinator import mcp_server as mcp
from ads_mcp.governance import approval
from ads_mcp.governance import audit
from ads_mcp.tools._ads_api import common_types
from ads_mcp.tools._ads_api import enum_types
from ads_mcp.tools._ads_api import resource_types
from ads_mcp.tools._ads_api import service_types
from ads_mcp.tools.mutations.common import _get_client
from ads_mcp.tools.mutations.common import _handle_google_ads_error
from ads_mcp.tools import mutations_gated


OP = "campaign_migration"

# Every mask names a leaf field. A mask naming a message with subfields is
# rejected by the API with FieldMaskError ("The field mask updated a field
# with subfields") — verified live during the Manual CPC build.
MASK_AD_GROUP_CPC = ["cpc_bid_micros"]
MASK_BUDGET_AMOUNT = ["amount_micros"]
MASK_CAMPAIGN_STATUS = ["status"]
MASK_AD_GROUP_STATUS = ["status"]
MASK_AD_GROUP_CRITERION_STATUS = ["status"]

_ALLOWED_STATUSES = ("ENABLED", "PAUSED")

# Campaigns this tooling must never be able to touch, whatever a spec
# says. Growth is out of scope for the Brand migration by owner decision;
# encoding it here means a hand-edited spec cannot reach it either.
_PROTECTED_CAMPAIGN_ENV_VAR = "LO_MIGRATION_PROTECTED_CAMPAIGNS"
_DEFAULT_PROTECTED_CAMPAIGN_IDS = frozenset({"24130111343"})

# Operation kinds this module is allowed to emit. Anything else in a
# built operation list is a bug or a tampered spec, and is refused before
# the request is sent. Conversion actions, ads, assets, geo targeting and
# network settings are absent by construction — there is no builder for
# them here, and the scope check rejects them if one ever appears.
_ALLOWED_OPERATION_KINDS = frozenset({
    "ad_group_operation",
    "campaign_budget_operation",
    "campaign_operation",
    "campaign_criterion_operation",
    "ad_group_criterion_operation",
})


def protected_campaign_ids(explicit: list[str] | None = None) -> set[str]:
  """Campaign IDs this tooling refuses to touch.

  Union of the built-in default, anything in the environment override and
  any caller-supplied additions. Union, never replacement: an override
  can widen the protected set but cannot unprotect Growth.
  """
  ids = set(_DEFAULT_PROTECTED_CAMPAIGN_IDS)
  env = os.environ.get(_PROTECTED_CAMPAIGN_ENV_VAR, "")
  ids.update(part.strip() for part in env.split(",") if part.strip())
  ids.update(str(i).strip() for i in (explicit or []) if str(i).strip())
  return ids


def _campaign_id(resource_name: str) -> str:
  """Extracts the campaign ID from a campaign resource name."""
  return resource_name.rsplit("/", 1)[-1]


def _usd(micros: int) -> str:
  return f"${micros / 1_000_000:.2f}"


# -----------------------------------------------------------------------------
# Operation builders — one narrow capability each, pure, no I/O
# -----------------------------------------------------------------------------


def build_ad_group_cpc_operation(
    ad_group_resource_name: str, cpc_bid_micros: int
) -> Any:
  """Capability 1: update an existing AdGroup.cpc_bid_micros."""
  if cpc_bid_micros <= 0:
    raise ToolError(
        f"cpc_bid_micros must be positive, got {cpc_bid_micros}. A zero or "
        "negative bid is never an intended migration input."
    )
  ad_group = resource_types.AdGroup(
      resource_name=ad_group_resource_name, cpc_bid_micros=cpc_bid_micros
  )
  operation = service_types.AdGroupOperation(update=ad_group)
  operation.update_mask.CopyFrom(
      field_mask_pb2.FieldMask(paths=MASK_AD_GROUP_CPC)
  )
  return service_types.MutateOperation(ad_group_operation=operation)


def build_campaign_budget_amount_operation(
    budget_resource_name: str, amount_micros: int
) -> Any:
  """Capability 2: update an existing CampaignBudget.amount_micros.

  Updates only. There is no create path here: a migration that invents a
  new budget resource silently orphans the old one and its history.
  """
  if amount_micros <= 0:
    raise ToolError(
        f"amount_micros must be positive, got {amount_micros}."
    )
  budget = resource_types.CampaignBudget(
      resource_name=budget_resource_name, amount_micros=amount_micros
  )
  operation = service_types.CampaignBudgetOperation(update=budget)
  operation.update_mask.CopyFrom(
      field_mask_pb2.FieldMask(paths=MASK_BUDGET_AMOUNT)
  )
  return service_types.MutateOperation(campaign_budget_operation=operation)


def build_campaign_status_operation(
    campaign_resource_name: str, status: str
) -> Any:
  """Capability 3: set a campaign to ENABLED or PAUSED.

  REMOVED is deliberately unreachable — campaign deletion is not a
  migration step and must not be one keystroke away from one.
  """
  if status not in _ALLOWED_STATUSES:
    raise ToolError(
        f"Campaign status must be one of {_ALLOWED_STATUSES}, got {status!r}."
    )
  status_enum = enum_types.CampaignStatusEnum.CampaignStatus
  campaign = resource_types.Campaign(
      resource_name=campaign_resource_name, status=status_enum[status]
  )
  operation = service_types.CampaignOperation(update=campaign)
  operation.update_mask.CopyFrom(
      field_mask_pb2.FieldMask(paths=MASK_CAMPAIGN_STATUS)
  )
  return service_types.MutateOperation(campaign_operation=operation)


def build_ad_group_status_operation(
    ad_group_resource_name: str, status: str
) -> Any:
  """Capability 4: set an ad group to ENABLED or PAUSED."""
  if status not in _ALLOWED_STATUSES:
    raise ToolError(
        f"Ad group status must be one of {_ALLOWED_STATUSES}, got {status!r}."
    )
  status_enum = enum_types.AdGroupStatusEnum.AdGroupStatus
  ad_group = resource_types.AdGroup(
      resource_name=ad_group_resource_name, status=status_enum[status]
  )
  operation = service_types.AdGroupOperation(update=ad_group)
  operation.update_mask.CopyFrom(
      field_mask_pb2.FieldMask(paths=MASK_AD_GROUP_STATUS)
  )
  return service_types.MutateOperation(ad_group_operation=operation)


def build_campaign_language_operation(
    campaign_resource_name: str, language_constant: str
) -> Any:
  """Capability 5: add a LANGUAGE criterion to a campaign.

  Narrow to LANGUAGE. The same CampaignCriterion resource carries geo
  targeting, negatives, ad schedules and device modifiers; building only
  the language variant here means this tool cannot express a geo or
  negative change even by accident.
  """
  if not language_constant.startswith("languageConstants/"):
    raise ToolError(
        "language_constant must look like 'languageConstants/1000', got "
        f"{language_constant!r}."
    )
  criterion = resource_types.CampaignCriterion(
      campaign=campaign_resource_name,
      language=common_types.LanguageInfo(language_constant=language_constant),
  )
  operation = service_types.CampaignCriterionOperation(create=criterion)
  return service_types.MutateOperation(campaign_criterion_operation=operation)


def build_ad_group_criterion_status_operation(
    criterion_resource_name: str, status: str
) -> Any:
  """Keyword pause/enable, expressed as a MutateOperation.

  The existing `pause_keyword` gate mutates through AdGroupCriterionService
  directly, which cannot join a heterogeneous atomic request. Same field,
  same mask, same semantics — rebuilt in MutateOperation form so the
  keyword pauses land inside the migration transaction rather than beside
  it.
  """
  if status not in _ALLOWED_STATUSES:
    raise ToolError(
        f"Criterion status must be one of {_ALLOWED_STATUSES}, got {status!r}."
    )
  status_enum = enum_types.AdGroupCriterionStatusEnum.AdGroupCriterionStatus
  criterion = resource_types.AdGroupCriterion(
      resource_name=criterion_resource_name, status=status_enum[status]
  )
  operation = service_types.AdGroupCriterionOperation(update=criterion)
  operation.update_mask.CopyFrom(
      field_mask_pb2.FieldMask(paths=MASK_AD_GROUP_CRITERION_STATUS)
  )
  return service_types.MutateOperation(ad_group_criterion_operation=operation)


# -----------------------------------------------------------------------------
# Scope enforcement
# -----------------------------------------------------------------------------


def operation_kind(mutate_operation) -> str:
  """Returns which oneof member a MutateOperation carries."""
  return mutate_operation._pb.WhichOneof("operation")  # pylint: disable=protected-access


def assert_operations_within_scope(
    operations: list[Any],
    allowed_resource_names: set[str],
    protected_ids: set[str],
) -> None:
  """Refuses any operation outside the approved allowlist.

  This is the last line before the request is sent, and it is deliberately
  paranoid: it re-derives what each operation targets from the built proto
  rather than trusting the spec that produced it. A tampered pending file
  that named a Growth ad group, a conversion action or an RSA would be
  caught here even if every earlier check were bypassed.

  Raises:
      ToolError: On an unexpected operation kind, an out-of-allowlist
          target, or any target belonging to a protected campaign.
  """
  for i, mo in enumerate(operations):
    kind = operation_kind(mo)
    if kind not in _ALLOWED_OPERATION_KINDS:
      raise ToolError(
          f"Operation {i} is a {kind}, which this migration tooling is not "
          "permitted to emit. Refusing the whole package."
      )

    if kind == "ad_group_operation":
      target = mo.ad_group_operation.update.resource_name
    elif kind == "campaign_budget_operation":
      target = mo.campaign_budget_operation.update.resource_name
    elif kind == "campaign_operation":
      target = mo.campaign_operation.update.resource_name
    elif kind == "ad_group_criterion_operation":
      target = mo.ad_group_criterion_operation.update.resource_name
    else:  # campaign_criterion_operation — a create, keyed by its campaign
      create = mo.campaign_criterion_operation.create
      if not create.language.language_constant:
        raise ToolError(
            f"Operation {i} is a campaign criterion that is not a LANGUAGE "
            "criterion. This tooling may not change geo targeting, "
            "negatives, devices or ad schedules."
        )
      target = create.campaign

    if target not in allowed_resource_names:
      raise ToolError(
          f"Operation {i} targets {target}, which is not in the approved "
          "resource allowlist for this migration. Refusing."
      )

    for pid in protected_ids:
      if f"/campaigns/{pid}" in target:
        raise ToolError(
            f"Operation {i} targets protected campaign {pid}. This tooling "
            "is incapable of mutating it. Refusing."
        )


# -----------------------------------------------------------------------------
# Live state reader — feeds both propose-time framing and apply-time guards
# -----------------------------------------------------------------------------


def _search(ads_client, customer_id: str, query: str) -> list[Any]:
  service = ads_client.get_service("GoogleAdsService")
  try:
    return list(service.search(customer_id=customer_id, query=query))
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return []


def read_campaign_state(
    ads_client, customer_id: str, campaign_resource_name: str
) -> dict[str, Any]:
  """Reads the guard-relevant posture of one campaign."""
  rows = _search(
      ads_client,
      customer_id,
      "SELECT campaign.name, campaign.status, campaign.bidding_strategy_type, "
      "campaign.maximize_conversions.target_cpa_micros, "
      "campaign_budget.resource_name, campaign_budget.amount_micros, "
      "campaign_budget.reference_count, campaign_budget.explicitly_shared, "
      "campaign.selective_optimization.conversion_actions "
      f"FROM campaign WHERE campaign.resource_name = "
      f"'{campaign_resource_name}' LIMIT 1",
  )
  if not rows:
    raise ToolError(f"No campaign found at {campaign_resource_name}.")
  row = rows[0]
  return {
      "name": row.campaign.name,
      "status": row.campaign.status.name,
      "bidding_strategy_type": row.campaign.bidding_strategy_type.name,
      "tcpa_micros": row.campaign.maximize_conversions.target_cpa_micros,
      "budget_resource_name": row.campaign_budget.resource_name,
      "budget_amount_micros": row.campaign_budget.amount_micros,
      "budget_reference_count": row.campaign_budget.reference_count,
      "budget_explicitly_shared": row.campaign_budget.explicitly_shared,
      "selective_optimization": list(
          row.campaign.selective_optimization.conversion_actions
      ),
  }


def read_ad_group_state(
    ads_client, customer_id: str, ad_group_resource_name: str
) -> dict[str, Any]:
  rows = _search(
      ads_client,
      customer_id,
      "SELECT ad_group.name, ad_group.status, ad_group.cpc_bid_micros "
      f"FROM ad_group WHERE ad_group.resource_name = "
      f"'{ad_group_resource_name}' LIMIT 1",
  )
  if not rows:
    raise ToolError(f"No ad group found at {ad_group_resource_name}.")
  row = rows[0]
  return {
      "name": row.ad_group.name,
      "status": row.ad_group.status.name,
      "cpc_bid_micros": row.ad_group.cpc_bid_micros,
  }


def read_criterion_state(
    ads_client, customer_id: str, criterion_resource_name: str
) -> dict[str, Any] | None:
  rows = _search(
      ads_client,
      customer_id,
      "SELECT ad_group_criterion.resource_name, "
      "ad_group_criterion.keyword.text, "
      "ad_group_criterion.keyword.match_type, "
      "ad_group_criterion.status, ad_group_criterion.negative, "
      "ad_group.name FROM ad_group_criterion "
      f"WHERE ad_group_criterion.resource_name = "
      f"'{criterion_resource_name}' LIMIT 1",
  )
  if not rows:
    return None
  row = rows[0]
  return {
      "resource_name": row.ad_group_criterion.resource_name,
      "text": row.ad_group_criterion.keyword.text,
      "match_type": row.ad_group_criterion.keyword.match_type.name,
      "status": row.ad_group_criterion.status.name,
      "negative": row.ad_group_criterion.negative,
      "ad_group_name": row.ad_group.name,
  }


def read_campaign_languages(
    ads_client, customer_id: str, campaign_resource_name: str
) -> list[str]:
  rows = _search(
      ads_client,
      customer_id,
      "SELECT campaign_criterion.language.language_constant "
      "FROM campaign_criterion "
      f"WHERE campaign.resource_name = '{campaign_resource_name}' "
      "AND campaign_criterion.type = 'LANGUAGE'",
  )
  return [
      r.campaign_criterion.language.language_constant
      for r in rows
      if r.campaign_criterion.language.language_constant
  ]


def read_conversion_action_primacy(
    ads_client, customer_id: str, action_ids: list[str]
) -> dict[str, dict[str, Any]]:
  if not action_ids:
    return {}
  id_list = ", ".join(str(i) for i in action_ids)
  rows = _search(
      ads_client,
      customer_id,
      "SELECT conversion_action.id, conversion_action.name, "
      "conversion_action.status, conversion_action.primary_for_goal "
      f"FROM conversion_action WHERE conversion_action.id IN ({id_list})",
  )
  return {
      str(r.conversion_action.id): {
          "name": r.conversion_action.name,
          "status": r.conversion_action.status.name,
          "primary_for_goal": r.conversion_action.primary_for_goal,
      }
      for r in rows
  }


def read_custom_conversion_goal_count(ads_client, customer_id: str) -> int:
  rows = _search(
      ads_client,
      customer_id,
      "SELECT custom_conversion_goal.id, custom_conversion_goal.status "
      "FROM custom_conversion_goal",
  )
  return sum(
      1 for r in rows if r.custom_conversion_goal.status.name != "REMOVED"
  )


# -----------------------------------------------------------------------------
# Rule 4 conflict check — same semantics as the governance amendment
# -----------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
  return text.lower().split()


def _phrase_blocks(neg_text: str, pos_text: str) -> bool:
  n, p = _tokens(neg_text), _tokens(pos_text)
  if not n or len(n) > len(p):
    return False
  return any(p[i : i + len(n)] == n for i in range(len(p) - len(n) + 1))


def _exact_blocks(neg_text: str, pos_text: str) -> bool:
  return _tokens(neg_text) == _tokens(pos_text)


def rule4_conflicts(
    ads_client, customer_id: str, campaign_resource_name: str
) -> list[dict[str, str]]:
  """Diffs a campaign's enabled negatives against its enabled positives.

  Mirrors active_constraints.md Rule 4 clause 4. A negative only reaches
  positives inside its own ad group, so the comparison is scoped per ad
  group rather than campaign-wide.
  """
  neg_rows = _search(
      ads_client,
      customer_id,
      "SELECT ad_group.name, ad_group_criterion.keyword.text, "
      "ad_group_criterion.keyword.match_type FROM ad_group_criterion "
      f"WHERE campaign.resource_name = '{campaign_resource_name}' "
      "AND ad_group_criterion.type = 'KEYWORD' "
      "AND ad_group_criterion.negative = TRUE "
      "AND ad_group_criterion.status = 'ENABLED'",
  )
  pos_rows = _search(
      ads_client,
      customer_id,
      "SELECT ad_group.name, ad_group_criterion.keyword.text, "
      "ad_group_criterion.keyword.match_type FROM keyword_view "
      f"WHERE campaign.resource_name = '{campaign_resource_name}' "
      "AND ad_group_criterion.negative = FALSE "
      "AND ad_group_criterion.status = 'ENABLED'",
  )

  negs: dict[str, list[tuple[str, str]]] = {}
  poss: dict[str, list[tuple[str, str]]] = {}
  for r in neg_rows:
    negs.setdefault(r.ad_group.name, []).append((
        r.ad_group_criterion.keyword.text,
        r.ad_group_criterion.keyword.match_type.name,
    ))
  for r in pos_rows:
    poss.setdefault(r.ad_group.name, []).append((
        r.ad_group_criterion.keyword.text,
        r.ad_group_criterion.keyword.match_type.name,
    ))

  conflicts = []
  for ad_group in set(negs) | set(poss):
    for ntext, nmatch in negs.get(ad_group, []):
      for ptext, _pmatch in poss.get(ad_group, []):
        hit = (
            _phrase_blocks(ntext, ptext)
            if nmatch == "PHRASE"
            else _exact_blocks(ntext, ptext)
        )
        if hit:
          conflicts.append(
              {"ad_group": ad_group, "negative": ntext, "positive": ptext}
          )
  return conflicts


# -----------------------------------------------------------------------------
# Drift guard
# -----------------------------------------------------------------------------


def assert_no_drift(ads_client, customer_id: str, spec: dict[str, Any]) -> None:
  """Re-reads everything the approval depended on; refuses on any change.

  Every check is a hard refusal, never a warning. A migration approved
  against one account state must not execute against a different one, and
  "close enough" is not a category that exists here.
  """
  problems: list[str] = []

  guard = spec["guard_master_campaign"]
  master = read_campaign_state(ads_client, customer_id, guard["resource_name"])
  if master["status"] != guard["expect_status"]:
    problems.append(
        f"Master campaign status is {master['status']}, expected "
        f"{guard['expect_status']}."
    )
  if master["bidding_strategy_type"] != guard["expect_bidding_strategy_type"]:
    problems.append(
        f"Master bidding strategy is {master['bidding_strategy_type']}, "
        f"expected {guard['expect_bidding_strategy_type']}."
    )
  if master["tcpa_micros"] != guard["expect_tcpa_micros"]:
    problems.append(
        "Master tCPA (maximize_conversions.target_cpa_micros) is "
        f"{_usd(master['tcpa_micros'])}, expected "
        f"{_usd(guard['expect_tcpa_micros'])}."
    )

  for upd in spec["budget_updates"]:
    rows = _search(
        ads_client,
        customer_id,
        "SELECT campaign_budget.amount_micros, "
        "campaign_budget.reference_count FROM campaign_budget "
        f"WHERE campaign_budget.resource_name = "
        f"'{upd['budget_resource_name']}' LIMIT 1",
    )
    if not rows:
      problems.append(f"Budget {upd['budget_resource_name']} not found.")
      continue
    live = rows[0].campaign_budget.amount_micros
    if live == upd["after_micros"]:
      continue  # idempotent re-apply; not drift
    if live != upd["before_micros"]:
      problems.append(
          f"Budget {upd['budget_resource_name']} is {_usd(live)}, but the "
          f"approval recorded BEFORE {_usd(upd['before_micros'])}."
      )

  for ver in spec["budget_verifications"]:
    rows = _search(
        ads_client,
        customer_id,
        "SELECT campaign_budget.amount_micros, "
        "campaign_budget.reference_count FROM campaign_budget "
        f"WHERE campaign_budget.resource_name = "
        f"'{ver['budget_resource_name']}' LIMIT 1",
    )
    if not rows:
      problems.append(f"Budget {ver['budget_resource_name']} not found.")
      continue
    budget = rows[0].campaign_budget
    if budget.amount_micros != ver["expect_micros"]:
      problems.append(
          f"Budget {ver['budget_resource_name']} is "
          f"{_usd(budget.amount_micros)}, expected "
          f"{_usd(ver['expect_micros'])}."
      )
    if budget.reference_count != ver["expect_reference_count"]:
      problems.append(
          f"Budget {ver['budget_resource_name']} reference_count is "
          f"{budget.reference_count}, expected "
          f"{ver['expect_reference_count']}. It may now be shared with "
          "another campaign."
      )

  for upd in spec["ad_group_cpc_updates"]:
    live = read_ad_group_state(
        ads_client, customer_id, upd["ad_group_resource_name"]
    )
    if live["cpc_bid_micros"] == upd["after_micros"]:
      continue
    if live["cpc_bid_micros"] != upd["before_micros"]:
      problems.append(
          f"Ad group \"{live['name']}\" CPC is "
          f"{_usd(live['cpc_bid_micros'])}, but the approval recorded "
          f"BEFORE {_usd(upd['before_micros'])}."
      )

  for upd in spec["ad_group_status_updates"]:
    live = read_ad_group_state(
        ads_client, customer_id, upd["ad_group_resource_name"]
    )
    if live["status"] == upd["after"]:
      continue
    if live["status"] != upd["before"]:
      problems.append(
          f"Ad group \"{live['name']}\" is {live['status']}, but the "
          f"approval recorded BEFORE {upd['before']}."
      )

  for upd in spec["campaign_status_updates"]:
    live = read_campaign_state(
        ads_client, customer_id, upd["campaign_resource_name"]
    )
    if live["status"] == upd["after"]:
      continue
    if live["status"] != upd["before"]:
      problems.append(
          f"Campaign \"{live['name']}\" is {live['status']}, but the "
          f"approval recorded BEFORE {upd['before']}."
      )
    if upd.get("expect_budget_micros") is not None:
      if live["budget_amount_micros"] != upd["expect_budget_micros"]:
        problems.append(
            f"Campaign \"{live['name']}\" budget is "
            f"{_usd(live['budget_amount_micros'])}, expected "
            f"{_usd(upd['expect_budget_micros'])}."
        )

  for pause in spec["keyword_pauses"]:
    live = read_criterion_state(
        ads_client, customer_id, pause["criterion_resource_name"]
    )
    if live is None:
      problems.append(
          f"Keyword criterion {pause['criterion_resource_name']} no longer "
          "exists. Its resource identity changed since approval."
      )
      continue
    if live["text"] != pause["text"] or live["match_type"] != pause[
        "match_type"
    ]:
      problems.append(
          f"Criterion {pause['criterion_resource_name']} is now "
          f"\"{live['text']}\" [{live['match_type']}], but was approved as "
          f"\"{pause['text']}\" [{pause['match_type']}]. Resource identity "
          "changed."
      )
      continue
    if live["status"] == "PAUSED":
      continue
    if live["status"] != pause["before_status"]:
      problems.append(
          f"Keyword \"{live['text']}\" is {live['status']}, but the approval "
          f"recorded BEFORE {pause['before_status']}."
      )

  for add in spec["language_additions"]:
    live = read_campaign_languages(
        ads_client, customer_id, add["campaign_resource_name"]
    )
    unexpected = [
        lang for lang in live if lang != add["language_constant"]
    ]
    if unexpected:
      problems.append(
          f"Campaign {add['campaign_resource_name']} already carries "
          f"language criteria {unexpected}, which the approval did not "
          "anticipate."
      )

  primacy = read_conversion_action_primacy(
      ads_client, customer_id, spec["require_primary_conversion_action_ids"]
  )
  for action_id in spec["require_primary_conversion_action_ids"]:
    info = primacy.get(str(action_id))
    if info is None:
      problems.append(f"Conversion action {action_id} not found.")
    elif not info["primary_for_goal"]:
      problems.append(
          f"Conversion action {action_id} (\"{info['name']}\") is no longer "
          "Primary."
      )
    elif info["status"] != "ENABLED":
      problems.append(
          f"Conversion action {action_id} is {info['status']}, expected "
          "ENABLED."
      )

  goal_count = read_custom_conversion_goal_count(ads_client, customer_id)
  if goal_count:
    problems.append(
        f"{goal_count} custom conversion goal(s) now exist. The approval "
        "was made against an account with none."
    )

  for resource_name in spec["guard_no_selective_optimization"]:
    state = read_campaign_state(ads_client, customer_id, resource_name)
    if state["selective_optimization"]:
      problems.append(
          f"Campaign \"{state['name']}\" now has selective optimization "
          f"set ({len(state['selective_optimization'])} action(s))."
      )

  for resource_name in spec["rule4_campaign_resource_names"]:
    conflicts = rule4_conflicts(ads_client, customer_id, resource_name)
    if conflicts:
      detail = "; ".join(
          f"{c['ad_group']}: \"{c['negative']}\" blocks \"{c['positive']}\""
          for c in conflicts[:5]
      )
      problems.append(
          f"Rule 4 conflict check on {resource_name} returned "
          f"{len(conflicts)} blocking conflict(s): {detail}"
      )

  if problems:
    raise ToolError(
        "MIGRATION REFUSED — the account no longer matches the approved "
        "state. Nothing was mutated.\n  - " + "\n  - ".join(problems)
    )


# -----------------------------------------------------------------------------
# Build the full operation list from a spec
# -----------------------------------------------------------------------------


def build_migration_operations(
    spec: dict[str, Any], existing_languages: dict[str, list[str]]
) -> tuple[list[Any], list[str]]:
  """Assembles the ordered operation list for one migration.

  Order matters for readability, not for correctness: the API applies the
  whole request as one transaction, so there is no intermediate state in
  which the bids are live but the campaign is not.

  Returns:
      (operations, human_readable_lines)
  """
  operations: list[Any] = []
  lines: list[str] = []

  for upd in spec["ad_group_cpc_updates"]:
    operations.append(
        build_ad_group_cpc_operation(
            upd["ad_group_resource_name"], upd["after_micros"]
        )
    )
    lines.append(
        f"    CPC  {upd['label']}: {_usd(upd['before_micros'])} -> "
        f"{_usd(upd['after_micros'])}"
    )

  for upd in spec["budget_updates"]:
    operations.append(
        build_campaign_budget_amount_operation(
            upd["budget_resource_name"], upd["after_micros"]
        )
    )
    lines.append(
        f"    BUDGET  {upd['label']}: {_usd(upd['before_micros'])}/day -> "
        f"{_usd(upd['after_micros'])}/day"
    )

  for pause in spec["keyword_pauses"]:
    operations.append(
        build_ad_group_criterion_status_operation(
            pause["criterion_resource_name"], "PAUSED"
        )
    )
    lines.append(
        f"    KEYWORD PAUSE  \"{pause['text']}\" [{pause['match_type']}] "
        f"in {pause['ad_group_label']}"
    )

  for add in spec["language_additions"]:
    already = add["language_constant"] in existing_languages.get(
        add["campaign_resource_name"], []
    )
    if already:
      lines.append(
          f"    LANGUAGE  {add['label']}: {add['language_constant']} already "
          "present, no operation emitted (idempotent)"
      )
      continue
    operations.append(
        build_campaign_language_operation(
            add["campaign_resource_name"], add["language_constant"]
        )
    )
    lines.append(
        f"    LANGUAGE  {add['label']}: add {add['language_constant']}"
    )

  for upd in spec["ad_group_status_updates"]:
    operations.append(
        build_ad_group_status_operation(
            upd["ad_group_resource_name"], upd["after"]
        )
    )
    lines.append(
        f"    AD GROUP  {upd['label']}: {upd['before']} -> {upd['after']}"
    )

  for upd in spec["campaign_status_updates"]:
    operations.append(
        build_campaign_status_operation(
            upd["campaign_resource_name"], upd["after"]
        )
    )
    lines.append(
        f"    CAMPAIGN  {upd['label']}: {upd['before']} -> {upd['after']}"
    )

  return operations, lines


def allowed_resource_names(spec: dict[str, Any]) -> set[str]:
  """The complete set of resources this migration may touch."""
  names: set[str] = set()
  for upd in spec["ad_group_cpc_updates"]:
    names.add(upd["ad_group_resource_name"])
  for upd in spec["budget_updates"]:
    names.add(upd["budget_resource_name"])
  for upd in spec["ad_group_status_updates"]:
    names.add(upd["ad_group_resource_name"])
  for upd in spec["campaign_status_updates"]:
    names.add(upd["campaign_resource_name"])
  for pause in spec["keyword_pauses"]:
    names.add(pause["criterion_resource_name"])
  for add in spec["language_additions"]:
    names.add(add["campaign_resource_name"])
  return names


# -----------------------------------------------------------------------------
# Executor
# -----------------------------------------------------------------------------


def _execute_campaign_migration(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
  """Applies an approved migration as ONE atomic heterogeneous request."""
  assert_no_drift(ads_client, customer_id, spec)

  existing_languages = {
      add["campaign_resource_name"]: read_campaign_languages(
          ads_client, customer_id, add["campaign_resource_name"]
      )
      for add in spec["language_additions"]
  }
  operations, _lines = build_migration_operations(spec, existing_languages)

  if not operations:
    return {
        "outcome": "no_op",
        "detail": "Every operation in this migration is already applied.",
    }

  assert_operations_within_scope(
      operations,
      allowed_resource_names(spec),
      protected_campaign_ids(spec.get("protected_campaign_ids")),
  )

  service = ads_client.get_service("GoogleAdsService")

  # Pre-flight the exact request Google is about to execute. If the real
  # mutate would fail, it fails here instead, having changed nothing.
  try:
    service.mutate(
        request=service_types.MutateGoogleAdsRequest(
            customer_id=customer_id,
            mutate_operations=operations,
            partial_failure=False,
            validate_only=True,
        )
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  try:
    response = service.mutate(
        request=service_types.MutateGoogleAdsRequest(
            customer_id=customer_id,
            mutate_operations=operations,
            partial_failure=False,
            validate_only=False,
        )
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  verified = _verify_after(ads_client, customer_id, spec)
  return {
      "outcome": "applied",
      "atomicity": "single GoogleAdsService.Mutate request, "
                   "partial_failure=False",
      "operations_sent": len(operations),
      "results": len(response.mutate_operation_responses),
      "verified": verified,
      "verified_by": "GAQL read-back after the atomic mutate",
  }


def _verify_after(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
  """Reads the account back and confirms every intended end state."""
  failures: list[str] = []
  observed: dict[str, Any] = {}

  for upd in spec["ad_group_cpc_updates"]:
    live = read_ad_group_state(
        ads_client, customer_id, upd["ad_group_resource_name"]
    )
    observed[f"cpc:{upd['label']}"] = _usd(live["cpc_bid_micros"])
    if live["cpc_bid_micros"] != upd["after_micros"]:
      failures.append(
          f"{upd['label']} CPC read back {_usd(live['cpc_bid_micros'])}, "
          f"expected {_usd(upd['after_micros'])}."
      )

  for upd in spec["budget_updates"]:
    rows = _search(
        ads_client,
        customer_id,
        "SELECT campaign_budget.amount_micros FROM campaign_budget WHERE "
        f"campaign_budget.resource_name = '{upd['budget_resource_name']}' "
        "LIMIT 1",
    )
    live = rows[0].campaign_budget.amount_micros if rows else -1
    observed[f"budget:{upd['label']}"] = _usd(live)
    if live != upd["after_micros"]:
      failures.append(
          f"{upd['label']} budget read back {_usd(live)}, expected "
          f"{_usd(upd['after_micros'])}."
      )

  for upd in spec["ad_group_status_updates"]:
    live = read_ad_group_state(
        ads_client, customer_id, upd["ad_group_resource_name"]
    )
    observed[f"ad_group_status:{upd['label']}"] = live["status"]
    if live["status"] != upd["after"]:
      failures.append(
          f"{upd['label']} read back {live['status']}, expected "
          f"{upd['after']}."
      )

  for upd in spec["campaign_status_updates"]:
    live = read_campaign_state(
        ads_client, customer_id, upd["campaign_resource_name"]
    )
    observed[f"campaign_status:{upd['label']}"] = live["status"]
    if live["status"] != upd["after"]:
      failures.append(
          f"{upd['label']} read back {live['status']}, expected "
          f"{upd['after']}."
      )

  for pause in spec["keyword_pauses"]:
    live = read_criterion_state(
        ads_client, customer_id, pause["criterion_resource_name"]
    )
    status = live["status"] if live else "MISSING"
    observed[f"keyword:{pause['text']}"] = status
    if status != "PAUSED":
      failures.append(f"Keyword \"{pause['text']}\" read back {status}.")

  for add in spec["language_additions"]:
    live = read_campaign_languages(
        ads_client, customer_id, add["campaign_resource_name"]
    )
    observed[f"language:{add['label']}"] = live
    if add["language_constant"] not in live:
      failures.append(
          f"{add['label']} language {add['language_constant']} not present "
          "after apply."
      )

  if failures:
    raise ToolError(
        "APPLIED BUT VERIFICATION FAILED. The atomic mutate reported "
        "success; the read-back disagrees. Investigate before any further "
        "change.\n  - " + "\n  - ".join(failures)
    )
  return observed


# -----------------------------------------------------------------------------
# Propose
# -----------------------------------------------------------------------------


def _to_micros(usd: float) -> int:
  return int(round(float(usd) * 1_000_000))


def _resolve_criterion(
    ads_client,
    customer_id: str,
    ad_group_id: str,
    text: str,
    match_type: str,
) -> dict[str, Any]:
  """Finds one keyword criterion by ad group + text + match type.

  Resolving identity at propose time and recording it in the spec is what
  lets the apply-time guard detect that a keyword was rebuilt: same text,
  different resource name means a different criterion, and the migration
  refuses rather than pausing a keyword nobody approved.
  """
  rows = _search(
      ads_client,
      customer_id,
      "SELECT ad_group_criterion.resource_name, "
      "ad_group_criterion.keyword.text, "
      "ad_group_criterion.keyword.match_type, ad_group_criterion.status, "
      "ad_group_criterion.negative, ad_group.name FROM ad_group_criterion "
      f"WHERE ad_group.id = {ad_group_id} "
      "AND ad_group_criterion.type = 'KEYWORD' "
      "AND ad_group_criterion.negative = FALSE "
      f"AND ad_group_criterion.keyword.text = '{text}' "
      f"AND ad_group_criterion.keyword.match_type = '{match_type}'",
  )
  live = [r for r in rows if r.ad_group_criterion.status.name != "REMOVED"]
  if not live:
    raise ToolError(
        f'No enabled keyword "{text}" [{match_type}] found in ad group '
        f"{ad_group_id}. Nothing to pause."
    )
  if len(live) > 1:
    raise ToolError(
        f'Ambiguous: {len(live)} criteria match "{text}" [{match_type}] in '
        f"ad group {ad_group_id}. Refusing to guess."
    )
  row = live[0]
  return {
      "criterion_resource_name": row.ad_group_criterion.resource_name,
      "text": row.ad_group_criterion.keyword.text,
      "match_type": row.ad_group_criterion.keyword.match_type.name,
      "before_status": row.ad_group_criterion.status.name,
      "ad_group_label": row.ad_group.name,
  }


@mcp.tool()
def propose_campaign_migration(
    customer_id: str,
    migration_label: str,
    reason_code: str,
    guard_master_campaign_id: str,
    guard_master_tcpa_usd: float,
    require_primary_conversion_action_ids: list[str],
    rule4_campaign_ids: list[str],
    ad_group_cpc_updates: list[dict] | None = None,
    budget_updates: list[dict] | None = None,
    budget_verifications: list[dict] | None = None,
    ad_group_status_updates: list[dict] | None = None,
    campaign_status_updates: list[dict] | None = None,
    keyword_pauses: list[dict] | None = None,
    language_additions: list[dict] | None = None,
    protected_campaign_ids_extra: list[str] | None = None,
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes a coordinated campaign migration as ONE immutable package.

  Reads every affected resource first and writes explicit BEFORE -> AFTER
  lines into the approval block, so the human approves a transition of the
  whole account, not a list of destinations.

  The resulting proposal applies through a single
  `GoogleAdsService.Mutate` request with `partial_failure=False`. There is
  no supported way to apply half of it.

  Args:
      migration_label: Short human name for this migration, shown in the
          approval block.
      guard_master_campaign_id: Campaign whose ENABLED status, bidding
          strategy and tCPA must be unchanged at apply time.
      guard_master_tcpa_usd: The tCPA that must still be in force, read
          from maximize_conversions.target_cpa_micros.
      require_primary_conversion_action_ids: Conversion actions that must
          still be Primary and ENABLED at apply time.
      rule4_campaign_ids: Campaigns whose negative-vs-positive conflict
          count must be zero at apply time (active_constraints.md Rule 4).
      ad_group_cpc_updates: [{"ad_group_id": "...", "cpc_bid_usd": 6.5}]
      budget_updates: [{"campaign_id": "...", "new_daily_usd": 115}]
      budget_verifications: [{"campaign_id": "...", "expect_daily_usd": 45,
          "expect_reference_count": 1}] — verified, never written.
      ad_group_status_updates: [{"ad_group_id": "...", "status": "PAUSED"}]
      campaign_status_updates: [{"campaign_id": "...", "status": "ENABLED"}]
      keyword_pauses: [{"ad_group_id": "...", "text": "...",
          "match_type": "EXACT"}]
      language_additions: [{"campaign_id": "...",
          "language_constant_id": "1000"}] — idempotent if already present.
      protected_campaign_ids_extra: Additional campaign IDs this migration
          must never touch. Widens the built-in protected set; cannot
          narrow it.
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)

  def campaign_rn(cid: str) -> str:
    return f"customers/{customer_id}/campaigns/{cid}"

  def ad_group_rn(agid: str) -> str:
    return f"customers/{customer_id}/adGroups/{agid}"

  protected = protected_campaign_ids(protected_campaign_ids_extra)

  # Refuse protected campaigns up front, before anything is read or built.
  for entry in (campaign_status_updates or []) + (budget_updates or []) + (
      budget_verifications or []
  ) + (language_additions or []) + (rule4_campaign_ids and [
      {"campaign_id": c} for c in rule4_campaign_ids
  ] or []):
    cid = str(entry.get("campaign_id", ""))
    if cid and cid in protected:
      raise ToolError(
          f"Campaign {cid} is protected and out of scope for migration "
          "tooling. Refusing to propose."
      )

  master = read_campaign_state(
      ads_client, customer_id, campaign_rn(guard_master_campaign_id)
  )
  expect_tcpa = _to_micros(guard_master_tcpa_usd)
  if master["tcpa_micros"] != expect_tcpa:
    raise ToolError(
        "Master tCPA guard mismatch at propose time: account reads "
        f"{_usd(master['tcpa_micros'])}, you specified "
        f"{_usd(expect_tcpa)}. Refusing to record a guard that is already "
        "wrong."
    )

  spec: dict[str, Any] = {
      "op": OP,
      "migration_label": migration_label,
      "ad_group_cpc_updates": [],
      "budget_updates": [],
      "budget_verifications": [],
      "ad_group_status_updates": [],
      "campaign_status_updates": [],
      "keyword_pauses": [],
      "language_additions": [],
      "guard_master_campaign": {
          "resource_name": campaign_rn(guard_master_campaign_id),
          "expect_status": master["status"],
          "expect_bidding_strategy_type": master["bidding_strategy_type"],
          "expect_tcpa_micros": expect_tcpa,
      },
      "guard_no_selective_optimization": [],
      "require_primary_conversion_action_ids": [
          str(i) for i in require_primary_conversion_action_ids
      ],
      "rule4_campaign_resource_names": [
          campaign_rn(c) for c in rule4_campaign_ids
      ],
      "protected_campaign_ids": sorted(protected),
      "login_customer_id": login_customer_id,
  }

  for upd in ad_group_cpc_updates or []:
    rn = ad_group_rn(str(upd["ad_group_id"]))
    live = read_ad_group_state(ads_client, customer_id, rn)
    spec["ad_group_cpc_updates"].append({
        "ad_group_resource_name": rn,
        "label": live["name"],
        "before_micros": live["cpc_bid_micros"],
        "after_micros": _to_micros(upd["cpc_bid_usd"]),
    })

  for upd in budget_updates or []:
    state = read_campaign_state(
        ads_client, customer_id, campaign_rn(str(upd["campaign_id"]))
    )
    spec["budget_updates"].append({
        "budget_resource_name": state["budget_resource_name"],
        "label": state["name"],
        "before_micros": state["budget_amount_micros"],
        "after_micros": _to_micros(upd["new_daily_usd"]),
    })
    spec["guard_no_selective_optimization"].append(
        campaign_rn(str(upd["campaign_id"]))
    )

  for ver in budget_verifications or []:
    state = read_campaign_state(
        ads_client, customer_id, campaign_rn(str(ver["campaign_id"]))
    )
    spec["budget_verifications"].append({
        "budget_resource_name": state["budget_resource_name"],
        "label": state["name"],
        "expect_micros": _to_micros(ver["expect_daily_usd"]),
        "expect_reference_count": int(ver["expect_reference_count"]),
        "observed_micros": state["budget_amount_micros"],
        "observed_reference_count": state["budget_reference_count"],
        "explicitly_shared": state["budget_explicitly_shared"],
    })

  for upd in ad_group_status_updates or []:
    rn = ad_group_rn(str(upd["ad_group_id"]))
    live = read_ad_group_state(ads_client, customer_id, rn)
    spec["ad_group_status_updates"].append({
        "ad_group_resource_name": rn,
        "label": live["name"],
        "before": live["status"],
        "after": str(upd["status"]).upper(),
    })

  for upd in campaign_status_updates or []:
    rn = campaign_rn(str(upd["campaign_id"]))
    live = read_campaign_state(ads_client, customer_id, rn)
    entry = {
        "campaign_resource_name": rn,
        "label": live["name"],
        "before": live["status"],
        "after": str(upd["status"]).upper(),
        "expect_budget_micros": live["budget_amount_micros"],
    }
    spec["campaign_status_updates"].append(entry)
    spec["guard_no_selective_optimization"].append(rn)

  for pause in keyword_pauses or []:
    resolved = _resolve_criterion(
        ads_client,
        customer_id,
        str(pause["ad_group_id"]),
        pause["text"],
        str(pause["match_type"]).upper(),
    )
    spec["keyword_pauses"].append(resolved)

  existing_languages: dict[str, list[str]] = {}
  for add in language_additions or []:
    rn = campaign_rn(str(add["campaign_id"]))
    langs = read_campaign_languages(ads_client, customer_id, rn)
    existing_languages[rn] = langs
    state = read_campaign_state(ads_client, customer_id, rn)
    spec["language_additions"].append({
        "campaign_resource_name": rn,
        "label": state["name"],
        "language_constant": (
            f"languageConstants/{add['language_constant_id']}"
        ),
        "observed_languages": langs,
    })

  spec["guard_no_selective_optimization"] = sorted(
      set(spec["guard_no_selective_optimization"])
  )

  operations, op_lines = build_migration_operations(spec, existing_languages)
  assert_operations_within_scope(
      operations, allowed_resource_names(spec), protected
  )

  human_lines = [
      f"MIGRATION PACKAGE: {migration_label}",
      f"Applied as ONE GoogleAdsService.Mutate request "
      f"({len(operations)} operation(s)), partial_failure=False.",
      "All operations succeed together or none are applied.",
      "",
      "OPERATIONS:",
  ]
  human_lines.extend(op_lines)
  human_lines.append("")
  human_lines.append("VERIFY ONLY (read, never written):")
  for ver in spec["budget_verifications"]:
    human_lines.append(
        f"    {ver['label']} budget: expect "
        f"{_usd(ver['expect_micros'])}/day and reference_count="
        f"{ver['expect_reference_count']}; currently "
        f"{_usd(ver['observed_micros'])}/day, reference_count="
        f"{ver['observed_reference_count']}, explicitly_shared="
        f"{ver['explicitly_shared']}"
    )
  human_lines.append("")
  human_lines.append("APPLY-TIME GUARDS (hard refusal on any drift):")
  human_lines.append(
      f"    Master \"{master['name']}\" must still be {master['status']} / "
      f"{master['bidding_strategy_type']} / tCPA {_usd(expect_tcpa)}"
  )
  human_lines.append(
      "    Every BEFORE value above must still match the live account"
  )
  human_lines.append(
      "    Conversion actions still Primary + ENABLED: "
      + ", ".join(spec["require_primary_conversion_action_ids"])
  )
  human_lines.append(
      "    No custom conversion goals, no selective optimization"
  )
  human_lines.append(
      "    Rule 4 conflict count must be 0 on: "
      + ", ".join(spec["rule4_campaign_resource_names"])
  )
  human_lines.append(
      "    Protected campaigns this package cannot touch: "
      + ", ".join(sorted(protected))
  )

  return approval.write_proposal(
      root,
      tool_name=OP,
      customer_id=customer_id,
      operations_human=human_lines,
      reason_code=reason_code,
      reason_detail=reason_detail,
      spec=spec,
      client_label=client_label,
  )


propose_campaign_migration.__doc__ = (
    propose_campaign_migration.__doc__
    % mutations_gated._common_propose_args_doc()  # pylint: disable=protected-access
)


mutations_gated.register_executor(OP, _execute_campaign_migration)
