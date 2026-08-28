"""Runtime provenance and the fail-closed pinned-runtime gate.

The gate compares THREE independent identities -- the configured pin, the
deployment stamp, and the actual git HEAD -- because an earlier revision
trusted the stamp and accepted two states it should have refused:

  * a plain developer checkout whose HEAD happened to equal the pin, with
    no deployment at all
  * a stamped deployment someone had since checked out to a different
    clean commit, which reported the stamped revision while executing a
    different one

Both are covered below, alongside every "cannot determine" path, which
must refuse rather than assume.
"""

import pathlib
from unittest import mock

import pytest

from ads_mcp.governance import provenance
from ads_mcp.tools import loader

A = "32562d39b3d7eb9e27d913c5e87298d171739804"
B = "9c7c83e7d3f2acb61bac5e5bf877932a25903428"


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch):
  """Never inherit a real pin from the developer's environment."""
  monkeypatch.delenv(provenance.PINNED_REVISION_VAR, raising=False)


def _state(
    monkeypatch,
    *,
    stamp=None,
    head=None,
    tracked_dirty=False,
    untracked=(),
    detached=True,
):
  """Poses the runtime as a given deployment shape."""
  monkeypatch.setattr(provenance, "read_stamp", lambda: stamp)
  monkeypatch.setattr(provenance, "_git_revision", lambda root: head)
  monkeypatch.setattr(provenance, "_git_detached", lambda root: detached)
  monkeypatch.setattr(
      provenance,
      "_git_status",
      lambda root: (
          tracked_dirty,
          None if untracked is None else list(untracked),
      ),
  )


def _deployed(monkeypatch, revision=A, **kwargs):
  """A correctly cut, untouched deployment."""
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, revision)
  _state(monkeypatch, stamp=revision, head=revision, **kwargs)


def _refused(match=None):
  return pytest.raises(provenance.UnpinnedRuntimeError, match=match)


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (A, True),
        (A.upper(), False),
        (A[:12], False),
        ("origin/lo-media-mutations", False),
        ("", False),
        (None, False),
        (12345, False),
    ],
)
def test_only_a_full_lowercase_sha_is_a_usable_identity(value, expected):
  """Two identities can only be compared if both are canonical."""
  assert provenance.is_commit_sha(value) is expected


# ---------------------------------------------------------------------------
# What the three identities report
# ---------------------------------------------------------------------------


def test_all_three_identities_are_reported_independently(monkeypatch):
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, A)
  _state(monkeypatch, stamp=A, head=B)
  state = provenance.runtime_provenance()
  assert state["pinned_revision"] == A
  assert state["stamp_revision"] == A
  assert state["git_head_revision"] == B
  assert state["matches_pin"] is False, "they disagree, so this is not a match"
  assert state["immutable"] is False


def test_working_tree_is_reported_as_mutable(monkeypatch):
  _state(monkeypatch, stamp=None, head=A)
  state = provenance.runtime_provenance()
  assert state["revision_source"] == "git_working_tree"
  assert state["immutable"] is False, "a developer checkout is never immutable"


def test_unprovable_runtime_reports_none_rather_than_guessing(monkeypatch):
  _state(monkeypatch, stamp=None, head=None, tracked_dirty=None, untracked=None)
  state = provenance.runtime_provenance()
  assert state["revision"] is None
  assert state["revision_source"] == "unknown"
  assert state["immutable"] is False


def test_verified_deployment_reports_immutable(monkeypatch):
  _deployed(monkeypatch)
  state = provenance.runtime_provenance()
  assert state["revision_source"] == "deployment_stamp"
  assert state["matches_pin"] is True
  assert state["immutable"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tracked_dirty": True},
        {"tracked_dirty": None},
        {"untracked": ["ads_mcp/tools/shadow.py"]},
        {"untracked": None},
        {"detached": False},
        {"detached": None},
    ],
)
def test_immutable_is_false_whenever_the_form_is_not_provably_intact(
    monkeypatch, kwargs
):
  """Every unknown counts against immutability, never for it."""
  _deployed(monkeypatch, **kwargs)
  assert provenance.runtime_provenance()["immutable"] is False


def test_stamp_reads_from_disk(tmp_path, monkeypatch):
  monkeypatch.setattr(provenance, "runtime_root", lambda: tmp_path)
  assert provenance.read_stamp() is None
  (tmp_path / provenance.STAMP_FILENAME).write_text(f"{A}\n", encoding="utf-8")
  assert provenance.read_stamp() == A


# ---------------------------------------------------------------------------
# git parsing
# ---------------------------------------------------------------------------


def test_status_separates_tracked_edits_from_untracked_additions():
  porcelain = " M ads_mcp/tools/loader.py\n?? ads_mcp/tools/shadow.py\n"
  with mock.patch.object(provenance, "_git", return_value=porcelain):
    dirty, untracked = provenance._git_status(pathlib.Path("."))
  assert dirty is True
  assert untracked == ["ads_mcp/tools/shadow.py"]


def test_the_deployment_stamp_is_the_only_permitted_untracked_file():
  with mock.patch.object(
      provenance, "_git", return_value=f"?? {provenance.STAMP_FILENAME}\n"
  ):
    dirty, untracked = provenance._git_status(pathlib.Path("."))
  assert dirty is False
  assert untracked == [], "the stamp is written by the deploy script"


def test_status_failure_is_unknown_not_clean():
  with mock.patch.object(provenance, "_git", return_value=None):
    assert provenance._git_status(pathlib.Path(".")) == (None, None)


def test_detached_detection_distinguishes_failure_from_a_branch():
  with mock.patch.object(provenance, "_git", return_value="HEAD"):
    assert provenance._git_detached(pathlib.Path(".")) is True
  with mock.patch.object(provenance, "_git", return_value="lo-media-mutations"):
    assert provenance._git_detached(pathlib.Path(".")) is False
  with mock.patch.object(provenance, "_git", return_value=None):
    assert provenance._git_detached(pathlib.Path(".")) is None


# ---------------------------------------------------------------------------
# The fail-closed gate -- the three reported defects
# ---------------------------------------------------------------------------


def test_defect_1_clean_developer_checkout_with_a_matching_pin_is_refused(
    monkeypatch
):
  """No stamp means no deployment, however well HEAD happens to line up.

  A checkout can be moved by an ordinary git command, so a matching HEAD
  proves nothing about what will be running a minute from now.
  """
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, A)
  _state(monkeypatch, stamp=None, head=A, tracked_dirty=False)
  with _refused("no deployment stamp"):
    provenance.verify_pinned_runtime(require_pin=True)


def test_defect_2_stamp_and_pin_agree_but_head_is_a_different_commit(
    monkeypatch
):
  """The worst case: it would report A while executing B."""
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, A)
  _state(monkeypatch, stamp=A, head=B, tracked_dirty=False)
  with _refused("stamped") as excinfo:
    provenance.verify_pinned_runtime(require_pin=True)
  message = str(excinfo.value)
  assert A in message and B in message
  assert "report one revision while executing another" in message


def test_defect_3_unknown_cleanliness_is_refused(monkeypatch):
  """A control that cannot see is not a control."""
  _deployed(monkeypatch, tracked_dirty=None)
  with _refused("could not be determined"):
    provenance.verify_pinned_runtime(require_pin=True)


# ---------------------------------------------------------------------------
# The fail-closed gate -- remaining refusals
# ---------------------------------------------------------------------------


def test_governed_tier_refuses_an_unpinned_runtime(monkeypatch):
  _state(monkeypatch, stamp=A, head=A)
  with _refused(provenance.PINNED_REVISION_VAR):
    provenance.verify_pinned_runtime(require_pin=True)


@pytest.mark.parametrize("bad_pin", [A[:12], A.upper(), "lo-media-mutations"])
def test_governed_tier_refuses_a_malformed_pin(monkeypatch, bad_pin):
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, bad_pin)
  _state(monkeypatch, stamp=A, head=A)
  with _refused("not a 40-character commit sha"):
    provenance.verify_pinned_runtime(require_pin=True)


@pytest.mark.parametrize("bad_stamp", [A[:12], A.upper(), "deployed-by-hand"])
def test_governed_tier_refuses_a_malformed_stamp(monkeypatch, bad_stamp):
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, A)
  _state(monkeypatch, stamp=bad_stamp, head=A)
  with _refused("not a 40-character commit"):
    provenance.verify_pinned_runtime(require_pin=True)


def test_governed_tier_refuses_when_head_cannot_be_read(monkeypatch):
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, A)
  _state(monkeypatch, stamp=A, head=None)
  with _refused("could not be read"):
    provenance.verify_pinned_runtime(require_pin=True)


def test_governed_tier_refuses_a_revision_that_does_not_match_the_pin(
    monkeypatch
):
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, A)
  _state(monkeypatch, stamp=B, head=B)
  with _refused("does not match the pinned"):
    provenance.verify_pinned_runtime(require_pin=True)


def test_governed_tier_refuses_an_edited_deployment(monkeypatch):
  _deployed(monkeypatch, tracked_dirty=True)
  with _refused("never be edited in place"):
    provenance.verify_pinned_runtime(require_pin=True)


def test_governed_tier_refuses_runtime_affecting_untracked_files(monkeypatch):
  """An untracked .py inside the package can shadow what is imported."""
  _deployed(monkeypatch, untracked=["ads_mcp/tools/shadow.py"])
  with _refused("unexpected untracked files") as excinfo:
    provenance.verify_pinned_runtime(require_pin=True)
  assert "ads_mcp/tools/shadow.py" in str(excinfo.value)


def test_governed_tier_refuses_when_untracked_cannot_be_enumerated(
    monkeypatch
):
  _deployed(monkeypatch, untracked=None)
  with _refused("could not be enumerated"):
    provenance.verify_pinned_runtime(require_pin=True)


def test_governed_tier_refuses_a_deployment_on_a_branch(monkeypatch):
  """Detached HEAD is part of the contract: a branch can be moved."""
  _deployed(monkeypatch, detached=False)
  with _refused("detached HEAD"):
    provenance.verify_pinned_runtime(require_pin=True)


def test_governed_tier_refuses_when_detachment_cannot_be_determined(
    monkeypatch
):
  _deployed(monkeypatch, detached=None)
  with _refused("could not be determined"):
    provenance.verify_pinned_runtime(require_pin=True)


# ---------------------------------------------------------------------------
# What must still be allowed
# ---------------------------------------------------------------------------


def test_read_only_runtime_may_stay_unpinned(monkeypatch):
  """Local read-only development must not be bricked by this control."""
  _state(monkeypatch, stamp=None, head=A)
  assert provenance.verify_pinned_runtime(require_pin=False)["revision"] == A


def test_governed_tier_accepts_a_verified_immutable_deployment(monkeypatch):
  _deployed(monkeypatch)
  state = provenance.verify_pinned_runtime(require_pin=True)
  assert state["matches_pin"] is True
  assert state["immutable"] is True
  assert state["revision_source"] == "deployment_stamp"


def test_the_permitted_stamp_does_not_block_a_good_deployment(monkeypatch):
  """The deploy script writes the stamp after checkout; that is expected."""
  _deployed(monkeypatch, untracked=[])
  provenance.verify_pinned_runtime(require_pin=True)


def test_two_deployments_cannot_both_satisfy_one_pin(monkeypatch):
  """The concurrency failure this control exists to prevent."""
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, A)
  _state(monkeypatch, stamp=A, head=A)
  provenance.verify_pinned_runtime(require_pin=True)  # the intended one
  _state(monkeypatch, stamp=B, head=B)
  with _refused():
    provenance.verify_pinned_runtime(require_pin=True)


def test_describe_provenance_is_stderr_safe_and_states_the_posture(
    monkeypatch
):
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, A)
  _state(monkeypatch, stamp=A, head=A, tracked_dirty=True)
  line = provenance.describe_provenance(provenance.runtime_provenance())
  assert A[:12] in line
  assert "MUTABLE" in line and "DIRTY" in line
  assert "\n" not in line


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_runtime_tool_is_read_tier(monkeypatch):
  """Provable revision must survive every mutation tier being off."""
  monkeypatch.setenv("ADS_MCP_ENABLE_MUTATIONS", "false")
  monkeypatch.setenv("ADS_MCP_ENABLE_RAW_MUTATIONS", "false")
  loaded = loader.load_tools(log=False)
  assert "runtime" in loaded["read"]
  assert loaded["governed"] == []
  assert loaded["raw"] == []


def test_provenance_tool_reports_all_three_identities(monkeypatch):
  from ads_mcp.tools import runtime as runtime_tool

  monkeypatch.setenv("ADS_MCP_ENABLE_MUTATIONS", "true")
  monkeypatch.setenv("ADS_MCP_ENABLE_RAW_MUTATIONS", "false")
  _deployed(monkeypatch)

  state = runtime_tool.get_runtime_provenance()
  assert state["pinned_revision"] == A
  assert state["stamp_revision"] == A
  assert state["git_head_revision"] == A
  assert state["matches_pin"] is True
  assert state["governed_tier_enabled"] is True
  assert state["raw_tier_enabled"] is False, "RAW must stay fail-closed"


# ---------------------------------------------------------------------------
# The serve-time boundary
#
# The check sits in main(), not at module import: importing the entrypoint
# to inspect the registry is legitimate, serving from a runtime that cannot
# prove its revision is not.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entrypoint", ["stdio", "server"])
def test_entrypoint_refuses_to_serve_a_governed_unpinned_runtime(
    entrypoint, monkeypatch
):
  module = __import__(f"ads_mcp.{entrypoint}", fromlist=["main"])
  _state(monkeypatch, stamp=None, head=A)
  monkeypatch.setattr(module.flags, "mutations_enabled", lambda: True)
  run = mock.Mock()
  monkeypatch.setattr(module.mcp_server, "run", run)

  with _refused():
    module.main()

  run.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["stdio", "server"])
def test_entrypoint_serves_when_the_runtime_is_verified(
    entrypoint, monkeypatch
):
  """Control: a properly pinned deployment still starts normally."""
  module = __import__(f"ads_mcp.{entrypoint}", fromlist=["main"])
  _deployed(monkeypatch)
  monkeypatch.setattr(module.flags, "mutations_enabled", lambda: True)
  monkeypatch.setattr(module.mcp_server, "run", mock.Mock())
  monkeypatch.setattr(module, "get_ads_client", mock.Mock())
  monkeypatch.setattr(module, "update_views_yaml", mock.Mock(return_value=None))
  monkeypatch.setattr(module.asyncio, "run", mock.Mock())

  module.main()
  module.mcp_server.run.assert_called_once()
