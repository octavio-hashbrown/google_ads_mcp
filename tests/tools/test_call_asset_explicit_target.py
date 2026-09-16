# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""The approved call asset must be an asset, not a phone number.

A phone number is not an identity. An account may legitimately hold
several CALL assets carrying the same digits, and the attach path used to
resolve one by number at BOTH ends independently: propose returned the
first match, and apply re-resolved from scratch. Two consequences, both
live defects:

  1. The hash-verified spec recorded a NUMBER, so a human approving the
     proposal could not tell which asset they had approved.
  2. Apply could legitimately act on a DIFFERENT asset than the one that
     was reviewed, because nothing carried the reviewed identity forward.

Everything here is synthetic. No Google Ads client is constructed and no
mutation is issued -- the account read is replaced, and the two
reuse-safety checks are stubbed permissive so the target-selection
behaviour under test is what actually fails a test.
"""

from unittest import mock

import pytest
from fastmcp.exceptions import ToolError

from ads_mcp.tools import gated_assets

CUSTOMER = "7849914897"
OTHER_CUSTOMER = "1234567890"

# Two assets carrying the SAME number, stored in different formats, plus
# an unrelated one. This is the shape of the live account that exposed
# the defect.
INTENDED = "customers/7849914897/assets/328461156792"
DUPLICATE = "customers/7849914897/assets/330416214481"
OTHER_NUMBER = "customers/7849914897/assets/118041005838"


def _asset(resource_name, phone, country="US"):
  return {
      "resource_name": resource_name,
      "name": "",
      "phone_number": phone,
      "country_code": country,
      "call_conversion_reporting_state": (
          "USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION"
      ),
      "call_conversion_action": "customers/7849914897/conversionActions/179",
      "ad_schedule_targets": [],
  }


DUPLICATE_NUMBER_ACCOUNT = [
    _asset(INTENDED, "9144404316"),
    _asset(DUPLICATE, "(914) 440-4316"),
    _asset(OTHER_NUMBER, "(917) 833-6189"),
]
SINGLE_MATCH_ACCOUNT = [
    _asset(INTENDED, "9144404316"),
    _asset(OTHER_NUMBER, "(917) 833-6189"),
]


@pytest.fixture(name="account")
def _account():
  """Replaces the account read and the two reuse-safety checks."""
  with mock.patch.object(gated_assets, "_read_call_assets") as read, \
       mock.patch.object(gated_assets, "_assess_asset_for_reuse",
                         return_value=([], [])), \
       mock.patch.object(gated_assets, "verify_account_call_reporting",
                         return_value=({"call_conversion_action": "ca/1"}, [])):
    read.return_value = list(DUPLICATE_NUMBER_ACCOUNT)
    yield read


def _prepare(account_assets=None, **kwargs):
  """Runs the campaign-shaped proposal path and returns its spec."""
  if account_assets is not None:
    kwargs.pop("_", None)
  args = dict(
      level="campaign",
      parent_resource_name="customers/7849914897/campaigns/22581514720",
      parent_label="Search Campaign May 2025",
      links=[],
      phone_number="9144404316",
      country_code="US",
      intended_state=None,
      expected_account_action=None,
      reuse_incompatible_asset=False,
      reuse_existing_asset_only=True,
      asset_resource_name=None,
  )
  args.update(kwargs)
  _, spec = gated_assets._prepare_attachment(mock.Mock(), CUSTOMER, **args)
  return spec


# ---------------------------------------------------------------------------
# duplicate numbers: the proposal must refuse to guess
# ---------------------------------------------------------------------------


def test_duplicate_numbers_refuse_without_an_explicit_target(account):
  """First-match selection is what made the approval ambiguous."""
  with pytest.raises(ToolError) as exc:
    _prepare()
  message = str(exc.value)
  assert "2 CALL assets" in message
  assert INTENDED in message and DUPLICATE in message, "must name the candidates"
  assert "asset_resource_name" in message, "must say how to proceed"


def test_duplicate_numbers_are_resolved_by_an_explicit_target(account):
  """Naming the asset is the sanctioned way through."""
  spec = _prepare(asset_resource_name=INTENDED)
  assert spec["target_asset_resource_name"] == INTENDED


def test_a_bare_asset_id_is_accepted(account):
  """Operators read IDs off the UI, not resource names."""
  spec = _prepare(asset_resource_name="328461156792")
  assert spec["target_asset_resource_name"] == INTENDED


def test_single_match_still_needs_no_explicit_target(account):
  """The unambiguous case must not become harder to use."""
  account.return_value = list(SINGLE_MATCH_ACCOUNT)
  spec = _prepare()
  assert spec["target_asset_resource_name"] == INTENDED


# ---------------------------------------------------------------------------
# validating the named target
# ---------------------------------------------------------------------------


def test_target_belonging_to_another_customer_is_refused(account):
  """Silently repointing it at this account is the worst reading."""
  foreign = f"customers/{OTHER_CUSTOMER}/assets/328461156792"
  with pytest.raises(ToolError, match="belongs to customer"):
    _prepare(asset_resource_name=foreign)


def test_target_that_is_not_a_call_asset_is_refused(account):
  """The read is CALL-typed, so a non-CALL id simply is not there."""
  with pytest.raises(ToolError) as exc:
    _prepare(asset_resource_name="customers/7849914897/assets/999999999")
  assert "No CALL asset" in str(exc.value)
  assert "Refusing to fall back" in str(exc.value)


def test_missing_target_is_refused_rather_than_matched_by_number(account):
  """The whole point: absence must never degrade into a number match."""
  with pytest.raises(ToolError, match="Refusing to fall back"):
    _prepare(asset_resource_name="12345")


def test_malformed_target_is_refused(account):
  with pytest.raises(ToolError, match="neither a numeric asset ID"):
    _prepare(asset_resource_name="not-an-asset")


def test_target_not_carrying_the_advertised_number_is_refused(account):
  """The block and the hashed target must describe the same thing."""
  with pytest.raises(ToolError, match="does not carry the number"):
    _prepare(asset_resource_name=OTHER_NUMBER)


def test_target_with_mismatched_country_is_refused(account):
  account.return_value = [_asset(INTENDED, "9144404316", country="CA")]
  with pytest.raises(ToolError, match="country_code"):
    _prepare(asset_resource_name=INTENDED)


# ---------------------------------------------------------------------------
# propose -> apply: the approved target is what executes
# ---------------------------------------------------------------------------


def test_apply_uses_the_approved_target_not_the_number(account):
  """The load-bearing test.

  The duplicate is deliberately ordered FIRST in the account read, so a
  number-matching apply would return it. Only carrying the approved
  identity forward yields the intended asset.
  """
  spec = _prepare(asset_resource_name=INTENDED)
  account.return_value = [
      _asset(DUPLICATE, "9144404316"),
      _asset(INTENDED, "(914) 440-4316"),
  ]
  resolved, created = gated_assets._resolve_asset_for_apply(
      mock.Mock(), CUSTOMER, spec
  )
  assert resolved == INTENDED, "apply re-picked an asset by phone number"
  assert created is False


def test_apply_refuses_when_the_approved_target_is_gone(account):
  """No substitution, even though the number is still in the account."""
  spec = _prepare(asset_resource_name=INTENDED)
  account.return_value = [_asset(DUPLICATE, "9144404316")]
  with pytest.raises(ToolError) as exc:
    gated_assets._resolve_asset_for_apply(mock.Mock(), CUSTOMER, spec)
  assert "no longer exists" in str(exc.value)
  assert "NOT substitute" in str(exc.value)


def test_apply_refuses_when_the_approved_target_was_edited(account):
  """Drift in the target itself, not just in the account around it."""
  spec = _prepare(asset_resource_name=INTENDED)
  account.return_value = [_asset(INTENDED, "2015550000")]
  with pytest.raises(ToolError, match="no longer carries the number"):
    gated_assets._resolve_asset_for_apply(mock.Mock(), CUSTOMER, spec)


def test_apply_refuses_a_reuse_only_spec_with_no_approved_target(account):
  """Fail closed on an artifact that predates the approved-target field."""
  spec = _prepare(asset_resource_name=INTENDED)
  spec.pop("target_asset_resource_name")
  with pytest.raises(ToolError, match="records no approved target asset"):
    gated_assets._resolve_asset_for_apply(mock.Mock(), CUSTOMER, spec)


def test_approved_target_is_inside_the_hashed_spec(account):
  """Approval must cover the asset, so it has to be in what is hashed."""
  from ads_mcp.governance import approval

  spec = _prepare(asset_resource_name=INTENDED)
  base = {"tool": "attach_call_asset_to_campaign",
          "customer_id": CUSTOMER, "spec": dict(spec)}
  other = {"tool": "attach_call_asset_to_campaign", "customer_id": CUSTOMER,
           "spec": dict(spec, target_asset_resource_name=DUPLICATE)}
  assert approval._generate_code(base) != approval._generate_code(other), (
      "changing the approved target must change the approval code"
  )


def test_no_google_ads_mutation_is_reachable_from_these_paths(account):
  """These tests construct no client; every ads_client here is a Mock."""
  spec = _prepare(asset_resource_name=INTENDED)
  client = mock.Mock()
  gated_assets._resolve_asset_for_apply(client, CUSTOMER, spec)
  assert not client.get_service.called, "resolution must not call a service"
