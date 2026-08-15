# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for gated text updates to existing Responsive Search Ads."""

import copy
import types
from unittest import mock

from ads_mcp.tools import gated_ad_copy as gac
from fastmcp.exceptions import ToolError
import pytest


CID = "6922218131"
AD_ID = "820665316770"
AD_RN = f"customers/{CID}/ads/{AD_ID}"
AD_RN_2 = f"customers/{CID}/ads/820780023482"

BEFORE_H = [f"Headline {i}" for i in range(1, 11)]
BEFORE_D = ["Description one.", "Description two.", "Description three."]
AFTER_H = BEFORE_H + [f"New Headline {i}" for i in range(1, 6)]
AFTER_D = BEFORE_D + ["Description four."]


def _state(**over):
  s = {
      "ad_id": int(AD_ID),
      "resource_name": AD_RN,
      "ad_type": "RESPONSIVE_SEARCH_AD",
      "status": "ENABLED",
      "final_urls": ["https://thespineandhealthcenter.com/"],
      "path1": "",
      "path2": "",
      "headlines": [{"text": t, "pinned_field": "UNSPECIFIED"}
                    for t in BEFORE_H],
      "descriptions": [{"text": t, "pinned_field": "UNSPECIFIED"}
                       for t in BEFORE_D],
      "campaign": "24130111127",
      "campaign_name": "Brand",
      "ad_group": "202267797514",
      "ad_group_name": "Brand - Core",
  }
  s.update(over)
  s["fingerprint"] = gac.fingerprint(s)
  return s


def _spec(**over):
  before = _state()
  after_h = [{"text": t, "pinned_field": "UNSPECIFIED"} for t in AFTER_H]
  after_d = [{"text": t, "pinned_field": "UNSPECIFIED"} for t in AFTER_D]
  after_state = dict(before)
  after_state["headlines"] = after_h
  after_state["descriptions"] = after_d
  spec = {
      "op": gac.OP,
      "update_label": "Brand RSA expansion",
      "ads": [{
          "ad_id": AD_ID,
          "label": "Brand - Core / ad " + AD_ID,
          "before": before,
          "after": {"headlines": after_h, "descriptions": after_d},
          "before_fingerprint": before["fingerprint"],
          "after_fingerprint": gac.fingerprint(after_state),
      }],
      "allowed_update_mask": sorted(gac.ALLOWED_UPDATE_MASK),
      "login_customer_id": "7788207317",
  }
  spec.update(over)
  return spec


class _FakeSearch:
  def __init__(self, state):
    self.state = state

  def __call__(self, customer_id, query):
    s = self.state
    if s is None:
      return []
    pin = types.SimpleNamespace
    return [types.SimpleNamespace(
        ad_group_ad=types.SimpleNamespace(
            status=pin(name=s["status"]),
            ad=types.SimpleNamespace(
                id=s["ad_id"],
                resource_name=s["resource_name"],
                type_=pin(name=s["ad_type"]),
                final_urls=s["final_urls"],
                responsive_search_ad=types.SimpleNamespace(
                    path1=s["path1"],
                    path2=s["path2"],
                    headlines=[
                        pin(text=a["text"],
                            pinned_field=pin(name=a["pinned_field"]))
                        for a in s["headlines"]],
                    descriptions=[
                        pin(text=a["text"],
                            pinned_field=pin(name=a["pinned_field"]))
                        for a in s["descriptions"]],
                ),
            ),
        ),
        ad_group=pin(id=int(s["ad_group"]), name=s["ad_group_name"]),
        campaign=pin(id=int(s["campaign"]), name=s["campaign_name"]),
    )]


def _client(state):
  service = mock.Mock()
  service.search = _FakeSearch(state)
  client = mock.Mock()
  client.get_service.return_value = service
  return client, service


# -----------------------------------------------------------------------------
# Valid expansions
# -----------------------------------------------------------------------------


def test_valid_10_to_15_headline_expansion():
  out = gac.normalize_after(AFTER_H, "headline")
  assert len(out) == 15
  assert all(a["pinned_field"] == "UNSPECIFIED" for a in out)


def test_valid_3_to_4_description_expansion():
  out = gac.normalize_after(AFTER_D, "description")
  assert len(out) == 4


def test_description_rewrite_is_accepted():
  """Replacing an existing description in place is a normal edit."""
  rewritten = ["Description one.", "Find hours and directions before you visit.",
               "Description three.", "Description four."]
  out = gac.normalize_after(rewritten, "description")
  assert out[1]["text"] == "Find hours and directions before you visit."
  assert len(out) == 4


def test_pinning_is_carried_through_when_supplied():
  out = gac.normalize_after(
      [{"text": "A", "pinned_field": "HEADLINE_1"}, "B", "C"], "headline")
  assert out[0]["pinned_field"] == "HEADLINE_1"
  assert out[1]["pinned_field"] == "UNSPECIFIED"


# -----------------------------------------------------------------------------
# Character-limit and count rejection
# -----------------------------------------------------------------------------


def test_headline_over_30_characters_is_rejected():
  bad = "A" * 31
  with pytest.raises(ToolError, match="31 characters.*limit is 30"):
    gac.normalize_after(BEFORE_H[:2] + [bad], "headline")


def test_headline_of_exactly_30_characters_is_accepted():
  out = gac.normalize_after(["A" * 30, "B", "C"], "headline")
  assert len(out[0]["text"]) == 30


def test_description_over_90_characters_is_rejected():
  bad = "B" * 91
  with pytest.raises(ToolError, match="91 characters.*limit is 90"):
    gac.normalize_after(["ok", bad], "description")


def test_description_of_exactly_90_characters_is_accepted():
  out = gac.normalize_after(["C" * 90, "ok"], "description")
  assert len(out[0]["text"]) == 90


def test_too_many_headlines_rejected():
  with pytest.raises(ToolError, match="between 3 and 15"):
    gac.normalize_after([f"H{i}" for i in range(16)], "headline")


def test_too_few_descriptions_rejected():
  with pytest.raises(ToolError, match="between 2 and 4"):
    gac.normalize_after(["only one"], "description")


def test_duplicate_assets_within_one_ad_rejected():
  with pytest.raises(ToolError, match="Duplicate headline"):
    gac.normalize_after(["Same", "Same", "Other"], "headline")


def test_empty_asset_text_rejected():
  with pytest.raises(ToolError, match="Empty headline"):
    gac.normalize_after(["ok", "   ", "other"], "headline")


# -----------------------------------------------------------------------------
# Forbidden update masks / scope
# -----------------------------------------------------------------------------


def _op(mask=None, **ad_kwargs):
  from ads_mcp.tools._ads_api import resource_types, service_types
  from google.protobuf import field_mask_pb2
  ad = resource_types.Ad(resource_name=AD_RN, **ad_kwargs)
  op = service_types.AdOperation(update=ad)
  op.update_mask.CopyFrom(field_mask_pb2.FieldMask(
      paths=mask or sorted(gac.ALLOWED_UPDATE_MASK)))
  return op


def test_scope_check_passes_the_real_operation():
  op = gac.build_rsa_text_update_operation(
      AD_RN,
      [{"text": t, "pinned_field": "UNSPECIFIED"} for t in AFTER_H],
      [{"text": t, "pinned_field": "UNSPECIFIED"} for t in AFTER_D])
  gac.assert_operations_within_scope([op], {AD_RN})


@pytest.mark.parametrize("mask", [
    ["responsive_search_ad.headlines"],
    ["responsive_search_ad.headlines", "responsive_search_ad.descriptions",
     "final_urls"],
    ["responsive_search_ad.path1"],
    ["final_urls"],
    ["status"],
    ["responsive_search_ad.headlines", "responsive_search_ad.path2"],
])
def test_forbidden_update_masks_rejected(mask):
  with pytest.raises(ToolError, match="update mask"):
    gac.assert_operations_within_scope([_op(mask=mask)], {AD_RN})


def test_operation_carrying_final_urls_rejected():
  op = _op(final_urls=["https://example.com/"])
  with pytest.raises(ToolError, match="final_urls"):
    gac.assert_operations_within_scope([op], {AD_RN})


def test_operation_carrying_display_paths_rejected():
  from ads_mcp.tools._ads_api import resource_types, service_types
  from google.protobuf import field_mask_pb2
  ad = resource_types.Ad(resource_name=AD_RN)
  ad.responsive_search_ad.path1 = "Brand"
  op = service_types.AdOperation(update=ad)
  op.update_mask.CopyFrom(field_mask_pb2.FieldMask(
      paths=sorted(gac.ALLOWED_UPDATE_MASK)))
  with pytest.raises(ToolError, match="path1/path2"):
    gac.assert_operations_within_scope([op], {AD_RN})


def test_target_ad_mismatch_rejected():
  op = gac.build_rsa_text_update_operation(
      AD_RN_2,
      [{"text": t, "pinned_field": "UNSPECIFIED"} for t in AFTER_H],
      [{"text": t, "pinned_field": "UNSPECIFIED"} for t in AFTER_D])
  with pytest.raises(ToolError, match="not in the approved ad allowlist"):
    gac.assert_operations_within_scope([op], {AD_RN})


def test_ad_operation_has_no_create_or_remove_member():
  """Structural: the API type itself cannot express ad replacement."""
  from ads_mcp.tools._ads_api import service_types
  fields = {f.name for f in service_types.AdOperation()._pb.DESCRIPTOR.fields}
  assert "create" not in fields
  assert "remove" not in fields
  assert "update" in fields


# -----------------------------------------------------------------------------
# Drift guards
# -----------------------------------------------------------------------------


def test_no_drift_passes_on_the_approved_state():
  client, _ = _client(_state())
  gac.assert_no_drift(client, CID, _spec())


def test_refuses_on_status_drift():
  client, _ = _client(_state(status="PAUSED"))
  with pytest.raises(ToolError, match="status is PAUSED"):
    gac.assert_no_drift(client, CID, _spec())


def test_refuses_on_final_url_drift():
  client, _ = _client(_state(final_urls=["https://example.com/other/"]))
  with pytest.raises(ToolError, match="final URL changed"):
    gac.assert_no_drift(client, CID, _spec())


def test_refuses_on_pinning_drift():
  s = _state()
  s["headlines"][0]["pinned_field"] = "HEADLINE_1"
  s["fingerprint"] = gac.fingerprint(s)
  client, _ = _client(s)
  with pytest.raises(ToolError, match="pinning changed"):
    gac.assert_no_drift(client, CID, _spec())


def test_refuses_on_existing_headline_edited():
  s = _state()
  s["headlines"][2]["text"] = "Someone Edited This"
  s["fingerprint"] = gac.fingerprint(s)
  client, _ = _client(s)
  with pytest.raises(ToolError, match="existing headlines changed"):
    gac.assert_no_drift(client, CID, _spec())


def test_refuses_on_existing_description_edited():
  s = _state()
  s["descriptions"][1]["text"] = "Edited elsewhere."
  s["fingerprint"] = gac.fingerprint(s)
  client, _ = _client(s)
  with pytest.raises(ToolError, match="existing descriptions changed"):
    gac.assert_no_drift(client, CID, _spec())


def test_refuses_on_before_asset_count_drift():
  s = _state()
  s["headlines"].append({"text": "Added By Someone",
                         "pinned_field": "UNSPECIFIED"})
  s["fingerprint"] = gac.fingerprint(s)
  client, _ = _client(s)
  with pytest.raises(ToolError, match="headline count is 11"):
    gac.assert_no_drift(client, CID, _spec())


def test_refuses_on_ownership_drift():
  client, _ = _client(_state(ad_group="999999", campaign="888888"))
  with pytest.raises(ToolError, match="ownership changed"):
    gac.assert_no_drift(client, CID, _spec())


def test_refuses_on_display_path_drift():
  client, _ = _client(_state(path1="Brand"))
  with pytest.raises(ToolError, match="display paths changed"):
    gac.assert_no_drift(client, CID, _spec())


def test_refuses_when_ad_disappeared():
  client, _ = _client(None)
  with pytest.raises(ToolError, match="No ad found"):
    gac.assert_no_drift(client, CID, _spec())


def test_refuses_when_ad_is_not_an_rsa():
  client, _ = _client(_state(ad_type="EXPANDED_TEXT_AD"))
  with pytest.raises(ToolError, match="not a RESPONSIVE_SEARCH_AD"):
    gac.assert_no_drift(client, CID, _spec())


def test_already_applied_state_is_idempotent_not_drift():
  after = _state()
  after["headlines"] = [{"text": t, "pinned_field": "UNSPECIFIED"}
                        for t in AFTER_H]
  after["descriptions"] = [{"text": t, "pinned_field": "UNSPECIFIED"}
                           for t in AFTER_D]
  after["fingerprint"] = gac.fingerprint(after)
  client, _ = _client(after)
  gac.assert_no_drift(client, CID, _spec())


def test_spec_without_required_guard_fields_refuses():
  """A spec lacking the BEFORE fingerprint cannot be validated."""
  client, _ = _client(_state())
  spec = _spec()
  del spec["ads"][0]["before_fingerprint"]
  with pytest.raises(KeyError):
    gac.assert_no_drift(client, CID, spec)


def test_spec_from_an_older_tool_without_after_fingerprint_refuses():
  client, _ = _client(_state())
  spec = _spec()
  del spec["ads"][0]["after_fingerprint"]
  with pytest.raises(KeyError):
    gac.assert_no_drift(client, CID, spec)


# -----------------------------------------------------------------------------
# Fingerprint behavior
# -----------------------------------------------------------------------------


def test_fingerprint_is_stable_for_identical_state():
  assert gac.fingerprint(_state()) == gac.fingerprint(_state())


@pytest.mark.parametrize("field,value", [
    ("status", "PAUSED"),
    ("path1", "Brand"),
    ("final_urls", ["https://other.example/"]),
    ("ad_group", "111111"),
    ("campaign", "222222"),
])
def test_fingerprint_moves_when_any_guarded_field_moves(field, value):
  base = _state()
  moved = _state(**{field: value})
  assert base["fingerprint"] != moved["fingerprint"]


def test_fingerprint_moves_when_an_asset_is_repinned():
  base = _state()
  moved = copy.deepcopy(base)
  moved["headlines"][0]["pinned_field"] = "HEADLINE_2"
  assert gac.fingerprint(moved) != base["fingerprint"]


# -----------------------------------------------------------------------------
# Executor / batch configuration
# -----------------------------------------------------------------------------


def test_executor_sends_one_batch_with_partial_failure_disabled():
  client, service = _client(_state())
  service.mutate_ads = mock.Mock(
      return_value=types.SimpleNamespace(results=[object()]))
  with mock.patch.object(gac, "read_ad_state") as reader:
    before = _state()
    after = _state()
    after["headlines"] = [{"text": t, "pinned_field": "UNSPECIFIED"}
                          for t in AFTER_H]
    after["descriptions"] = [{"text": t, "pinned_field": "UNSPECIFIED"}
                             for t in AFTER_D]
    after["fingerprint"] = gac.fingerprint(after)
    # drift check -> before; pending scan -> before; verify -> after
    reader.side_effect = [before, before, after]
    result = gac._execute_update_responsive_search_ads(client, CID, _spec())

  assert service.mutate_ads.call_count == 2, "validate_only then real"
  dry = service.mutate_ads.call_args_list[0].kwargs["request"]
  real = service.mutate_ads.call_args_list[1].kwargs["request"]
  assert dry.validate_only is True
  assert real.validate_only is False
  assert dry.partial_failure is False
  assert real.partial_failure is False
  assert len(real.operations) == 1
  assert sorted(real.operations[0].update_mask.paths) == sorted(
      gac.ALLOWED_UPDATE_MASK)
  assert result["outcome"] == "applied"


def test_executor_refuses_before_mutating_on_drift():
  client, service = _client(_state(status="PAUSED"))
  service.mutate_ads = mock.Mock()
  with pytest.raises(ToolError, match="RSA COPY UPDATE REFUSED"):
    gac._execute_update_responsive_search_ads(client, CID, _spec())
  service.mutate_ads.assert_not_called()


def test_executor_no_ops_when_already_applied():
  after = _state()
  after["headlines"] = [{"text": t, "pinned_field": "UNSPECIFIED"}
                        for t in AFTER_H]
  after["descriptions"] = [{"text": t, "pinned_field": "UNSPECIFIED"}
                           for t in AFTER_D]
  after["fingerprint"] = gac.fingerprint(after)
  client, service = _client(after)
  service.mutate_ads = mock.Mock()
  result = gac._execute_update_responsive_search_ads(client, CID, _spec())
  assert result["outcome"] == "no_op"
  service.mutate_ads.assert_not_called()


def test_executor_raises_when_read_back_disagrees():
  client, service = _client(_state())
  service.mutate_ads = mock.Mock(
      return_value=types.SimpleNamespace(results=[object()]))
  with pytest.raises(ToolError, match="VERIFICATION FAILED"):
    gac._execute_update_responsive_search_ads(client, CID, _spec())


def test_executor_is_registered_in_the_shared_gate():
  from ads_mcp.tools import mutations_gated
  assert gac.OP in mutations_gated._DISPATCH


def test_allowed_mask_is_exactly_two_leaf_paths():
  assert gac.ALLOWED_UPDATE_MASK == frozenset({
      "responsive_search_ad.headlines",
      "responsive_search_ad.descriptions",
  })
