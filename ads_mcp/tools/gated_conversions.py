# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Gated conversion-action mutation: primary_for_goal only.

Deliberately narrow. The only field this module can write is
`ConversionAction.primary_for_goal`, with a field mask that names exactly
that path. It cannot delete an action, cannot change status, category,
counting type, attribution or `include_in_conversions_metric`, and cannot
touch campaign goals or any GA4 configuration.

Setting `primary_for_goal = false` makes an action secondary to every
conversion goal, which excludes it from bidding optimization while
leaving it fully reportable under All conversions. That is the whole
purpose of this tool: retire an action from bidding without destroying
its history.

Four independent guards run at propose time AND again at apply time:

1. Protected actions   — named IDs can never appear in a batch.
2. Conversion volume   — an action with recent conversions is refused
                         unless the approval explicitly authorizes it.
3. Custom-goal usage    — if a custom conversion goal or a campaign's
                         selective_optimization names a targeted action,
                         `primary_for_goal=false` alone may not remove it
                         from bidding, so the tool stops.
4. State drift         — the account must still match the approved BEFORE
                         state, or apply refuses.

Batches are applied as one atomic MutateConversionActions request with
partial_failure disabled: the package either lands whole or not at all.

Registers into the shared approval gate in `mutations_gated`.
"""

import datetime
import os
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


# The single writable field. Anything else is out of scope by construction.
FIELD_MASK_PATHS = ["primary_for_goal"]

_DEFAULT_LIFETIME_START = "2025-01-01"
_PROTECTED_ENV_VAR = "LO_PROTECTED_CONVERSION_ACTIONS"


def _normalize_ids(conversion_action_ids: list[str]) -> list[str]:
  """Accepts bare IDs or full resource names; returns bare ID strings."""
  out = []
  for raw in conversion_action_ids:
    text = str(raw).strip()
    if not text:
      continue
    out.append(text.rsplit("/", 1)[-1])
  if not out:
    raise ToolError("conversion_action_ids must contain at least one ID.")
  duplicates = {i for i in out if out.count(i) > 1}
  if duplicates:
    raise ToolError(
        f"Duplicate conversion action IDs in the batch: {sorted(duplicates)}"
    )
  return out


def _resource_name(customer_id: str, action_id: str) -> str:
  return f"customers/{customer_id}/conversionActions/{action_id}"


def _protected_ids(explicit: list[str] | None) -> set[str]:
  """Union of explicitly-passed protected IDs and the env-var list.

  Kept configurable rather than hard-coded so the fork stays
  multi-client; a per-client protected set can be pinned via
  LO_PROTECTED_CONVERSION_ACTIONS without a code change.
  """
  ids = {str(i).strip().rsplit("/", 1)[-1] for i in (explicit or []) if str(i).strip()}
  env = os.environ.get(_PROTECTED_ENV_VAR, "")
  ids |= {p.strip() for p in env.split(",") if p.strip()}
  return ids


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


def _read_conversion_actions(
    ads_client, customer_id: str, action_ids: list[str]
) -> dict[str, dict[str, Any]]:
  """Reads the current state of each conversion action, keyed by ID."""
  service = ads_client.get_service("GoogleAdsService")
  id_list = ", ".join(action_ids)
  query = (
      "SELECT conversion_action.id, conversion_action.name, "
      "conversion_action.status, conversion_action.category, "
      "conversion_action.type, conversion_action.origin, "
      "conversion_action.primary_for_goal, "
      "conversion_action.include_in_conversions_metric "
      "FROM conversion_action "
      f"WHERE conversion_action.id IN ({id_list})"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  found = {}
  for row in rows:
    action = row.conversion_action
    found[str(action.id)] = {
        "id": str(action.id),
        "name": action.name,
        "status": action.status.name,
        "category": action.category.name,
        "type": action.type_.name,
        "origin": action.origin.name,
        "primary_for_goal": bool(action.primary_for_goal),
        # Recorded READ ONLY. Never written by this tool.
        "include_in_conversions_metric": bool(
            action.include_in_conversions_metric
        ),
    }
  missing = [i for i in action_ids if i not in found]
  if missing:
    raise ToolError(
        f"Conversion action(s) not found in customer {customer_id}: "
        f"{missing}. Verify the IDs before proposing."
    )
  return found


def _read_conversion_volumes(
    ads_client, customer_id: str, start: str, end: str
) -> dict[str, dict[str, float]]:
  """Returns {action_id: {conversions, all_conversions}} for a date window."""
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT segments.conversion_action, metrics.conversions, "
      "metrics.all_conversions "
      "FROM customer "
      f"WHERE segments.date BETWEEN '{start}' AND '{end}'"
  )
  try:
    rows = service.search(customer_id=customer_id, query=query)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  return {
      row.segments.conversion_action.rsplit("/", 1)[-1]: {
          "conversions": row.metrics.conversions,
          "all_conversions": row.metrics.all_conversions,
      }
      for row in rows
  }


def find_custom_goal_usage(
    ads_client, customer_id: str, action_ids: list[str]
) -> list[str]:
  """Returns reasons a targeted action may still influence bidding.

  `primary_for_goal=false` demotes an action against the standard,
  category-based goals. It does NOT necessarily remove it from a custom
  conversion goal or from a campaign's selective_optimization list, both
  of which name conversion actions directly. If either references a
  targeted action, this tool refuses rather than producing a change that
  looks complete but is not.
  """
  service = ads_client.get_service("GoogleAdsService")
  targets = {_resource_name(customer_id, i) for i in action_ids}
  issues = []

  try:
    goals = service.search(
        customer_id=customer_id,
        query=(
            "SELECT custom_conversion_goal.id, custom_conversion_goal.name, "
            "custom_conversion_goal.status, "
            "custom_conversion_goal.conversion_actions "
            "FROM custom_conversion_goal"
        ),
    )
    for row in goals:
      goal = row.custom_conversion_goal
      overlap = targets.intersection(set(goal.conversion_actions))
      if overlap:
        issues.append(
            f"Custom conversion goal \"{goal.name}\" (id {goal.id}, "
            f"{goal.status.name}) references {sorted(overlap)}."
        )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  try:
    campaigns = service.search(
        customer_id=customer_id,
        query=(
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.selective_optimization.conversion_actions "
            "FROM campaign WHERE campaign.status != 'REMOVED'"
        ),
    )
    for row in campaigns:
      selected = set(row.campaign.selective_optimization.conversion_actions)
      overlap = targets.intersection(selected)
      if overlap:
        issues.append(
            f"Campaign \"{row.campaign.name}\" (id {row.campaign.id}, "
            f"{row.campaign.status.name}) uses selective_optimization "
            f"naming {sorted(overlap)}."
        )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  return issues


# -----------------------------------------------------------------------------
# Operation builder — split out so the request shape can be validated live
# with validate_only, without going near a real mutate.
# -----------------------------------------------------------------------------


def build_primary_for_goal_operations(
    customer_id: str, action_ids: list[str], primary_for_goal: bool
):
  """Builds one ConversionActionOperation per action.

  The field mask names `primary_for_goal` and nothing else, so no other
  field on the resource can be written even accidentally.
  """
  operations = []
  for action_id in action_ids:
    action = resource_types.ConversionAction(
        resource_name=_resource_name(customer_id, action_id)
    )
    action.primary_for_goal = primary_for_goal
    operation = service_types.ConversionActionOperation(update=action)
    operation.update_mask.CopyFrom(
        field_mask_pb2.FieldMask(paths=FIELD_MASK_PATHS)
    )
    operations.append(operation)
  return operations


# -----------------------------------------------------------------------------
# Propose
# -----------------------------------------------------------------------------


@mcp.tool()
def propose_set_conversion_actions_primary_for_goal(
    customer_id: str,
    conversion_action_ids: list[str],
    primary_for_goal: bool,
    reason_code: str,
    protected_conversion_action_ids: list[str] | None = None,
    allow_nonzero_recent_conversions: bool = False,
    recent_window_days: int = 30,
    extended_window_start: str | None = None,
    extended_window_end: str | None = None,
    lifetime_start: str = _DEFAULT_LIFETIME_START,
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes flipping primary_for_goal on a batch of conversion actions.

  Setting `primary_for_goal=false` makes an action secondary to every
  conversion goal, excluding it from bidding optimization while keeping
  its full history reportable. Nothing is deleted and no other field is
  touched: the field mask is exactly ["primary_for_goal"].

  The whole batch travels as ONE proposal so a multi-action cleanup is
  approved and applied as a single immutable package, and it is applied
  as one atomic request with partial failure disabled.

  Refuses at propose time if any targeted action is protected, has
  non-zero conversions in a recent window without explicit authorization,
  or is referenced by a custom conversion goal or a campaign's
  selective_optimization. Every one of those is re-checked at apply.

  Args:
      customer_id: Google Ads customer ID (digits only).
      conversion_action_ids: IDs or full resource names to change.
      primary_for_goal: Desired state. False demotes to secondary.
      protected_conversion_action_ids: IDs that must never appear in the
          batch. Merged with the LO_PROTECTED_CONVERSION_ACTIONS env var.
      allow_nonzero_recent_conversions: Required to be True before an
          action with recent conversions will be included. Recorded in
          the hash-verified spec.
      recent_window_days: Trailing window for the volume guard. Default
          30, ending yesterday.
      extended_window_start: Optional second window start, e.g.
          "2026-05-01". Reported alongside the recent window.
      extended_window_end: Optional second window end.
      lifetime_start: Start of the historical window. Default
          "2025-01-01".
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)

  action_ids = _normalize_ids(conversion_action_ids)

  # --- Guard 1: protected actions.
  protected = _protected_ids(protected_conversion_action_ids)
  collision = sorted(protected.intersection(set(action_ids)))
  if collision:
    raise ToolError(
        "HARD REFUSAL. The batch contains protected conversion action(s): "
        f"{collision}. These are excluded from any primary_for_goal "
        "change. Remove them from conversion_action_ids."
    )

  current = _read_conversion_actions(ads_client, customer_id, action_ids)

  # --- Guard 3 (custom goals / selective optimization).
  custom_goal_issues = find_custom_goal_usage(
      ads_client, customer_id, action_ids
  )
  if custom_goal_issues:
    raise ToolError(
        "STOPPING FOR APPROVAL. One or more targeted actions are named "
        "directly by a custom conversion goal or a campaign's "
        "selective_optimization. Setting primary_for_goal=false alone may "
        "NOT remove them from bidding:\n"
        + "\n".join(f"  - {issue}" for issue in custom_goal_issues)
        + "\nResolve the goal configuration first; this tool will not "
        "produce a change that looks complete but is not."
    )

  # --- Guard 2 (conversion volume).
  today = datetime.date.today()
  recent_end = today - datetime.timedelta(days=1)
  recent_start = recent_end - datetime.timedelta(days=recent_window_days - 1)
  windows = {
      "recent": (recent_start.isoformat(), recent_end.isoformat()),
      "lifetime": (lifetime_start, recent_end.isoformat()),
  }
  if extended_window_start and extended_window_end:
    windows["extended"] = (extended_window_start, extended_window_end)

  volumes: dict[str, dict[str, dict[str, float]]] = {}
  for label, (start, end) in windows.items():
    volumes[label] = _read_conversion_volumes(
        ads_client, customer_id, start, end
    )

  def conv(label: str, action_id: str) -> float:
    return volumes.get(label, {}).get(action_id, {}).get("conversions", 0.0)

  nonzero = []
  for action_id in action_ids:
    recent = conv("recent", action_id)
    extended = conv("extended", action_id) if "extended" in windows else 0.0
    if recent > 0 or extended > 0:
      nonzero.append(
          f"{action_id} \"{current[action_id]['name']}\" — recent="
          f"{recent:.2f}, extended={extended:.2f}"
      )
  if nonzero and not allow_nonzero_recent_conversions:
    raise ToolError(
        "STOPPING FOR APPROVAL. These actions have non-zero Conversions "
        "in a recent window, so demoting them WOULD change the bidding "
        "signal:\n"
        + "\n".join(f"  - {n}" for n in nonzero)
        + "\nRe-call with allow_nonzero_recent_conversions=True only if "
        "that is genuinely intended; the flag is recorded in the "
        "hash-verified approval block."
    )

  # --- Guard 4 groundwork: record BEFORE state, detect no-ops.
  already = [
      i for i in action_ids if current[i]["primary_for_goal"] == primary_for_goal
  ]
  if len(already) == len(action_ids):
    raise ToolError(
        f"All {len(action_ids)} action(s) already have "
        f"primary_for_goal={primary_for_goal}. Nothing to propose."
    )

  human_lines = [
      f"Set primary_for_goal = {primary_for_goal} on "
      f"{len(action_ids)} conversion action(s).",
      "Field mask is exactly [\"primary_for_goal\"]. Nothing is deleted; "
      "status, category, counting type, attribution, "
      "include_in_conversions_metric, campaign goals and GA4 config are "
      "NOT modified.",
      f"Recent window: {windows['recent'][0]} .. {windows['recent'][1]}",
  ]
  if "extended" in windows:
    human_lines.append(
        f"Extended window: {windows['extended'][0]} .. "
        f"{windows['extended'][1]}"
    )
  human_lines.append(
      f"Historical window: {windows['lifetime'][0]} .. "
      f"{windows['lifetime'][1]}"
  )
  human_lines.append(
      "Custom conversion goals / selective_optimization referencing these "
      "actions: NONE FOUND."
  )
  if protected:
    human_lines.append(
        f"Protected actions confirmed absent from batch: {sorted(protected)}"
    )

  before_state = {}
  for action_id in action_ids:
    state = current[action_id]
    before_state[action_id] = state["primary_for_goal"]
    marker = "  [already at target — will no-op]" if action_id in already else ""
    human_lines.append(
        f"  {action_id} \"{state['name']}\" [{state['status']}, "
        f"{state['category']}]"
    )
    human_lines.append(
        f"      primary_for_goal: {state['primary_for_goal']} -> "
        f"{primary_for_goal}{marker}"
    )
    human_lines.append(
        "      include_in_conversions_metric: "
        f"{state['include_in_conversions_metric']} (READ ONLY, not changed)"
    )
    parts = [f"recent={conv('recent', action_id):.2f}"]
    if "extended" in windows:
      parts.append(f"extended={conv('extended', action_id):.2f}")
    parts.append(f"historical={conv('lifetime', action_id):.2f}")
    human_lines.append(f"      Conversions: {', '.join(parts)}")

  spec = {
      "op": "set_conversion_actions_primary_for_goal",
      "conversion_action_ids": action_ids,
      "primary_for_goal": primary_for_goal,
      "before_primary_for_goal": before_state,
      "protected_conversion_action_ids": sorted(protected),
      "allow_nonzero_recent_conversions": allow_nonzero_recent_conversions,
      "windows": {k: list(v) for k, v in windows.items()},
      "login_customer_id": login_customer_id,
  }
  return approval.write_proposal(
      root,
      tool_name="set_conversion_actions_primary_for_goal",
      customer_id=customer_id,
      operations_human=human_lines,
      reason_code=reason_code,
      reason_detail=reason_detail,
      spec=spec,
      client_label=client_label,
  )


propose_set_conversion_actions_primary_for_goal.__doc__ = (
    propose_set_conversion_actions_primary_for_goal.__doc__
    % mutations_gated._common_propose_args_doc()  # pylint: disable=protected-access
)


# -----------------------------------------------------------------------------
# Apply
# -----------------------------------------------------------------------------


def _execute_set_conversion_actions_primary_for_goal(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
  """Applies the approved batch after re-running every guard."""
  action_ids = list(spec["conversion_action_ids"])
  target = bool(spec["primary_for_goal"])

  # --- Guard 1 re-check.
  protected = _protected_ids(spec.get("protected_conversion_action_ids"))
  collision = sorted(protected.intersection(set(action_ids)))
  if collision:
    raise ToolError(
        f"HARD REFUSAL at apply: batch contains protected action(s) "
        f"{collision}."
    )

  current = _read_conversion_actions(ads_client, customer_id, action_ids)

  # --- Guard 3 re-check: custom goal drift.
  custom_goal_issues = find_custom_goal_usage(
      ads_client, customer_id, action_ids
  )
  if custom_goal_issues:
    raise ToolError(
        "Refusing to apply: a custom conversion goal or campaign "
        "selective_optimization now references a targeted action, which "
        "was not the case when this was approved:\n"
        + "\n".join(f"  - {issue}" for issue in custom_goal_issues)
    )

  # --- Guard 4: state drift against the approved BEFORE state.
  before = spec.get("before_primary_for_goal") or {}
  drift = []
  pending = []
  for action_id in action_ids:
    now = current[action_id]["primary_for_goal"]
    if now == target:
      continue  # already at target — nothing to do for this one
    approved_before = before.get(action_id)
    if approved_before is not None and now != approved_before:
      drift.append(
          f"{action_id} \"{current[action_id]['name']}\": approved "
          f"BEFORE={approved_before}, account now={now}"
      )
    pending.append(action_id)
  if drift:
    raise ToolError(
        "Refusing to apply: conversion action state changed since "
        "approval:\n" + "\n".join(f"  - {d}" for d in drift)
        + "\nRe-propose so the human approves the real transition."
    )

  if not pending:
    return {
        "outcome": "no_op",
        "detail": (
            f"All {len(action_ids)} action(s) already have "
            f"primary_for_goal={target}."
        ),
        "conversion_action_ids": action_ids,
    }

  service = ads_client.get_service("ConversionActionService")
  operations = build_primary_for_goal_operations(
      customer_id, pending, target
  )
  request = service_types.MutateConversionActionsRequest(
      customer_id=customer_id,
      operations=operations,
      # All-or-nothing. The approved package must not land partially.
      partial_failure=False,
      validate_only=False,
  )
  try:
    service.mutate_conversion_actions(request=request)
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  # --- Verify every action, including any that were already at target.
  after = _read_conversion_actions(ads_client, customer_id, action_ids)
  mismatched = [
      f"{i} \"{after[i]['name']}\" primary_for_goal={after[i]['primary_for_goal']}"
      for i in action_ids
      if after[i]["primary_for_goal"] != target
  ]
  if mismatched:
    raise ToolError(
        "Post-apply verification FAILED: expected primary_for_goal="
        f"{target} on all {len(action_ids)} action(s), but read back:\n"
        + "\n".join(f"  - {m}" for m in mismatched)
    )

  return {
      "outcome": "applied",
      "mutated": pending,
      "already_at_target": [i for i in action_ids if i not in pending],
      "primary_for_goal": target,
      "field_mask": FIELD_MASK_PATHS,
      "partial_failure": False,
      "verified": [
          {
              "id": i,
              "name": after[i]["name"],
              "status": after[i]["status"],
              "primary_for_goal": after[i]["primary_for_goal"],
              # Reported as Google returns it. Never set by this tool.
              "include_in_conversions_metric": after[i][
                  "include_in_conversions_metric"
              ],
          }
          for i in action_ids
      ],
      "verified_by": "GAQL read-back of conversion_action.primary_for_goal",
  }


mutations_gated.register_executor(
    "set_conversion_actions_primary_for_goal",
    _execute_set_conversion_actions_primary_for_goal,
)
