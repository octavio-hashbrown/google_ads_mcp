# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Gated text updates to EXISTING Responsive Search Ads.

Editing ad copy is normally done by building a replacement ad and
retiring the original, which throws away the ad ID and everything
attached to it. `AdService.MutateAds` can edit an RSA's text in place
instead, and this module is deliberately built only on that path:
`AdOperation` carries `update` and nothing else — no create, no remove —
so a replacement-ad workflow is not merely discouraged here, it is
unreachable.

The update mask is pinned to two leaf paths, headlines and descriptions.
Everything else that lives on an ad — final URL, path1/path2, status, the
ad group and campaign it belongs to — is outside what this tooling can
express, and the scope check rejects any operation that tries.

Because an RSA text update replaces the whole asset list, the proposal
records the complete BEFORE and AFTER lists for every ad plus a SHA-256
fingerprint of the exact prior state. At apply time the fingerprint is
recomputed and compared, which catches every drift case in one
comparison: a changed headline, a re-pinned asset, an edited final URL,
a moved ad, a status flip.
"""

import hashlib
import json
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


OP = "update_responsive_search_ads"

# The only two paths this tooling may ever write. Verified against
# google-ads 30.1.0 / v24: Ad.responsive_search_ad carries exactly
# headlines, descriptions, path1, path2 — path1/path2 are deliberately
# excluded here.
ALLOWED_UPDATE_MASK = frozenset({
    "responsive_search_ad.headlines",
    "responsive_search_ad.descriptions",
})

# Google Ads RSA limits.
MAX_HEADLINES = 15
MAX_DESCRIPTIONS = 4
MIN_HEADLINES = 3
MIN_DESCRIPTIONS = 2
MAX_HEADLINE_CHARS = 30
MAX_DESCRIPTION_CHARS = 90

_RSA_TYPE = "RESPONSIVE_SEARCH_AD"


def _asset_list(assets) -> list[dict[str, str]]:
  """Normalizes AdTextAsset protos to comparable plain dicts."""
  out = []
  for a in assets:
    pinned = a.pinned_field.name if a.pinned_field else "UNSPECIFIED"
    out.append({"text": a.text, "pinned_field": pinned})
  return out


def fingerprint(state: dict[str, Any]) -> str:
  """SHA-256 over the guard-relevant fields of an ad's exact state.

  One comparison covers identity, ownership, status, destination,
  pinning, paths and every asset's text. Anything an editor could change
  moves the digest.
  """
  payload = {
      "ad_id": str(state["ad_id"]),
      "resource_name": state["resource_name"],
      "campaign": state["campaign"],
      "ad_group": state["ad_group"],
      "status": state["status"],
      "ad_type": state["ad_type"],
      "final_urls": list(state["final_urls"]),
      "path1": state["path1"],
      "path2": state["path2"],
      "headlines": state["headlines"],
      "descriptions": state["descriptions"],
  }
  canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
  return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_ad_state(ads_client, customer_id: str, ad_id: str) -> dict[str, Any]:
  """Reads one ad's complete guard-relevant state.

  Raises:
      ToolError: If the ad does not exist or is not an RSA.
  """
  service = ads_client.get_service("GoogleAdsService")
  query = (
      "SELECT ad_group_ad.ad.id, ad_group_ad.ad.resource_name, "
      "ad_group_ad.ad.type, ad_group_ad.status, "
      "ad_group_ad.ad.final_urls, "
      "ad_group_ad.ad.responsive_search_ad.headlines, "
      "ad_group_ad.ad.responsive_search_ad.descriptions, "
      "ad_group_ad.ad.responsive_search_ad.path1, "
      "ad_group_ad.ad.responsive_search_ad.path2, "
      "ad_group.id, ad_group.name, campaign.id, campaign.name "
      f"FROM ad_group_ad WHERE ad_group_ad.ad.id = {ad_id} LIMIT 1"
  )
  try:
    rows = list(service.search(customer_id=customer_id, query=query))
  except GoogleAdsException as e:
    _handle_google_ads_error(e)
  if not rows:
    raise ToolError(f"No ad found with id {ad_id} in customer {customer_id}.")
  row = rows[0]
  ad = row.ad_group_ad.ad
  if ad.type_.name != _RSA_TYPE:
    raise ToolError(
        f"Ad {ad_id} is a {ad.type_.name}, not a {_RSA_TYPE}. This tool only "
        "edits Responsive Search Ads."
    )
  state = {
      "ad_id": ad.id,
      "resource_name": ad.resource_name,
      "ad_type": ad.type_.name,
      "status": row.ad_group_ad.status.name,
      "final_urls": list(ad.final_urls),
      "path1": ad.responsive_search_ad.path1,
      "path2": ad.responsive_search_ad.path2,
      "headlines": _asset_list(ad.responsive_search_ad.headlines),
      "descriptions": _asset_list(ad.responsive_search_ad.descriptions),
      "campaign": str(row.campaign.id),
      "campaign_name": row.campaign.name,
      "ad_group": str(row.ad_group.id),
      "ad_group_name": row.ad_group.name,
  }
  state["fingerprint"] = fingerprint(state)
  return state


def normalize_after(assets: list[Any], kind: str) -> list[dict[str, str]]:
  """Accepts plain strings or {text, pinned_field} dicts; validates both.

  Raises:
      ToolError: On count, length or pinning problems.
  """
  if kind == "headline":
    lo, hi, limit = MIN_HEADLINES, MAX_HEADLINES, MAX_HEADLINE_CHARS
  else:
    lo, hi, limit = MIN_DESCRIPTIONS, MAX_DESCRIPTIONS, MAX_DESCRIPTION_CHARS

  out: list[dict[str, str]] = []
  for a in assets:
    if isinstance(a, str):
      text, pinned = a, "UNSPECIFIED"
    else:
      text = a.get("text", "")
      pinned = str(a.get("pinned_field") or "UNSPECIFIED").upper()
    if not text or not text.strip():
      raise ToolError(f"Empty {kind} text is not allowed.")
    if len(text) > limit:
      raise ToolError(
          f'{kind.capitalize()} "{text}" is {len(text)} characters; the '
          f"Google Ads limit is {limit}. Refusing."
      )
    out.append({"text": text, "pinned_field": pinned})

  if not lo <= len(out) <= hi:
    raise ToolError(
        f"An RSA needs between {lo} and {hi} {kind}s; got {len(out)}."
    )
  texts = [a["text"] for a in out]
  dupes = {t for t in texts if texts.count(t) > 1}
  if dupes:
    raise ToolError(
        f"Duplicate {kind}(s) within one ad: {sorted(dupes)}. Google rejects "
        "identical assets in the same RSA."
    )
  return out


def build_rsa_text_update_operation(
    ad_resource_name: str,
    headlines: list[dict[str, str]],
    descriptions: list[dict[str, str]],
) -> Any:
  """Builds the AdOperation that rewrites an existing RSA's text.

  The mask names the two leaf paths and nothing else. `AdOperation`
  itself has no create or remove member, so this cannot become an
  ad-replacement operation.
  """
  pin_enum = enum_types.ServedAssetFieldTypeEnum.ServedAssetFieldType
  ad = resource_types.Ad(resource_name=ad_resource_name)
  for h in headlines:
    asset = common_types.AdTextAsset(text=h["text"])
    if h["pinned_field"] != "UNSPECIFIED":
      asset.pinned_field = pin_enum[h["pinned_field"]]
    ad.responsive_search_ad.headlines.append(asset)
  for d in descriptions:
    asset = common_types.AdTextAsset(text=d["text"])
    if d["pinned_field"] != "UNSPECIFIED":
      asset.pinned_field = pin_enum[d["pinned_field"]]
    ad.responsive_search_ad.descriptions.append(asset)

  operation = service_types.AdOperation(update=ad)
  operation.update_mask.CopyFrom(
      field_mask_pb2.FieldMask(paths=sorted(ALLOWED_UPDATE_MASK))
  )
  return operation


def assert_operations_within_scope(
    operations: list[Any], allowed_resource_names: set[str]
) -> None:
  """Rejects any operation outside the two permitted mask paths.

  Re-derives the target and mask from the built proto rather than
  trusting the spec that produced it.
  """
  for i, op in enumerate(operations):
    which = op._pb.WhichOneof("operation")  # pylint: disable=protected-access
    if which != "update":
      raise ToolError(
          f"Operation {i} is an ad {which!r} operation. This tooling may "
          "only update existing ads."
      )
    mask = set(op.update_mask.paths)
    if mask != set(ALLOWED_UPDATE_MASK):
      raise ToolError(
          f"Operation {i} carries update mask {sorted(mask)}, but only "
          f"{sorted(ALLOWED_UPDATE_MASK)} may be written. Refusing the "
          "whole batch."
      )
    target = op.update.resource_name
    if target not in allowed_resource_names:
      raise ToolError(
          f"Operation {i} targets {target}, which is not in the approved "
          "ad allowlist. Refusing."
      )
    # Belt and braces: the payload must not carry paths even though the
    # mask excludes them, so a mask bug cannot silently blank them.
    rsa = op.update.responsive_search_ad
    if rsa.path1 or rsa.path2:
      raise ToolError(
          f"Operation {i} carries path1/path2 values. This tooling must not "
          "express display-path changes."
      )
    if op.update.final_urls:
      raise ToolError(
          f"Operation {i} carries final_urls. This tooling must not express "
          "destination changes."
      )


def assert_no_drift(ads_client, customer_id: str, spec: dict[str, Any]) -> None:
  """Re-reads every target ad and refuses on any change since approval."""
  problems: list[str] = []

  for entry in spec["ads"]:
    ad_id = entry["ad_id"]
    try:
      live = read_ad_state(ads_client, customer_id, ad_id)
    except ToolError as e:
      problems.append(f"Ad {ad_id}: {e}")
      continue

    if live["fingerprint"] == entry["after_fingerprint"]:
      continue  # already applied; idempotent, not drift

    if live["fingerprint"] == entry["before_fingerprint"]:
      continue  # unchanged since approval — the expected case

    # Fingerprint moved. Say exactly what moved.
    b = entry["before"]
    if live["resource_name"] != b["resource_name"]:
      problems.append(
          f"Ad {ad_id}: resource identity changed "
          f"({b['resource_name']} -> {live['resource_name']})."
      )
    if live["status"] != b["status"]:
      problems.append(
          f"Ad {ad_id}: status is {live['status']}, approved against "
          f"{b['status']}."
      )
    if list(live["final_urls"]) != list(b["final_urls"]):
      problems.append(
          f"Ad {ad_id}: final URL changed ({b['final_urls']} -> "
          f"{live['final_urls']})."
      )
    if live["campaign"] != b["campaign"] or live["ad_group"] != b["ad_group"]:
      problems.append(
          f"Ad {ad_id}: ownership changed (campaign {b['campaign']}/"
          f"ad group {b['ad_group']} -> {live['campaign']}/"
          f"{live['ad_group']})."
      )
    if live["path1"] != b["path1"] or live["path2"] != b["path2"]:
      problems.append(
          f"Ad {ad_id}: display paths changed "
          f"({b['path1']!r}/{b['path2']!r} -> "
          f"{live['path1']!r}/{live['path2']!r})."
      )
    if len(live["headlines"]) != len(b["headlines"]):
      problems.append(
          f"Ad {ad_id}: headline count is {len(live['headlines'])}, approved "
          f"against {len(b['headlines'])}."
      )
    if len(live["descriptions"]) != len(b["descriptions"]):
      problems.append(
          f"Ad {ad_id}: description count is {len(live['descriptions'])}, "
          f"approved against {len(b['descriptions'])}."
      )
    for label, key in (("headline", "headlines"), ("description",
                                                   "descriptions")):
      live_texts = [a["text"] for a in live[key]]
      before_texts = [a["text"] for a in b[key]]
      if live_texts != before_texts:
        added = [t for t in live_texts if t not in before_texts]
        removed = [t for t in before_texts if t not in live_texts]
        problems.append(
            f"Ad {ad_id}: existing {label}s changed. "
            f"Now present and unapproved: {added or 'none'}. "
            f"Approved but missing: {removed or 'none'}."
        )
      live_pins = {a["text"]: a["pinned_field"] for a in live[key]}
      before_pins = {a["text"]: a["pinned_field"] for a in b[key]}
      moved = [
          t for t in before_pins
          if t in live_pins and live_pins[t] != before_pins[t]
      ]
      if moved:
        problems.append(f"Ad {ad_id}: {label} pinning changed for {moved}.")

    if not problems:
      problems.append(
          f"Ad {ad_id}: state fingerprint changed since approval "
          f"({entry['before_fingerprint'][:12]} -> "
          f"{live['fingerprint'][:12]}) without a field-level explanation. "
          "Refusing rather than guessing."
      )

  if problems:
    raise ToolError(
        "RSA COPY UPDATE REFUSED — the ads no longer match the approved "
        "state. Nothing was mutated.\n  - " + "\n  - ".join(problems)
    )


def _build_all(spec: dict[str, Any]) -> list[Any]:
  return [
      build_rsa_text_update_operation(
          entry["before"]["resource_name"],
          entry["after"]["headlines"],
          entry["after"]["descriptions"],
      )
      for entry in spec["ads"]
  ]


def _execute_update_responsive_search_ads(
    ads_client, customer_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
  """Applies approved RSA text updates as one batch, all-or-nothing."""
  assert_no_drift(ads_client, customer_id, spec)

  already = 0
  pending = []
  for entry in spec["ads"]:
    live = read_ad_state(ads_client, customer_id, entry["ad_id"])
    if live["fingerprint"] == entry["after_fingerprint"]:
      already += 1
    else:
      pending.append(entry)

  if not pending:
    return {
        "outcome": "no_op",
        "detail": f"All {already} ad(s) already carry the approved copy.",
    }

  operations = _build_all({"ads": pending})
  assert_operations_within_scope(
      operations, {e["before"]["resource_name"] for e in pending}
  )

  service = ads_client.get_service("AdService")

  try:
    service.mutate_ads(
        request=service_types.MutateAdsRequest(
            customer_id=customer_id,
            operations=operations,
            partial_failure=False,
            validate_only=True,
        )
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  try:
    response = service.mutate_ads(
        request=service_types.MutateAdsRequest(
            customer_id=customer_id,
            operations=operations,
            partial_failure=False,
            validate_only=False,
        )
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  verified = []
  failures = []
  for entry in pending:
    live = read_ad_state(ads_client, customer_id, entry["ad_id"])
    ok = live["fingerprint"] == entry["after_fingerprint"]
    verified.append({
        "ad_id": entry["ad_id"],
        "label": entry["label"],
        "headlines": len(live["headlines"]),
        "descriptions": len(live["descriptions"]),
        "matches_approved_after": ok,
    })
    if not ok:
      failures.append(
          f"Ad {entry['ad_id']} ({entry['label']}) read back a state that "
          "does not match the approved AFTER fingerprint."
      )

  if failures:
    raise ToolError(
        "APPLIED BUT VERIFICATION FAILED. The batch reported success; the "
        "read-back disagrees.\n  - " + "\n  - ".join(failures)
    )

  return {
      "outcome": "applied",
      "atomicity": "single AdService.MutateAds request, partial_failure=False",
      "operations_sent": len(operations),
      "results": len(response.results),
      "already_at_target": already,
      "verified": verified,
      "verified_by": "GAQL read-back + AFTER-state fingerprint match",
  }


@mcp.tool()
def propose_update_responsive_search_ads(
    customer_id: str,
    update_label: str,
    reason_code: str,
    ads: list[dict],
    reason_detail: str | None = None,
    client_root: str | None = None,
    client_label: str | None = None,
    login_customer_id: str | None = None,
) -> dict[str, str]:
  """Proposes text-only edits to EXISTING Responsive Search Ads.

  Reads each ad's complete current state first and records both the full
  BEFORE and AFTER asset lists plus a SHA-256 fingerprint of the prior
  state, so the human approves an exact transition of known ads.

  The update mask is fixed to responsive_search_ad.headlines and
  responsive_search_ad.descriptions. Final URL, display paths, status,
  ad group and campaign are outside what this tool can express, and
  pinning is carried through from the AFTER list you supply.

  Applies as one AdService.MutateAds batch with partial_failure=False.

  Args:
      update_label: Short human name for this copy change.
      ads: One entry per ad:
          {"ad_id": "820665316770",
           "headlines": ["...", ...],        complete AFTER list, 3-15
           "descriptions": ["...", ...]}     complete AFTER list, 2-4
          Each asset may be a plain string, or
          {"text": "...", "pinned_field": "HEADLINE_1"} to set pinning
          explicitly. Strings default to unpinned.
%s
      login_customer_id: MCC account ID if customer is managed.
  """
  audit.validate_reason(reason_code, reason_detail)
  root = audit.resolve_client_root(client_root)
  ads_client = _get_client(login_customer_id)

  if not ads:
    raise ToolError("No ads supplied. Nothing to propose.")

  seen = set()
  entries = []
  human_lines = [
      f"RSA COPY UPDATE: {update_label}",
      f"Applied as ONE AdService.MutateAds request ({len(ads)} ad(s)), "
      "partial_failure=False. All succeed together or none are applied.",
      "Update mask is fixed to: "
      + ", ".join(sorted(ALLOWED_UPDATE_MASK)),
      "Final URL, display paths, status, ad group and campaign are NOT "
      "writable by this tool.",
      "",
  ]

  for item in ads:
    ad_id = str(item["ad_id"])
    if ad_id in seen:
      raise ToolError(f"Ad {ad_id} appears more than once in this proposal.")
    seen.add(ad_id)

    before = read_ad_state(ads_client, customer_id, ad_id)
    after_headlines = normalize_after(item["headlines"], "headline")
    after_descriptions = normalize_after(item["descriptions"], "description")

    after_state = dict(before)
    after_state["headlines"] = after_headlines
    after_state["descriptions"] = after_descriptions
    after_fp = fingerprint(after_state)

    label = f"{before['ad_group_name']} / ad {ad_id}"
    entries.append({
        "ad_id": ad_id,
        "label": label,
        "before": before,
        "after": {
            "headlines": after_headlines,
            "descriptions": after_descriptions,
        },
        "before_fingerprint": before["fingerprint"],
        "after_fingerprint": after_fp,
    })

    kept_h = [a for a in after_headlines
              if a["text"] in [b["text"] for b in before["headlines"]]]
    new_h = [a for a in after_headlines if a not in kept_h]
    kept_d = [a for a in after_descriptions
              if a["text"] in [b["text"] for b in before["descriptions"]]]
    new_d = [a for a in after_descriptions if a not in kept_d]
    dropped_h = [b["text"] for b in before["headlines"]
                 if b["text"] not in [a["text"] for a in after_headlines]]
    dropped_d = [b["text"] for b in before["descriptions"]
                 if b["text"] not in [a["text"] for a in after_descriptions]]

    human_lines.extend([
        f"  {label}",
        f"    campaign {before['campaign']} / ad group {before['ad_group']} "
        f"/ status {before['status']}",
        f"    final URL (unchanged): {', '.join(before['final_urls'])}",
        f"    display paths (unchanged): "
        f"{before['path1']!r} / {before['path2']!r}",
        f"    headlines    {len(before['headlines'])} -> "
        f"{len(after_headlines)}   (kept {len(kept_h)}, added "
        f"{len(new_h)}, dropped {len(dropped_h)})",
        f"    descriptions {len(before['descriptions'])} -> "
        f"{len(after_descriptions)}   (kept {len(kept_d)}, added "
        f"{len(new_d)}, dropped {len(dropped_d)})",
        f"    BEFORE fingerprint: {before['fingerprint']}",
        f"    AFTER  fingerprint: {after_fp}",
    ])
    if dropped_h:
      for t in dropped_h:
        human_lines.append(f"      DROP headline:    \"{t}\"")
    if dropped_d:
      for t in dropped_d:
        human_lines.append(f"      DROP description: \"{t}\"")
    for a in new_h:
      human_lines.append(
          f"      ADD headline:     \"{a['text']}\" ({len(a['text'])})"
      )
    for a in new_d:
      human_lines.append(
          f"      ADD description:  \"{a['text']}\" ({len(a['text'])})"
      )
    pins = [a for a in after_headlines + after_descriptions
            if a["pinned_field"] != "UNSPECIFIED"]
    human_lines.append(
        f"    pinning: {'none — all assets unpinned' if not pins else pins}"
    )
    human_lines.append("")

  human_lines.extend([
      "APPLY-TIME GUARDS (hard refusal on any drift):",
      "    Every ad's BEFORE fingerprint must still match exactly.",
      "    That covers ad identity, status, final URL, display paths,",
      "    campaign/ad-group ownership, every existing headline and",
      "    description, asset counts and pinning.",
      "    A validate_only pass runs immediately before the real batch.",
  ])

  spec = {
      "op": OP,
      "update_label": update_label,
      "ads": entries,
      "allowed_update_mask": sorted(ALLOWED_UPDATE_MASK),
      "login_customer_id": login_customer_id,
  }
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


propose_update_responsive_search_ads.__doc__ = (
    propose_update_responsive_search_ads.__doc__
    % mutations_gated._common_propose_args_doc()  # pylint: disable=protected-access
)


mutations_gated.register_executor(OP, _execute_update_responsive_search_ads)
