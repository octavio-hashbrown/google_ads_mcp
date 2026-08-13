# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the gated Manual CPC bidding tool."""

import types
from unittest import mock

from ads_mcp.tools import gated_bidding
from ads_mcp.tools import mutations_gated
from fastmcp.exceptions import ToolError
import pytest


CAMPAIGN = "customers/123/campaigns/456"


def _campaign_row(
    *,
    name="Test Campaign",
    status="PAUSED",
    strategy="MAXIMIZE_CONVERSIONS",
    portfolio="",
    max_conv_tcpa=0,
    target_cpa=0,
):
  """Builds a GAQL row for the campaign bidding query."""
  return types.SimpleNamespace(
      campaign=types.SimpleNamespace(
          name=name,
          status=types.SimpleNamespace(name=status),
          bidding_strategy_type=types.SimpleNamespace(name=strategy),
          bidding_strategy=portfolio,
          advertising_channel_type=types.SimpleNamespace(name="SEARCH"),
          maximize_conversions=types.SimpleNamespace(
              target_cpa_micros=max_conv_tcpa
          ),
          target_cpa=types.SimpleNamespace(target_cpa_micros=target_cpa),
      )
  )


def _ad_group_row(name="AG", status="ENABLED", bid=2_000_000):
  return types.SimpleNamespace(
      ad_group=types.SimpleNamespace(
          name=name,
          status=types.SimpleNamespace(name=status),
          cpc_bid_micros=bid,
      )
  )


def _client(search_side_effect):
  client = mock.Mock()
  service = mock.Mock()
  client.get_service.return_value = service
  service.search.side_effect = search_side_effect
  return client, service


def test_registered_in_shared_gate():
  """The executor rides the existing approval gate, not a parallel one."""
  assert "set_manual_cpc" in mutations_gated._DISPATCH


def test_register_executor_rejects_duplicate():
  """Silent replacement would let a module hijack approved proposals."""
  with pytest.raises(ValueError):
    mutations_gated.register_executor("set_manual_cpc", lambda *_: None)


@mock.patch("ads_mcp.tools.gated_bidding._get_client")
@mock.patch("ads_mcp.tools.gated_bidding.audit.resolve_client_root")
def test_propose_refuses_enabled_campaign(mock_root, mock_get_client, tmp_path):
  """An ENABLED campaign is refused without explicit authorization."""
  mock_root.return_value = tmp_path
  client, _ = _client([[_campaign_row(status="ENABLED")]])
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gated_bidding.propose_set_manual_cpc(
        customer_id="123",
        campaign_resource_name=CAMPAIGN,
        reason_code="client_request",
    )
  assert "ENABLED and serving" in str(exc.value)


@mock.patch("ads_mcp.tools.gated_bidding._get_client")
@mock.patch("ads_mcp.tools.gated_bidding.audit.resolve_client_root")
def test_propose_refuses_when_already_manual(
    mock_root, mock_get_client, tmp_path
):
  """Idempotency: no approval is spent on a no-op."""
  mock_root.return_value = tmp_path
  client, _ = _client([[_campaign_row(strategy="MANUAL_CPC")]])
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gated_bidding.propose_set_manual_cpc(
        customer_id="123",
        campaign_resource_name=CAMPAIGN,
        reason_code="client_request",
    )
  assert "already on MANUAL_CPC" in str(exc.value)


@mock.patch("ads_mcp.tools.gated_bidding._get_client")
@mock.patch("ads_mcp.tools.gated_bidding.audit.resolve_client_root")
def test_propose_refuses_portfolio_strategy(
    mock_root, mock_get_client, tmp_path
):
  """Detaching from a portfolio strategy affects other campaigns."""
  mock_root.return_value = tmp_path
  client, _ = _client(
      [[_campaign_row(portfolio="customers/123/biddingStrategies/9")]]
  )
  mock_get_client.return_value = client

  with pytest.raises(ToolError) as exc:
    gated_bidding.propose_set_manual_cpc(
        customer_id="123",
        campaign_resource_name=CAMPAIGN,
        reason_code="client_request",
    )
  assert "portfolio bidding strategy" in str(exc.value)


@mock.patch("ads_mcp.tools.gated_bidding._get_client")
@mock.patch("ads_mcp.tools.gated_bidding.audit.resolve_client_root")
def test_propose_writes_before_after_and_context(
    mock_root, mock_get_client, tmp_path
):
  """The proposal states the transition and the bids that take over."""
  mock_root.return_value = tmp_path
  client, _ = _client([
      [_campaign_row(max_conv_tcpa=70_000_000)],
      [_ad_group_row("Montvale", bid=3_500_000), _ad_group_row("Closter", bid=0)],
  ])
  mock_get_client.return_value = client

  result = gated_bidding.propose_set_manual_cpc(
      customer_id="123",
      campaign_resource_name=CAMPAIGN,
      reason_code="client_request",
  )

  block = result["block"]
  assert "Bidding BEFORE: MAXIMIZE_CONVERSIONS" in block
  assert "Bidding AFTER:  MANUAL_CPC" in block
  assert "$70.00" in block  # live tCPA stops applying
  assert "Montvale" in block and "$3.50" in block
  assert "NOT SET" in block  # ad group with no bid is called out
  assert "at least one ad group has no CPC bid" in block


def test_execute_no_op_when_already_manual():
  """Re-applying an approved change is a no-op, not a second mutation."""
  client, service = _client([[_campaign_row(strategy="MANUAL_CPC")]])
  result = gated_bidding._execute_set_manual_cpc(
      client,
      "123",
      {
          "campaign_resource_name": CAMPAIGN,
          "enhanced_cpc_enabled": False,
          "allow_enabled_campaign": False,
          "before_bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
          "before_status": "PAUSED",
      },
  )
  assert result["outcome"] == "no_op"
  service.mutate_campaigns.assert_not_called()


def test_execute_refuses_on_strategy_drift():
  """If bidding moved since approval, the human approved a stale transition."""
  client, service = _client([[_campaign_row(strategy="TARGET_SPEND")]])
  with pytest.raises(ToolError) as exc:
    gated_bidding._execute_set_manual_cpc(
        client,
        "123",
        {
            "campaign_resource_name": CAMPAIGN,
            "enhanced_cpc_enabled": False,
            "allow_enabled_campaign": False,
            "before_bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
            "before_status": "PAUSED",
        },
    )
  assert "changed since this was proposed" in str(exc.value)
  service.mutate_campaigns.assert_not_called()


def test_execute_refuses_campaign_enabled_after_approval():
  """A campaign that went live after approval is not silently mutated."""
  client, service = _client([[_campaign_row(status="ENABLED")]])
  with pytest.raises(ToolError) as exc:
    gated_bidding._execute_set_manual_cpc(
        client,
        "123",
        {
            "campaign_resource_name": CAMPAIGN,
            "enhanced_cpc_enabled": False,
            "allow_enabled_campaign": False,
            "before_bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
            "before_status": "PAUSED",
        },
    )
  assert "did not authorize" in str(exc.value)
  service.mutate_campaigns.assert_not_called()


def test_execute_applies_and_verifies():
  """Happy path sets manual_cpc with a correct mask and reads back."""
  client, service = _client([
      [_campaign_row()],  # pre-check
      [_campaign_row(strategy="MANUAL_CPC")],  # post-apply verification
  ])
  result = gated_bidding._execute_set_manual_cpc(
      client,
      "123",
      {
          "campaign_resource_name": CAMPAIGN,
          "enhanced_cpc_enabled": False,
          "allow_enabled_campaign": False,
          "before_bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
          "before_status": "PAUSED",
      },
  )

  assert result["outcome"] == "applied"
  assert result["before"] == "MAXIMIZE_CONVERSIONS"
  assert result["after"] == "MANUAL_CPC"

  operation = service.mutate_campaigns.call_args.kwargs["operations"][0]
  # Verified against the live API: a bare "manual_cpc" path is rejected
  # with FieldMaskError, the leaf path is accepted.
  assert list(operation.update_mask.paths) == ["manual_cpc.enhanced_cpc_enabled"]
  assert operation.update.resource_name == CAMPAIGN
  # No campaign-level CPC ceiling is invented.
  assert "cpc_bid_ceiling_micros" not in str(operation.update)


def test_execute_fails_loudly_if_verification_disagrees():
  """A silent partial apply is worse than a loud failure."""
  client, _ = _client([
      [_campaign_row()],
      [_campaign_row(strategy="MAXIMIZE_CONVERSIONS")],  # never took effect
  ])
  with pytest.raises(ToolError) as exc:
    gated_bidding._execute_set_manual_cpc(
        client,
        "123",
        {
            "campaign_resource_name": CAMPAIGN,
            "enhanced_cpc_enabled": False,
            "allow_enabled_campaign": False,
            "before_bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
            "before_status": "PAUSED",
        },
    )
  assert "verification failed" in str(exc.value)
