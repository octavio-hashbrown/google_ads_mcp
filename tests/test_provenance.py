"""Runtime provenance and the fail-closed pinned-runtime gate."""

import pathlib
from unittest import mock

import pytest

from ads_mcp.governance import provenance
from ads_mcp.tools import loader

SHA = "9c7c83e7d3f2acb61bac5e5bf877932a25903428"
OTHER_SHA = "05c36676eab07fb2c539fc1c15ad7e29b7ee38e6"


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch):
  """Never inherit a real pin from the developer's environment."""
  monkeypatch.delenv(provenance.PINNED_REVISION_VAR, raising=False)


def _state(monkeypatch, *, stamp=None, git_revision=None, dirty=False):
  monkeypatch.setattr(provenance, "read_stamp", lambda: stamp)
  monkeypatch.setattr(provenance, "_git_revision", lambda root: git_revision)
  monkeypatch.setattr(provenance, "_git_tree_dirty", lambda root: dirty)


# ---------------------------------------------------------------------------
# Where the revision comes from
# ---------------------------------------------------------------------------


def test_deployment_stamp_is_authoritative(monkeypatch):
  """A stamped deployment does not depend on git being present."""
  _state(monkeypatch, stamp=SHA, git_revision=OTHER_SHA)
  state = provenance.runtime_provenance()
  assert state["revision"] == SHA
  assert state["revision_source"] == "deployment_stamp"
  assert state["immutable"] is True


def test_working_tree_is_reported_as_mutable(monkeypatch):
  """An unstamped checkout is honestly labelled, not dressed up."""
  _state(monkeypatch, stamp=None, git_revision=SHA)
  state = provenance.runtime_provenance()
  assert state["revision"] == SHA
  assert state["revision_source"] == "git_working_tree"
  assert state["immutable"] is False, "a developer checkout is never immutable"


def test_unprovable_runtime_reports_none_rather_than_guessing(monkeypatch):
  _state(monkeypatch, stamp=None, git_revision=None, dirty=None)
  state = provenance.runtime_provenance()
  assert state["revision"] is None
  assert state["revision_source"] == "unknown"
  assert state["immutable"] is False


def test_edited_deployment_is_not_immutable(monkeypatch):
  """Stamped but edited underneath: the imported code is not the reviewed code."""
  _state(monkeypatch, stamp=SHA, git_revision=SHA, dirty=True)
  assert provenance.runtime_provenance()["immutable"] is False


def test_stamp_reads_from_disk(tmp_path, monkeypatch):
  monkeypatch.setattr(provenance, "runtime_root", lambda: tmp_path)
  assert provenance.read_stamp() is None
  (tmp_path / provenance.STAMP_FILENAME).write_text(f"{SHA}\n", encoding="utf-8")
  assert provenance.read_stamp() == SHA


def test_untracked_files_do_not_count_as_dirty():
  """The stamp itself is untracked; it must not mark the tree dirty."""
  with mock.patch.object(provenance, "_git", return_value="") as git:
    assert provenance._git_tree_dirty(pathlib.Path(".")) is False
  assert "--untracked-files=no" in git.call_args.args


# ---------------------------------------------------------------------------
# The fail-closed gate
# ---------------------------------------------------------------------------


def test_read_only_runtime_may_stay_unpinned(monkeypatch):
  """Local read-only development must not be bricked by this control."""
  _state(monkeypatch, stamp=None, git_revision=SHA)
  state = provenance.verify_pinned_runtime(require_pin=False)
  assert state["revision"] == SHA


def test_governed_tier_refuses_an_unpinned_runtime(monkeypatch):
  _state(monkeypatch, stamp=None, git_revision=SHA)
  with pytest.raises(provenance.UnpinnedRuntimeError) as excinfo:
    provenance.verify_pinned_runtime(require_pin=True)
  assert "unpinned runtime" in str(excinfo.value)
  assert provenance.PINNED_REVISION_VAR in str(excinfo.value)


def test_governed_tier_refuses_when_revision_is_unprovable(
    monkeypatch
):
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, SHA)
  _state(monkeypatch, stamp=None, git_revision=None, dirty=None)
  with pytest.raises(provenance.UnpinnedRuntimeError) as excinfo:
    provenance.verify_pinned_runtime(require_pin=True)
  assert "cannot prove" in str(excinfo.value)


def test_governed_tier_refuses_a_revision_that_does_not_match_the_pin(
    monkeypatch
):
  """The exact scenario: something moved the deployment underneath us."""
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, SHA)
  _state(monkeypatch, stamp=OTHER_SHA, git_revision=OTHER_SHA)
  with pytest.raises(provenance.UnpinnedRuntimeError) as excinfo:
    provenance.verify_pinned_runtime(require_pin=True)
  message = str(excinfo.value)
  assert OTHER_SHA in message and SHA in message
  assert "Re-deploy" in message


def test_governed_tier_refuses_an_edited_deployment(monkeypatch):
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, SHA)
  _state(monkeypatch, stamp=SHA, git_revision=SHA, dirty=True)
  with pytest.raises(provenance.UnpinnedRuntimeError) as excinfo:
    provenance.verify_pinned_runtime(require_pin=True)
  assert "never be edited in place" in str(excinfo.value)


def test_governed_tier_accepts_a_verified_pinned_runtime(monkeypatch):
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, SHA)
  _state(monkeypatch, stamp=SHA, git_revision=SHA, dirty=False)
  state = provenance.verify_pinned_runtime(require_pin=True)
  assert state["matches_pin"] is True
  assert state["immutable"] is True


def test_two_sessions_on_different_revisions_cannot_both_satisfy_one_pin(
    monkeypatch
):
  """The concurrency failure this control exists to prevent.

  Two instances loaded from different revisions under one pinned launch
  config: at most the matching one starts, the other refuses.
  """
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, SHA)

  _state(monkeypatch, stamp=SHA, git_revision=SHA)
  provenance.verify_pinned_runtime(require_pin=True)  # the intended one

  _state(monkeypatch, stamp=OTHER_SHA, git_revision=OTHER_SHA)
  with pytest.raises(provenance.UnpinnedRuntimeError):
    provenance.verify_pinned_runtime(require_pin=True)


def test_describe_provenance_is_stderr_safe_and_states_the_posture(
    monkeypatch
):
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, SHA)
  _state(monkeypatch, stamp=SHA, git_revision=SHA, dirty=True)
  line = provenance.describe_provenance(provenance.runtime_provenance())
  assert SHA[:12] in line
  assert "PINNED" in line and "DIRTY" in line
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


def test_provenance_tool_reports_the_tier_posture(monkeypatch):
  from ads_mcp.tools import runtime as runtime_tool

  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, SHA)
  monkeypatch.setenv("ADS_MCP_ENABLE_MUTATIONS", "true")
  monkeypatch.setenv("ADS_MCP_ENABLE_RAW_MUTATIONS", "false")
  _state(monkeypatch, stamp=SHA, git_revision=SHA)

  state = runtime_tool.get_runtime_provenance()
  assert state["revision"] == SHA
  assert state["matches_pin"] is True
  assert state["governed_tier_enabled"] is True
  assert state["raw_tier_enabled"] is False, "RAW must stay fail-closed"


# ---------------------------------------------------------------------------
# The serve-time boundary
#
# The check deliberately sits in main(), not at module import: importing the
# entrypoint to inspect the registry is legitimate, serving from a runtime
# that cannot prove its revision is not. These prove nothing is served.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entrypoint", ["stdio", "server"])
def test_entrypoint_refuses_to_serve_a_governed_unpinned_runtime(
    entrypoint, monkeypatch
):
  module = __import__(f"ads_mcp.{entrypoint}", fromlist=["main"])
  _state(monkeypatch, stamp=None, git_revision=SHA)
  monkeypatch.setattr(
      module.flags, "mutations_enabled", lambda: True
  )
  run = mock.Mock()
  monkeypatch.setattr(module.mcp_server, "run", run)

  with pytest.raises(provenance.UnpinnedRuntimeError):
    module.main()

  run.assert_not_called(), "the transport must never start"


@pytest.mark.parametrize("entrypoint", ["stdio", "server"])
def test_entrypoint_serves_when_the_runtime_is_verified(
    entrypoint, monkeypatch
):
  """Control: a properly pinned deployment still starts normally."""
  module = __import__(f"ads_mcp.{entrypoint}", fromlist=["main"])
  monkeypatch.setenv(provenance.PINNED_REVISION_VAR, SHA)
  _state(monkeypatch, stamp=SHA, git_revision=SHA, dirty=False)
  monkeypatch.setattr(module.flags, "mutations_enabled", lambda: True)
  monkeypatch.setattr(module.mcp_server, "run", mock.Mock())
  monkeypatch.setattr(module, "get_ads_client", mock.Mock())
  monkeypatch.setattr(
      module, "update_views_yaml", mock.Mock(return_value=None)
  )
  monkeypatch.setattr(module.asyncio, "run", mock.Mock())

  module.main()
  module.mcp_server.run.assert_called_once()
