# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the read-only Keyword Planner tool."""

import types
from unittest import mock

from ads_mcp.tools import planning
from fastmcp.exceptions import ToolError
import pytest


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
  """Keeps the 1 req/sec limiter from making the suite sleep."""
  monkeypatch.setattr(planning, "_MIN_INTERVAL_SECONDS", 0.0)
  planning._last_call_at.clear()


def _idea(text, searches, competition="LOW", index=25, low=1_000_000,
          high=4_000_000):
  return types.SimpleNamespace(
      text=text,
      keyword_idea_metrics=types.SimpleNamespace(
          avg_monthly_searches=searches,
          competition=types.SimpleNamespace(name=competition),
          competition_index=index,
          low_top_of_page_bid_micros=low,
          high_top_of_page_bid_micros=high,
      ),
  )


def _client(ideas):
  client = mock.Mock()
  idea_service = mock.Mock()
  idea_service.generate_keyword_ideas.return_value = ideas
  geo_service = mock.Mock()
  geo_service.geo_target_constant_path.side_effect = (
      lambda i: f"geoTargetConstants/{i}"
  )
  services = {
      "KeywordPlanIdeaService": idea_service,
      "GeoTargetConstantService": geo_service,
  }
  client.get_service.side_effect = lambda n: services[n]
  return client, services


@mock.patch("ads_mcp.tools.planning._get_client")
def test_requires_a_seed(mock_get_client):
  mock_get_client.return_value = mock.Mock()
  with pytest.raises(ToolError) as exc:
    planning.generate_keyword_ideas(customer_id="123")
  assert "at least one seed" in str(exc.value)


@mock.patch("ads_mcp.tools.planning._get_client")
def test_keyword_seed_and_defaults(mock_get_client):
  client, services = _client([_idea("physical therapy", 368000)])
  mock_get_client.return_value = client

  result = planning.generate_keyword_ideas(
      customer_id="123", keyword_seeds=["physical therapy"]
  )

  assert result["seed_kind"] == "keyword_seed"
  assert result["geo_target_constant_ids"] == [2840]
  assert result["network"] == "GOOGLE_SEARCH"
  assert result["mutations_performed"].startswith("none")

  request = services[
      "KeywordPlanIdeaService"
  ].generate_keyword_ideas.call_args.kwargs["request"]
  assert list(request.keyword_seed.keywords) == ["physical therapy"]
  assert request.language == "languageConstants/1000"
  assert list(request.geo_target_constants) == ["geoTargetConstants/2840"]


@mock.patch("ads_mcp.tools.planning._get_client")
def test_url_seed_and_combined_seed(mock_get_client):
  client, services = _client([_idea("a", 1)])
  mock_get_client.return_value = client

  result = planning.generate_keyword_ideas(
      customer_id="123", page_url="https://example.com/pt"
  )
  assert result["seed_kind"] == "url_seed"

  result = planning.generate_keyword_ideas(
      customer_id="123",
      keyword_seeds=["physical therapy"],
      page_url="https://example.com/pt",
  )
  assert result["seed_kind"] == "keyword_and_url_seed"
  request = services[
      "KeywordPlanIdeaService"
  ].generate_keyword_ideas.call_args.kwargs["request"]
  assert request.keyword_and_url_seed.url == "https://example.com/pt"


@mock.patch("ads_mcp.tools.planning._get_client")
def test_search_partners_switch(mock_get_client):
  client, _ = _client([_idea("a", 1)])
  mock_get_client.return_value = client

  result = planning.generate_keyword_ideas(
      customer_id="123",
      keyword_seeds=["x"],
      include_search_partners=True,
  )
  assert result["network"] == "GOOGLE_SEARCH_AND_PARTNERS"


@mock.patch("ads_mcp.tools.planning._get_client")
def test_metrics_and_sorting(mock_get_client):
  client, _ = _client([
      _idea("low volume", 100, "HIGH", 90, 2_000_000, 8_500_000),
      _idea("high volume", 5000),
  ])
  mock_get_client.return_value = client

  result = planning.generate_keyword_ideas(
      customer_id="123", keyword_seeds=["x"]
  )

  assert [i["text"] for i in result["ideas"]] == ["high volume", "low volume"]
  top = result["ideas"][1]
  assert top["competition"] == "HIGH"
  assert top["competition_index"] == 90
  assert top["low_top_of_page_bid_micros"] == 2_000_000
  assert top["low_top_of_page_bid_usd"] == 2.0
  assert top["high_top_of_page_bid_usd"] == 8.5


@mock.patch("ads_mcp.tools.planning._get_client")
def test_max_results_caps_and_flags_truncation(mock_get_client):
  client, _ = _client([_idea(f"kw{i}", i) for i in range(50)])
  mock_get_client.return_value = client

  result = planning.generate_keyword_ideas(
      customer_id="123", keyword_seeds=["x"], max_results=5
  )
  assert result["returned"] == 5
  assert result["truncated_at_max_results"] is True


@mock.patch("ads_mcp.tools.planning._get_client")
def test_never_touches_keyword_plan_services(mock_get_client):
  """The tool must not be able to create a saved Keyword Plan."""
  client, _ = _client([_idea("a", 1)])
  mock_get_client.return_value = client

  planning.generate_keyword_ideas(customer_id="123", keyword_seeds=["x"])

  requested = {call.args[0] for call in client.get_service.call_args_list}
  assert requested == {"KeywordPlanIdeaService", "GeoTargetConstantService"}
  assert not any("KeywordPlanService" in s for s in requested)


@mock.patch("ads_mcp.tools.planning._get_client")
def test_zero_results_note_makes_no_causal_claim(mock_get_client):
  """We verified zero results, not why. The note must not assert a cause."""
  client, _ = _client([])
  mock_get_client.return_value = client

  result = planning.generate_keyword_ideas(
      customer_id="123", keyword_seeds=["x"]
  )

  note = result["note"]
  assert note == (
      "Google returned zero keyword ideas for this seed. Do not interpret "
      "this as zero search demand. Retry with commercial/local-intent "
      "variants, URL/keyword+URL seeds, and/or historical keyword metrics "
      "before drawing a demand conclusion."
  )
  lowered = note.lower()
  for banned in ("suppress", "regulated", "healthcare", "health-vertical",
                 "sensitive"):
    assert banned not in lowered


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------


def test_throttle_spaces_calls_per_customer(monkeypatch):
  """Calls for one CID are spaced by the documented 1 req/sec limit."""
  monkeypatch.setattr(planning, "_MIN_INTERVAL_SECONDS", 1.0)
  planning._last_call_at.clear()
  slept = []
  monkeypatch.setattr(planning.time, "sleep", slept.append)
  clock = iter([0.0, 0.2, 1.0])
  monkeypatch.setattr(planning.time, "monotonic", lambda: next(clock))

  planning._throttle("123")   # first call, no wait
  planning._throttle("123")   # 0.2s later, must wait 0.8s

  assert slept and abs(slept[0] - 0.8) < 1e-9


def test_throttle_is_per_customer(monkeypatch):
  """A different CID has its own budget and is not made to wait."""
  monkeypatch.setattr(planning, "_MIN_INTERVAL_SECONDS", 1.0)
  planning._last_call_at.clear()
  slept = []
  monkeypatch.setattr(planning.time, "sleep", slept.append)
  monkeypatch.setattr(planning.time, "monotonic", lambda: 0.0)

  planning._throttle("123")
  planning._throttle("456")

  assert not slept


def test_retries_on_quota_error(monkeypatch):
  """A quota rejection is retried with backoff rather than surfaced."""
  monkeypatch.setattr(planning, "_MIN_INTERVAL_SECONDS", 0.0)
  planning._last_call_at.clear()
  monkeypatch.setattr(planning.time, "sleep", lambda _: None)
  monkeypatch.setattr(planning, "_is_quota_error", lambda _: True)

  calls = []

  def flaky():
    calls.append(1)
    if len(calls) < 3:
      exc = planning.GoogleAdsException.__new__(
          planning.GoogleAdsException
      )
      exc.failure = types.SimpleNamespace(errors=[])
      raise exc
    return "ok"

  assert planning._call_with_retry("123", flaky) == "ok"
  assert len(calls) == 3


def test_non_quota_error_is_not_retried(monkeypatch):
  monkeypatch.setattr(planning, "_MIN_INTERVAL_SECONDS", 0.0)
  planning._last_call_at.clear()
  monkeypatch.setattr(planning, "_is_quota_error", lambda _: False)

  calls = []

  def failing():
    calls.append(1)
    exc = planning.GoogleAdsException.__new__(planning.GoogleAdsException)
    exc.failure = types.SimpleNamespace(errors=[])
    raise exc

  with pytest.raises(planning.GoogleAdsException):
    planning._call_with_retry("123", failing)
  assert len(calls) == 1


# ---------------------------------------------------------------------------
# Historical metrics
# ---------------------------------------------------------------------------


def _hist_result(text, searches, variants=(), months=()):
  return types.SimpleNamespace(
      text=text,
      close_variants=list(variants),
      keyword_metrics=types.SimpleNamespace(
          avg_monthly_searches=searches,
          competition=types.SimpleNamespace(name="MEDIUM"),
          competition_index=44,
          low_top_of_page_bid_micros=2_000_000,
          high_top_of_page_bid_micros=9_000_000,
          monthly_search_volumes=[
              types.SimpleNamespace(
                  year=y,
                  month=types.SimpleNamespace(name=m),
                  monthly_searches=v,
              )
              for y, m, v in months
          ],
      ),
  )


def _hist_client(results):
  client = mock.Mock()
  idea_service = mock.Mock()
  idea_service.generate_keyword_historical_metrics.return_value = (
      types.SimpleNamespace(results=results)
  )
  geo_service = mock.Mock()
  geo_service.geo_target_constant_path.side_effect = (
      lambda i: f"geoTargetConstants/{i}"
  )
  services = {
      "KeywordPlanIdeaService": idea_service,
      "GeoTargetConstantService": geo_service,
  }
  client.get_service.side_effect = lambda n: services[n]
  return client, services


@mock.patch("ads_mcp.tools.planning._get_client")
def test_historical_metrics_requires_keywords(mock_get_client):
  mock_get_client.return_value = mock.Mock()
  with pytest.raises(ToolError):
    planning.generate_keyword_historical_metrics(
        customer_id="123", keywords=[]
    )


@mock.patch("ads_mcp.tools.planning._get_client")
def test_historical_metrics_returns_series(mock_get_client):
  client, services = _hist_client([
      _hist_result(
          "scoliosis treatment",
          880,
          variants=["scoliosis treatments"],
          months=[(2026, "JUNE", 900), (2026, "JULY", 860)],
      )
  ])
  mock_get_client.return_value = client

  result = planning.generate_keyword_historical_metrics(
      customer_id="123",
      keywords=["scoliosis treatment", "not returned by google"],
  )

  row = result["results"][0]
  assert row["avg_monthly_searches"] == 880
  assert row["competition"] == "MEDIUM"
  assert row["competition_index"] == 44
  assert row["low_top_of_page_bid_usd"] == 2.0
  assert row["high_top_of_page_bid_usd"] == 9.0
  assert row["close_variants"] == ["scoliosis treatments"]
  assert row["monthly_search_volumes"] == [
      {"year": 2026, "month": "JUNE", "monthly_searches": 900},
      {"year": 2026, "month": "JULY", "monthly_searches": 860},
  ]
  assert row["has_metrics"] is True
  assert result["keywords_not_returned"] == ["not returned by google"]
  assert result["keywords_without_metrics"] == ["not returned by google"]
  assert result["returned_with_metrics"] == 1
  assert result["mutations_performed"].startswith("none")

  request = services[
      "KeywordPlanIdeaService"
  ].generate_keyword_historical_metrics.call_args.kwargs["request"]
  assert list(request.keywords) == [
      "scoliosis treatment",
      "not returned by google",
  ]


@mock.patch("ads_mcp.tools.planning._get_client")
def test_historical_metrics_flags_rows_returned_empty(mock_get_client):
  """A returned row with no usable data must not read as data."""
  empty = _hist_result("scoliosis", 0)
  empty.keyword_metrics.competition = types.SimpleNamespace(
      name="UNSPECIFIED"
  )
  empty.keyword_metrics.competition_index = 0
  empty.keyword_metrics.low_top_of_page_bid_micros = 0
  empty.keyword_metrics.high_top_of_page_bid_micros = 0
  client, _ = _hist_client([empty])
  mock_get_client.return_value = client

  result = planning.generate_keyword_historical_metrics(
      customer_id="123", keywords=["scoliosis"]
  )

  assert result["returned"] == 1
  assert result["returned_with_metrics"] == 0
  assert result["results"][0]["has_metrics"] is False
  assert result["keywords_returned_empty"] == ["scoliosis"]
  assert result["keywords_not_returned"] == []
  # Every row came back empty, so the caller gets the no-causal-claim note.
  assert result["note"].startswith("Google returned zero keyword ideas")


@mock.patch("ads_mcp.tools.planning._get_client")
def test_historical_metrics_close_variants_count_as_covered(mock_get_client):
  client, _ = _hist_client([
      _hist_result("chiropractor", 1000, variants=["Chiropractors"])
  ])
  mock_get_client.return_value = client

  result = planning.generate_keyword_historical_metrics(
      customer_id="123", keywords=["chiropractor", "chiropractors"]
  )
  assert result["keywords_without_metrics"] == []


@mock.patch("ads_mcp.tools.planning._get_client")
def test_historical_metrics_never_touches_keyword_plan_services(
    mock_get_client,
):
  client, _ = _hist_client([_hist_result("a", 1)])
  mock_get_client.return_value = client

  planning.generate_keyword_historical_metrics(
      customer_id="123", keywords=["a"]
  )

  requested = {call.args[0] for call in client.get_service.call_args_list}
  assert requested == {"KeywordPlanIdeaService", "GeoTargetConstantService"}
