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

"""Tests for criterion mutation tools."""

from unittest import mock
from ads_mcp.tools.mutations import criterion
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v24.errors.types import errors as error_types
import pytest


class MockGoogleAdsException(GoogleAdsException):
  """Mock GoogleAdsException for testing."""

  def __init__(self, errors):  # pylint: disable=super-init-not-called
    self.failure = mock.Mock()
    self.failure.errors = errors


def _keyword_row(text, match_type):
  """Builds a GAQL row shaped like an ad_group_criterion keyword result."""
  row = mock.Mock()
  row.ad_group_criterion.keyword.text = text
  row.ad_group_criterion.keyword.match_type.name = match_type
  return row


def _policy_failure(index, *, policy_name, violating_text, exemptible):
  """Builds a real GoogleAdsFailure carrying a policy violation.

  Real protos, not mocks — the parser reads field presence via the
  underlying pb2 message, which a Mock cannot emulate faithfully.
  """
  failure = error_types.GoogleAdsFailure()
  error = failure.errors._pb.add()  # pylint: disable=protected-access
  error.message = "Policy violation"
  details = error.details.policy_violation_details
  details.external_policy_name = policy_name
  details.external_policy_description = "policy description"
  details.is_exemptible = exemptible
  details.key.policy_name = policy_name
  details.key.violating_text = violating_text
  element = error.location.field_path_elements.add()
  element.field_name = "operations"
  element.index = index

  exception = MockGoogleAdsException([])
  exception.failure = failure
  return exception


def _client_with(mock_get_client, *, existing=(), mutate_side_effect=None):
  """Wires a mock client whose services share one mock, as upstream does."""
  mock_client = mock.Mock()
  mock_get_client.return_value = mock_client
  mock_service = mock.Mock()
  mock_client.get_service.return_value = mock_service
  mock_service.search.return_value = list(existing)
  if mutate_side_effect is not None:
    mock_service.mutate_ad_group_criteria.side_effect = mutate_side_effect
  return mock_service


def _results(*resource_names):
  response = mock.Mock()
  response.results = [
      mock.Mock(resource_name=rn) for rn in resource_names
  ]
  return response


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_create_keywords_success(mock_get_client):
  """Clean keywords are dry-run first, then created for real."""
  mock_service = _client_with(
      mock_get_client,
      mutate_side_effect=[
          _results(),  # dry run passes
          _results(
              "customers/123/adGroupCriteria/kw1",
              "customers/123/adGroupCriteria/kw2",
          ),
      ],
  )

  result = criterion.create_keywords(
      customer_id="123",
      ad_group_resource_name="customers/123/adGroups/abc",
      keywords=[
          {"text": "keyword1", "match_type": "EXACT"},
          {"text": "keyword2", "match_type": "PHRASE"},
      ],
  )

  assert result["resource_names"] == [
      "customers/123/adGroupCriteria/kw1",
      "customers/123/adGroupCriteria/kw2",
  ]
  assert len(result["created"]) == 2
  assert not result["blocked_needs_authorization"]
  assert not result["blocked_not_exemptible"]

  calls = mock_service.mutate_ad_group_criteria.call_args_list
  assert len(calls) == 2
  assert calls[0].kwargs["request"].validate_only is True
  assert calls[1].kwargs["request"].validate_only is False


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_create_keywords_failure(mock_get_client):
  """A non-policy error surfaces and nothing is written."""
  mock_error = mock.Mock()
  mock_error.message = "Invalid keyword"
  mock_error.details = None
  mock_error.location.field_path_elements = []

  mock_service = _client_with(
      mock_get_client,
      mutate_side_effect=MockGoogleAdsException([mock_error]),
  )

  with pytest.raises(ToolError) as exc_info:
    criterion.create_keywords(
        customer_id="123",
        ad_group_resource_name="customers/123/adGroups/abc",
        keywords=[{"text": "keyword1", "match_type": "EXACT"}],
    )

  assert "Invalid keyword" in str(exc_info.value)
  # Only the dry run ran; the real mutate was never attempted.
  assert mock_service.mutate_ad_group_criteria.call_count == 1
  assert (
      mock_service.mutate_ad_group_criteria.call_args_list[0]
      .kwargs["request"]
      .validate_only
      is True
  )


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_create_keywords_skips_existing(mock_get_client):
  """Keywords already in the ad group are skipped, not re-sent."""
  mock_service = _client_with(
      mock_get_client,
      existing=[_keyword_row("Keyword1", "EXACT")],
      mutate_side_effect=[
          _results(),
          _results("customers/123/adGroupCriteria/kw2"),
      ],
  )

  result = criterion.create_keywords(
      customer_id="123",
      ad_group_resource_name="customers/123/adGroups/abc",
      keywords=[
          {"text": "keyword1", "match_type": "EXACT"},
          {"text": "keyword2", "match_type": "PHRASE"},
      ],
  )

  assert result["skipped_already_exists"] == [
      {"text": "keyword1", "match_type": "EXACT"}
  ]
  assert len(result["created"]) == 1
  dry_run = mock_service.mutate_ad_group_criteria.call_args_list[0]
  assert len(dry_run.kwargs["request"].operations) == 1


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_create_keywords_exemptible_without_authorization(mock_get_client):
  """Unauthorized exemptible violations are reported, never resubmitted."""
  mock_service = _client_with(
      mock_get_client,
      mutate_side_effect=[
          _policy_failure(
              0,
              policy_name="HEALTH_IN_PERSONALIZED_ADS",
              violating_text="scoliosis treatment",
              exemptible=True,
          )
      ],
  )

  result = criterion.create_keywords(
      customer_id="123",
      ad_group_resource_name="customers/123/adGroups/abc",
      keywords=[{"text": "scoliosis treatment", "match_type": "PHRASE"}],
  )

  assert not result["created"]
  assert not result["created_with_exemption"]
  blocked = result["blocked_needs_authorization"]
  assert len(blocked) == 1
  assert blocked[0]["exemption_keys"] == [
      {
          "policy_name": "HEALTH_IN_PERSONALIZED_ADS",
          "violating_text": "scoliosis treatment",
      }
  ]
  # Dry run only. No write was attempted.
  assert mock_service.mutate_ad_group_criteria.call_count == 1


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_create_keywords_exemption_uses_returned_keys(mock_get_client):
  """When authorized, the resubmit carries Google's exact key verbatim."""
  mock_service = _client_with(
      mock_get_client,
      mutate_side_effect=[
          _policy_failure(
              0,
              policy_name="HEALTH_IN_PERSONALIZED_ADS",
              violating_text="scoliosis treatment",
              exemptible=True,
          ),
          _results("customers/123/adGroupCriteria/kw1"),
      ],
  )

  result = criterion.create_keywords(
      customer_id="123",
      ad_group_resource_name="customers/123/adGroups/abc",
      keywords=[{"text": "scoliosis treatment", "match_type": "PHRASE"}],
      request_policy_exemptions=True,
  )

  assert len(result["created_with_exemption"]) == 1
  resubmit = mock_service.mutate_ad_group_criteria.call_args_list[1]
  operation = resubmit.kwargs["request"].operations[0]
  keys = operation.exempt_policy_violation_keys
  assert len(keys) == 1
  assert keys[0].policy_name == "HEALTH_IN_PERSONALIZED_ADS"
  assert keys[0].violating_text == "scoliosis treatment"
  # Keyword text and match type are preserved byte-for-byte.
  assert operation.create.keyword.text == "scoliosis treatment"
  assert operation.create.keyword.match_type.name == "PHRASE"


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_create_keywords_never_exempts_non_exemptible(mock_get_client):
  """A non-exemptible violation is never resubmitted, even if authorized."""
  mock_service = _client_with(
      mock_get_client,
      mutate_side_effect=[
          _policy_failure(
              0,
              policy_name="DANGEROUS_PRODUCTS",
              violating_text="banned term",
              exemptible=False,
          )
      ],
  )

  result = criterion.create_keywords(
      customer_id="123",
      ad_group_resource_name="customers/123/adGroups/abc",
      keywords=[{"text": "banned term", "match_type": "EXACT"}],
      request_policy_exemptions=True,
  )

  assert not result["created"]
  assert not result["created_with_exemption"]
  assert len(result["blocked_not_exemptible"]) == 1
  assert mock_service.mutate_ad_group_criteria.call_count == 1


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_create_keywords_validate_only_writes_nothing(mock_get_client):
  """validate_only reports findings and never issues a real mutate."""
  mock_service = _client_with(
      mock_get_client, mutate_side_effect=[_results()]
  )

  result = criterion.create_keywords(
      customer_id="123",
      ad_group_resource_name="customers/123/adGroups/abc",
      keywords=[{"text": "keyword1", "match_type": "EXACT"}],
      validate_only=True,
  )

  assert result["validate_only"] is True
  assert not result["resource_names"]
  assert result["would_create"] == [
      {"text": "keyword1", "match_type": "EXACT"}
  ]
  assert mock_service.mutate_ad_group_criteria.call_count == 1
  assert (
      mock_service.mutate_ad_group_criteria.call_args_list[0]
      .kwargs["request"]
      .validate_only
      is True
  )


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_create_negative_campaign_keywords_success(mock_get_client):
  """Tests successful creation of negative campaign keywords."""
  mock_client = mock.Mock()
  mock_get_client.return_value = mock_client
  mock_service = mock.Mock()
  mock_client.get_service.return_value = mock_service

  mock_response = mock.Mock()
  mock_response.results = [
      mock.Mock(resource_name="customers/123/campaignCriteria/nc1"),
      mock.Mock(resource_name="customers/123/campaignCriteria/nc2"),
  ]
  mock_service.mutate_campaign_criteria.return_value = mock_response

  result = criterion.create_negative_campaign_keywords(
      customer_id="123",
      campaign_resource_name="customers/123/campaigns/789",
      keywords=["free", "fake"],
  )

  assert result == {
      "resource_names": [
          "customers/123/campaignCriteria/nc1",
          "customers/123/campaignCriteria/nc2",
      ]
  }
  mock_service.mutate_campaign_criteria.assert_called_once()


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_create_negative_campaign_keywords_failure(mock_get_client):
  """Tests create_negative_campaign_keywords handles GoogleAdsException."""
  mock_client = mock.Mock()
  mock_get_client.return_value = mock_client
  mock_service = mock.Mock()
  mock_client.get_service.return_value = mock_service

  mock_error = mock.Mock()
  mock_error.__str__ = mock.Mock(return_value="Invalid negative keyword")

  mock_service.mutate_campaign_criteria.side_effect = MockGoogleAdsException(
      [mock_error]
  )

  with pytest.raises(ToolError) as exc_info:
    criterion.create_negative_campaign_keywords(
        customer_id="123",
        campaign_resource_name="customers/123/campaigns/789",
        keywords=["free"],
    )

  assert "Invalid negative keyword" in str(exc_info.value)


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_create_geo_targeting_success(mock_get_client):
  """Tests create_geo_targeting successfully adds location targeting."""
  mock_client = mock.Mock()
  mock_get_client.return_value = mock_client
  mock_service = mock.Mock()
  mock_client.get_service.return_value = mock_service
  mock_geo_svc = mock.Mock()
  mock_client.get_service.side_effect = lambda name: {
      "CampaignCriterionService": mock_service,
      "GeoTargetConstantService": mock_geo_svc,
  }[name]

  mock_geo_svc.geo_target_constant_path.side_effect = (
      lambda geo_id: f"geoTargetConstants/{geo_id}"
  )

  mock_response = mock.Mock()
  mock_response.results = [
      mock.Mock(resource_name="customers/123/campaignCriteria/geo1"),
      mock.Mock(resource_name="customers/123/campaignCriteria/geo2"),
  ]
  mock_service.mutate_campaign_criteria.return_value = mock_response

  result = criterion.create_geo_targeting(
      customer_id="123",
      campaign_resource_name="customers/123/campaigns/789",
      geo_target_constant_ids=[2840, 2124],
  )

  assert result == {
      "resource_names": [
          "customers/123/campaignCriteria/geo1",
          "customers/123/campaignCriteria/geo2",
      ]
  }
  mock_service.mutate_campaign_criteria.assert_called_once()


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_create_geo_targeting_failure(mock_get_client):
  """Tests create_geo_targeting handles GoogleAdsException."""
  mock_client = mock.Mock()
  mock_get_client.return_value = mock_client
  mock_service = mock.Mock()
  mock_client.get_service.return_value = mock_service
  mock_geo_svc = mock.Mock()
  mock_client.get_service.side_effect = lambda name: {
      "CampaignCriterionService": mock_service,
      "GeoTargetConstantService": mock_geo_svc,
  }[name]

  mock_geo_svc.geo_target_constant_path.side_effect = (
      lambda geo_id: f"geoTargetConstants/{geo_id}"
  )

  mock_error = mock.Mock()
  mock_error.__str__ = mock.Mock(return_value="Invalid geo ID")

  mock_service.mutate_campaign_criteria.side_effect = MockGoogleAdsException(
      [mock_error]
  )

  with pytest.raises(ToolError) as exc_info:
    criterion.create_geo_targeting(
        customer_id="123",
        campaign_resource_name="customers/123/campaigns/789",
        geo_target_constant_ids=[2840],
    )

  assert "Invalid geo ID" in str(exc_info.value)


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_remove_campaign_criterion_success(mock_get_client):
  """Tests remove_campaign_criterion successfully removes a criterion."""
  mock_client = mock.Mock()
  mock_get_client.return_value = mock_client
  mock_service = mock.Mock()
  mock_client.get_service.return_value = mock_service

  mock_service.campaign_criterion_path.return_value = (
      "customers/123/campaignCriteria/789~abc"
  )

  mock_response = mock.Mock()
  mock_response.results = [
      mock.Mock(resource_name="customers/123/campaignCriteria/789~abc")
  ]
  mock_service.mutate_campaign_criteria.return_value = mock_response

  result = criterion.remove_campaign_criterion(
      customer_id="123",
      campaign_id="789",
      criterion_id="abc",
  )

  assert result == {"removed": "customers/123/campaignCriteria/789~abc"}
  mock_service.mutate_campaign_criteria.assert_called_once()


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_remove_campaign_criterion_failure(mock_get_client):
  """Tests remove_campaign_criterion handles GoogleAdsException."""
  mock_client = mock.Mock()
  mock_get_client.return_value = mock_client
  mock_service = mock.Mock()
  mock_client.get_service.return_value = mock_service

  mock_service.campaign_criterion_path.return_value = (
      "customers/123/campaignCriteria/789~abc"
  )

  mock_error = mock.Mock()
  mock_error.__str__ = mock.Mock(return_value="Criterion not found")

  mock_service.mutate_campaign_criteria.side_effect = MockGoogleAdsException(
      [mock_error]
  )

  with pytest.raises(ToolError) as exc_info:
    criterion.remove_campaign_criterion(
        customer_id="123",
        campaign_id="789",
        criterion_id="abc",
    )

  assert "Criterion not found" in str(exc_info.value)


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_exclude_geo_targets_success(mock_get_client):
  """Tests exclude_geo_targets successfully excludes locations."""
  mock_client = mock.Mock()
  mock_get_client.return_value = mock_client
  mock_service = mock.Mock()
  mock_client.get_service.return_value = mock_service
  mock_geo_svc = mock.Mock()
  mock_client.get_service.side_effect = lambda name: {
      "CampaignCriterionService": mock_service,
      "GeoTargetConstantService": mock_geo_svc,
  }[name]

  mock_geo_svc.geo_target_constant_path.side_effect = (
      lambda geo_id: f"geoTargetConstants/{geo_id}"
  )

  mock_response = mock.Mock()
  mock_response.results = [
      mock.Mock(resource_name="customers/123/campaignCriteria/geo1"),
      mock.Mock(resource_name="customers/123/campaignCriteria/geo2"),
  ]
  mock_service.mutate_campaign_criteria.return_value = mock_response

  result = criterion.exclude_geo_targets(
      customer_id="123",
      campaign_resource_name="customers/123/campaigns/789",
      geo_target_constant_ids=[2840, 2124],
  )

  assert result == {
      "resource_names": [
          "customers/123/campaignCriteria/geo1",
          "customers/123/campaignCriteria/geo2",
      ]
  }
  mock_service.mutate_campaign_criteria.assert_called_once()


@mock.patch("ads_mcp.tools.mutations.criterion._get_client")
def test_exclude_geo_targets_failure(mock_get_client):
  """Tests exclude_geo_targets handles GoogleAdsException."""
  mock_client = mock.Mock()
  mock_get_client.return_value = mock_client
  mock_service = mock.Mock()
  mock_client.get_service.return_value = mock_service
  mock_geo_svc = mock.Mock()
  mock_client.get_service.side_effect = lambda name: {
      "CampaignCriterionService": mock_service,
      "GeoTargetConstantService": mock_geo_svc,
  }[name]

  mock_geo_svc.geo_target_constant_path.side_effect = (
      lambda geo_id: f"geoTargetConstants/{geo_id}"
  )

  mock_error = mock.Mock()
  mock_error.__str__ = mock.Mock(return_value="Invalid geo ID")

  mock_service.mutate_campaign_criteria.side_effect = MockGoogleAdsException(
      [mock_error]
  )

  with pytest.raises(ToolError) as exc_info:
    criterion.exclude_geo_targets(
        customer_id="123",
        campaign_resource_name="customers/123/campaigns/789",
        geo_target_constant_ids=[2840],
    )

  assert "Invalid geo ID" in str(exc_info.value)
