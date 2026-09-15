# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""The supersession surface, asserted on what the server actually registers.

PR #7 put the collision guard under every propose tool but exposed
`supersedes=` on only one of them, so twelve tools could refuse a retired
code while naming a remedy their own signature did not offer.

These tests assert against the REGISTERED tools, not against a list in the
source. That distinction is the whole reason the raw-mutation gate went
undetected for months: a tool is exposed by being imported, and a registry
is the only honest answer to "what does a caller actually see?".
"""

import inspect
import pathlib
import types
from unittest import mock

import pytest
from fastmcp.exceptions import ToolError

from ads_mcp.governance import approval
from ads_mcp.tools import (
    gated_ad_copy,
    gated_assets,
    gated_bidding,
    gated_conversions,
    gated_migration,
    mutations_gated,
)

LIFECYCLE_ARGS = ("supersedes", "supersedes_evidence")

# Every gated propose tool, with the module that defines it.
PROPOSE_TOOLS = [
    (mutations_gated, "propose_pause_keyword"),
    (mutations_gated, "propose_enable_keyword"),
    (mutations_gated, "propose_remove_keyword"),
    (mutations_gated, "propose_add_ad_group_negative"),
    (mutations_gated, "propose_add_campaign_negative"),
    (mutations_gated, "propose_add_to_shared_negative_list"),
    (gated_assets, "propose_attach_call_asset_to_ad_group"),
    (gated_assets, "propose_attach_call_asset_to_campaign"),
    (gated_assets, "propose_detach_call_asset_from_campaign"),
    (gated_bidding, "propose_set_manual_cpc"),
    (gated_conversions, "propose_set_conversion_actions_primary_for_goal"),
    (gated_migration, "propose_campaign_migration"),
    (gated_ad_copy, "propose_update_responsive_search_ads"),
]
IDS = [name for _, name in PROPOSE_TOOLS]


def _registered_tools():
  """The tools a client would actually be offered, governed tier loaded."""
  import asyncio

  from ads_mcp.coordinator import mcp_server

  listed = mcp_server.list_tools()
  if inspect.isawaitable(listed):
    listed = asyncio.run(listed)
  return {t.name: t for t in listed}


# ---------------------------------------------------------------------------
# the surface, as registered
# ---------------------------------------------------------------------------


def test_registry_holds_exactly_the_propose_tools_under_test():
  """A new propose tool must not slip past this file unnoticed."""
  registered = {n for n in _registered_tools() if n.startswith("propose_")}
  assert registered == set(IDS)


@pytest.mark.parametrize("name", IDS)
def test_every_propose_tool_exposes_the_lifecycle_arguments(name):
  """The remedy the guard names must exist on the tool that names it."""
  props = _registered_tools()[name].parameters["properties"]
  missing = [a for a in LIFECYCLE_ARGS if a not in props]
  assert not missing, f"{name} does not expose {missing}"


@pytest.mark.parametrize("name", IDS)
def test_every_propose_tool_documents_the_lifecycle_arguments(name):
  """An undocumented argument is not a usable one."""
  props = _registered_tools()[name].parameters["properties"]
  for arg in LIFECYCLE_ARGS:
    assert props[arg].get("description"), f"{name}.{arg} has no description"
  assert "retired" in props["supersedes"]["description"]
  assert "APPLIED" in props["supersedes_evidence"]["description"]


@pytest.mark.parametrize("module,name", PROPOSE_TOOLS, ids=IDS)
def test_every_propose_tool_forwards_the_lifecycle_arguments(module, name):
  """Declaring the arguments and dropping them would be worse than neither.

  Source-level on purpose: the failure this guards against is a silently
  ignored keyword, which a signature check cannot see and which would
  otherwise need thirteen separate API harnesses to reach.
  """
  src = inspect.getsource(getattr(module, name))
  for arg in LIFECYCLE_ARGS:
    assert f"{arg}={arg}," in src, f"{name} never forwards {arg}"


# ---------------------------------------------------------------------------
# the docstring interpolation these arguments ride on
# ---------------------------------------------------------------------------


def test_no_registered_tool_leaks_a_format_placeholder():
  """The shared Args block used to be interpolated AFTER registration.

  The result was a literal "%s" in the description every caller received,
  and a docstring the parser could not read, so no argument documentation
  survived at all.
  """
  leaked = [n for n, t in _registered_tools().items()
            if "%s" in (t.description or "")]
  assert not leaked, f"unsubstituted placeholder in: {leaked}"


@pytest.mark.parametrize("name", IDS)
def test_shared_args_block_reaches_every_propose_tool(name):
  """reason_code's vocabulary is only useful if the caller can read it."""
  props = _registered_tools()[name].parameters["properties"]
  reason = props["reason_code"].get("description") or ""
  assert "underperforming" in reason and "client_request" in reason
  assert props["reason_detail"].get("description")
  assert props["client_root"].get("description")


@pytest.mark.parametrize("name", IDS)
def test_every_propose_parameter_is_documented(name):
  """Regression guard for the interpolation timing, argument by argument."""
  props = _registered_tools()[name].parameters["properties"]
  undocumented = sorted(k for k, v in props.items() if not v.get("description"))
  assert not undocumented, f"{name} leaves {undocumented} undocumented"


def test_decorator_is_a_no_op_without_a_placeholder():
  """It must not disturb a docstring that needs no interpolation."""

  def fn():
    """No placeholder here."""

  assert mutations_gated.with_common_args_doc(fn).__doc__ == "No placeholder here."


# ---------------------------------------------------------------------------
# behaviour, end to end through a real tool on a real filesystem
# ---------------------------------------------------------------------------


def _keyword_info(status="ENABLED"):
  return {
      "text": "private investigator nyc",
      "match_type": "EXACT",
      "ad_group_name": "Ad group 1",
      "status": status,
  }


CRITERION = "customers/123/adGroupCriteria/456~789"


@mock.patch("ads_mcp.tools.mutations_gated._gaql_lookup_keyword")
@mock.patch("ads_mcp.tools.mutations_gated._get_client")
@mock.patch("ads_mcp.tools.mutations_gated.audit.resolve_client_root")
def test_pause_keyword_refuses_to_reissue_a_retired_code(
    mock_root, mock_client, mock_lookup, tmp_path
):
  """The 2026-09-01 incident, on a tool that could not answer it before."""
  mock_root.return_value = tmp_path
  mock_client.return_value = mock.Mock()
  mock_lookup.return_value = _keyword_info()

  first = mutations_gated.propose_pause_keyword(
      customer_id="123",
      criterion_resource_name=CRITERION,
      reason_code="client_request",
  )
  retired = approval.archive_proposal(
      tmp_path, first["code"], outcome="rejected", note="must NOT be retried"
  )

  with pytest.raises(ToolError) as exc:
    mutations_gated.propose_pause_keyword(
        customer_id="123",
        criterion_resource_name=CRITERION,
        reason_code="client_request",
    )

  assert "already used by a rejected" in str(exc.value)
  assert "supersedes=" in str(exc.value), "the message must name a usable remedy"
  assert retired.is_file()
  assert not list((tmp_path / approval.PENDING_DIRNAME).glob("*.md"))


@mock.patch("ads_mcp.tools.mutations_gated._gaql_lookup_keyword")
@mock.patch("ads_mcp.tools.mutations_gated._get_client")
@mock.patch("ads_mcp.tools.mutations_gated.audit.resolve_client_root")
def test_pause_keyword_supersedes_earns_a_new_code(
    mock_root, mock_client, mock_lookup, tmp_path
):
  """The remedy now works on this tool, and is recorded in the artifact."""
  mock_root.return_value = tmp_path
  mock_client.return_value = mock.Mock()
  mock_lookup.return_value = _keyword_info()

  first = mutations_gated.propose_pause_keyword(
      customer_id="123",
      criterion_resource_name=CRITERION,
      reason_code="client_request",
  )
  approval.archive_proposal(
      tmp_path, first["code"], outcome="rejected", note="retired"
  )

  replacement = mutations_gated.propose_pause_keyword(
      customer_id="123",
      criterion_resource_name=CRITERION,
      reason_code="client_request",
      supersedes=first["code"],
  )

  assert replacement["code"] != first["code"]
  assert f"Supersedes:** `{first['code']}`" in replacement["block"]
  assert "(revision 1)" in replacement["block"]
  spec = approval.read_proposal(tmp_path, replacement["code"])
  assert spec["supersedes"] == first["code"]
  assert spec["proposal_revision"] == 1


@mock.patch("ads_mcp.tools.mutations_gated._gaql_lookup_keyword")
@mock.patch("ads_mcp.tools.mutations_gated._get_client")
@mock.patch("ads_mcp.tools.mutations_gated.audit.resolve_client_root")
def test_pause_keyword_applied_supersession_demands_evidence(
    mock_root, mock_client, mock_lookup, tmp_path
):
  """Re-proposing something that may already be live is the dangerous case."""
  mock_root.return_value = tmp_path
  mock_client.return_value = mock.Mock()
  mock_lookup.return_value = _keyword_info()

  first = mutations_gated.propose_pause_keyword(
      customer_id="123",
      criterion_resource_name=CRITERION,
      reason_code="client_request",
  )
  approval.archive_proposal(
      tmp_path, first["code"], outcome="applied", note="done"
  )

  with pytest.raises(ToolError, match="requires supersedes_evidence"):
    mutations_gated.propose_pause_keyword(
        customer_id="123",
        criterion_resource_name=CRITERION,
        reason_code="client_request",
        supersedes=first["code"],
    )

  evidence = "GAQL 2026-09-15: criterion 456~789 still reads ENABLED."
  ok = mutations_gated.propose_pause_keyword(
      customer_id="123",
      criterion_resource_name=CRITERION,
      reason_code="client_request",
      supersedes=first["code"],
      supersedes_evidence=evidence,
  )
  assert evidence in ok["block"]
  assert ok["code"] != first["code"]
