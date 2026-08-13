# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Read-only Keyword Planner access.

Both tools here are pure reads: they return demand estimates without
persisting anything. `KeywordPlanService` and friends (which create saved
plans, campaigns, ad groups and forecasts as real account objects) are
deliberately NOT wired up, so this module cannot create a Keyword Plan
even by accident.

These services require at least Basic developer-token access; under
Explorer access they return an authorization error.

Keyword Planning endpoints are rate limited to roughly one request per
second per customer ID, so every call routes through `_call_with_retry`,
which spaces requests and backs off on quota errors.
"""

import threading
import time
from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException

from ads_mcp.coordinator import mcp_server as mcp
from ads_mcp.tools._ads_api import enum_types
from ads_mcp.tools._ads_api import service_types
from ads_mcp.tools.mutations.common import _get_client
from ads_mcp.tools.mutations.common import _handle_google_ads_error


_MICROS = 1_000_000

# Google documents a per-customer limit of about 1 request/second for
# Keyword Planning. Spacing calls locally is cheaper than absorbing
# RESOURCE_EXHAUSTED and retrying.
_MIN_INTERVAL_SECONDS = 1.0
_MAX_ATTEMPTS = 3

_throttle_lock = threading.Lock()
_last_call_at: dict[str, float] = {}

_ZERO_RESULT_NOTE = (
    "Google returned zero keyword ideas for this seed. Do not interpret "
    "this as zero search demand. Retry with commercial/local-intent "
    "variants, URL/keyword+URL seeds, and/or historical keyword metrics "
    "before drawing a demand conclusion."
)


def _dollars(micros: int) -> float | None:
  return round(micros / _MICROS, 2) if micros else None


def _throttle(customer_id: str) -> None:
  """Blocks until at least _MIN_INTERVAL_SECONDS since this CID's last call."""
  with _throttle_lock:
    now = time.monotonic()
    last = _last_call_at.get(customer_id)
    if last is not None:
      wait = _MIN_INTERVAL_SECONDS - (now - last)
      if wait > 0:
        time.sleep(wait)
        now = time.monotonic()
    _last_call_at[customer_id] = now


def _is_quota_error(exception: GoogleAdsException) -> bool:
  """True when the failure is a rate/quota rejection worth retrying."""
  for error in exception.failure.errors:
    code = error.error_code
    try:
      if code._pb.WhichOneof("error_code") == "quota_error":  # pylint: disable=protected-access
        return True
    except (AttributeError, ValueError):
      pass
    if "RESOURCE_EXHAUSTED" in (error.message or "").upper():
      return True
  return False


def _call_with_retry(customer_id: str, call):
  """Runs a Keyword Planning call, throttled, retrying on quota errors."""
  delay = 2.0
  for attempt in range(_MAX_ATTEMPTS):
    _throttle(customer_id)
    try:
      return call()
    except GoogleAdsException as e:
      if attempt == _MAX_ATTEMPTS - 1 or not _is_quota_error(e):
        raise
      time.sleep(delay)
      delay *= 2
  raise AssertionError("unreachable")


def _metrics_payload(metrics) -> dict[str, Any]:
  """Flattens KeywordPlanHistoricalMetrics into a plain dict."""
  return {
      "avg_monthly_searches": metrics.avg_monthly_searches,
      "competition": metrics.competition.name,
      "competition_index": metrics.competition_index,
      "low_top_of_page_bid_micros": metrics.low_top_of_page_bid_micros,
      "high_top_of_page_bid_micros": metrics.high_top_of_page_bid_micros,
      "low_top_of_page_bid_usd": _dollars(metrics.low_top_of_page_bid_micros),
      "high_top_of_page_bid_usd": _dollars(metrics.high_top_of_page_bid_micros),
  }


def _monthly_series(metrics) -> list[dict[str, Any]]:
  """Returns the monthly search volume series, when Google supplies one."""
  return [
      {
          "year": int(volume.year),
          "month": volume.month.name,
          "monthly_searches": volume.monthly_searches,
      }
      for volume in metrics.monthly_search_volumes
  ]


def _resolve_network(include_search_partners: bool):
  network_enum = enum_types.KeywordPlanNetworkEnum.KeywordPlanNetwork
  return (
      network_enum.GOOGLE_SEARCH_AND_PARTNERS
      if include_search_partners
      else network_enum.GOOGLE_SEARCH
  )


@mcp.tool()
def generate_keyword_ideas(
    customer_id: str,
    keyword_seeds: list[str] | None = None,
    page_url: str | None = None,
    geo_target_constant_ids: list[int] | None = None,
    language_constant_id: str = "1000",
    include_search_partners: bool = False,
    include_adult_keywords: bool = False,
    max_results: int = 100,
    login_customer_id: str | None = None,
) -> dict[str, Any]:
  """Returns Keyword Planner demand estimates. Read-only, never mutates.

  Seeds the request one of three documented ways, chosen automatically:
  keyword_seeds alone, page_url alone, or both together.

  Volumes are Google's rounded, geo-aggregated estimates for the targeted
  locations. They indicate relative demand; they are not a promise of
  impressions and should not be presented as a traffic ceiling.

  Args:
      customer_id: Google Ads customer ID (digits only).
      keyword_seeds: Seed terms, e.g. ["physical therapy", "chiropractor"].
      page_url: Seed page whose content Google mines for ideas.
      geo_target_constant_ids: Geo target constant IDs to scope demand to,
          e.g. [1023191] for a metro, [2840] for the United States.
          Defaults to [2840] when omitted.
      language_constant_id: Language constant ID. Defaults to "1000"
          (English).
      include_search_partners: False queries Google Search only (the
          default); True adds search partners.
      include_adult_keywords: Whether to include adult keywords.
      max_results: Cap on ideas returned. Defaults to 100.
      login_customer_id: MCC account ID if customer is managed.

  Returns:
      Dict with the resolved request parameters and an `ideas` list of
      {text, avg_monthly_searches, competition, competition_index,
      low/high_top_of_page_bid_micros, low/high_top_of_page_bid_usd},
      sorted by average monthly searches descending.
  """
  seeds = [s for s in (keyword_seeds or []) if s and s.strip()]
  url = page_url.strip() if page_url and page_url.strip() else None
  if not seeds and not url:
    raise ToolError(
        "Provide keyword_seeds, page_url, or both — GenerateKeywordIdeas "
        "needs at least one seed."
    )
  if max_results < 1:
    raise ToolError("max_results must be at least 1.")

  ads_client = _get_client(login_customer_id)
  service = ads_client.get_service("KeywordPlanIdeaService")
  geo_service = ads_client.get_service("GeoTargetConstantService")

  geo_ids = geo_target_constant_ids or [2840]
  network = _resolve_network(include_search_partners)

  request = service_types.GenerateKeywordIdeasRequest(
      customer_id=customer_id,
      language=f"languageConstants/{language_constant_id}",
      keyword_plan_network=network,
      include_adult_keywords=include_adult_keywords,
  )
  for geo_id in geo_ids:
    request.geo_target_constants.append(
        geo_service.geo_target_constant_path(geo_id)
    )

  if seeds and url:
    request.keyword_and_url_seed.url = url
    request.keyword_and_url_seed.keywords.extend(seeds)
    seed_kind = "keyword_and_url_seed"
  elif url:
    request.url_seed.url = url
    seed_kind = "url_seed"
  else:
    request.keyword_seed.keywords.extend(seeds)
    seed_kind = "keyword_seed"

  try:
    response = _call_with_retry(
        customer_id, lambda: service.generate_keyword_ideas(request=request)
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  ideas = []
  for idea in response:
    payload = {"text": idea.text}
    payload.update(_metrics_payload(idea.keyword_idea_metrics))
    ideas.append(payload)
    if len(ideas) >= max_results:
      break

  ideas.sort(key=lambda i: i["avg_monthly_searches"], reverse=True)
  result = {
      "mutations_performed": "none — GenerateKeywordIdeas is a read call",
      "customer_id": customer_id,
      "seed_kind": seed_kind,
      "keyword_seeds": seeds,
      "page_url": url,
      "geo_target_constant_ids": geo_ids,
      "language_constant_id": language_constant_id,
      "network": network.name,
      "returned": len(ideas),
      "truncated_at_max_results": len(ideas) >= max_results,
      "ideas": ideas,
  }
  if not ideas:
    result["note"] = _ZERO_RESULT_NOTE
  return result


@mcp.tool()
def generate_keyword_historical_metrics(
    customer_id: str,
    keywords: list[str],
    geo_target_constant_ids: list[int] | None = None,
    language_constant_id: str = "1000",
    include_search_partners: bool = False,
    include_adult_keywords: bool = False,
    login_customer_id: str | None = None,
) -> dict[str, Any]:
  """Returns historical metrics for specific keywords. Read-only.

  Companion to generate_keyword_ideas. Where that tool expands a seed
  into related terms, this one reports metrics for the exact keywords
  supplied, including the monthly search volume series when Google
  returns one. That series is the better basis for a demand judgement
  than a single averaged figure, and it still applies when idea
  generation returns nothing for a seed.

  Args:
      customer_id: Google Ads customer ID (digits only).
      keywords: Exact keywords to look up. Google de-duplicates close
          variants and reports which ones it folded together.
      geo_target_constant_ids: Geo target constant IDs. Defaults to
          [2840] (United States).
      language_constant_id: Language constant ID. Defaults to "1000".
      include_search_partners: False queries Google Search only.
      include_adult_keywords: Whether to include adult keywords.
      login_customer_id: MCC account ID if customer is managed.

  Returns:
      Dict with a `results` list of {text, close_variants,
      avg_monthly_searches, monthly_search_volumes, competition,
      competition_index, low/high_top_of_page_bid_micros and _usd}, plus
      `keywords_without_metrics` for anything Google returned no data
      for.
  """
  terms = [k for k in (keywords or []) if k and k.strip()]
  if not terms:
    raise ToolError("keywords must contain at least one non-empty term.")

  ads_client = _get_client(login_customer_id)
  service = ads_client.get_service("KeywordPlanIdeaService")
  geo_service = ads_client.get_service("GeoTargetConstantService")

  geo_ids = geo_target_constant_ids or [2840]
  network = _resolve_network(include_search_partners)

  request = service_types.GenerateKeywordHistoricalMetricsRequest(
      customer_id=customer_id,
      language=f"languageConstants/{language_constant_id}",
      keyword_plan_network=network,
      include_adult_keywords=include_adult_keywords,
  )
  request.keywords.extend(terms)
  for geo_id in geo_ids:
    request.geo_target_constants.append(
        geo_service.geo_target_constant_path(geo_id)
    )

  try:
    response = _call_with_retry(
        customer_id,
        lambda: service.generate_keyword_historical_metrics(request=request),
    )
  except GoogleAdsException as e:
    _handle_google_ads_error(e)

  results = []
  for row in response.results:
    payload = {
        "text": row.text,
        "close_variants": list(row.close_variants),
    }
    payload.update(_metrics_payload(row.keyword_metrics))
    payload["monthly_search_volumes"] = _monthly_series(row.keyword_metrics)
    # Google returns a row for a requested keyword even when it carries no
    # usable data — zero searches, UNSPECIFIED competition, empty series.
    # Flag that explicitly so a row's presence is not read as data.
    payload["has_metrics"] = bool(
        payload["avg_monthly_searches"]
        or payload["monthly_search_volumes"]
        or payload["competition"] not in ("UNSPECIFIED", "UNKNOWN")
    )
    results.append(payload)

  results.sort(key=lambda r: r["avg_monthly_searches"], reverse=True)

  covered = set()
  for row in results:
    covered.add(row["text"].strip().lower())
    covered.update(v.strip().lower() for v in row["close_variants"])
  missing = [t for t in terms if t.strip().lower() not in covered]
  empty = [r["text"] for r in results if not r["has_metrics"]]

  result = {
      "mutations_performed": (
          "none — GenerateKeywordHistoricalMetrics is a read call"
      ),
      "customer_id": customer_id,
      "requested": terms,
      "geo_target_constant_ids": geo_ids,
      "language_constant_id": language_constant_id,
      "network": network.name,
      "returned": len(results),
      "returned_with_metrics": sum(1 for r in results if r["has_metrics"]),
      "keywords_not_returned": missing,
      # Retained under the previous name for callers already reading it.
      "keywords_without_metrics": sorted(set(missing) | set(empty)),
      "keywords_returned_empty": empty,
      "results": results,
  }
  if not any(r["has_metrics"] for r in results):
    result["note"] = _ZERO_RESULT_NOTE
  return result
