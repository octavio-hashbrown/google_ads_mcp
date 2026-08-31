"""Real-boundary tests. Nothing here mocks git or the transport.

The 2026-08-31 incident got past a 50-test suite because every one of
those tests replaced `_git` with a stand-in. They proved the logic and
never the boundary. `get_runtime_provenance` then took ~30s against the
live deployment, returned an unresolved HEAD, and left an orphaned git
process behind.

So these tests do the two things the others deliberately did not: run the
real git helpers against this real repository, and drive a real FastMCP
stdio server over a real pipe.
"""

import json
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from ads_mcp.governance import provenance

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# A request must be answered promptly. The incident sat at ~30s and the
# caller gave up at 1800s; anything near that is a failure, not slowness.
REQUEST_DEADLINE = 15.0


# ---------------------------------------------------------------------------
# Real subprocess: no mocked _git
# ---------------------------------------------------------------------------


def test_git_helper_never_inherits_the_transport_stdin():
  """The one-line root cause, pinned so it cannot regress.

  Under the stdio transport, stdin IS the JSON-RPC pipe. A child that
  inherits it blocks until the timeout.
  """
  import inspect

  source = inspect.getsource(provenance._git)
  assert "stdin=subprocess.DEVNULL" in source


def test_real_git_revision_matches_this_real_repository():
  """No mock: the helper must agree with git run directly."""
  expected = subprocess.run(
      ["git", "rev-parse", "HEAD"],
      cwd=str(REPO_ROOT), capture_output=True, text=True, check=True,
  ).stdout.strip()

  actual = provenance._git_revision(REPO_ROOT)
  assert actual == expected
  assert provenance.is_commit_sha(actual)


def test_real_git_status_and_detachment_return_real_values():
  """Exercises the real parsing path against a real working tree."""
  tracked_dirty, untracked = provenance._git_status(REPO_ROOT)
  assert tracked_dirty in (True, False), "unknown is a refusal, not a value"
  assert isinstance(untracked, list)
  assert provenance._git_detached(REPO_ROOT) in (True, False)


def test_real_git_helper_returns_promptly():
  """Bounded by wall clock, not by faith."""
  started = time.monotonic()
  provenance._git_revision(REPO_ROOT)
  assert time.monotonic() - started < 10.0


def test_real_git_helper_fails_closed_on_a_non_repository(tmp_path):
  """A directory that is not a repo yields None, never a guess."""
  assert provenance._git_revision(tmp_path) is None
  assert provenance._git_status(tmp_path) == (None, None)
  assert provenance._git_detached(tmp_path) is None


# ---------------------------------------------------------------------------
# The startup snapshot
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_snapshot(monkeypatch):
  monkeypatch.setattr(provenance, "_STARTUP_SNAPSHOT", None)


def test_tool_refuses_when_startup_never_verified(_no_snapshot):
  """No snapshot means no attestation. Do not invent one by re-reading."""
  from fastmcp.exceptions import ToolError
  from ads_mcp.tools import runtime as runtime_tool

  with pytest.raises(ToolError, match="No startup-verified provenance"):
    runtime_tool.get_runtime_provenance()


def test_snapshot_is_bound_to_the_recording_process(_no_snapshot):
  import os

  provenance.record_startup_snapshot({"revision": "a" * 40})
  snapshot = provenance.startup_snapshot()
  assert snapshot["process_id"] == os.getpid()
  assert snapshot["reported_by_process_id"] == os.getpid()
  assert snapshot["process_identity_matches"] is True
  assert snapshot["verified_at_startup"] is True


def test_reading_the_snapshot_spawns_no_subprocess(_no_snapshot, monkeypatch):
  """The request path must not shell out. This is the whole fix."""
  def _explode(*args, **kwargs):
    raise AssertionError("the request path must never spawn a subprocess")

  provenance.record_startup_snapshot({"revision": "b" * 40})
  monkeypatch.setattr(subprocess, "run", _explode)
  monkeypatch.setattr(provenance.subprocess, "run", _explode)

  from ads_mcp.tools import runtime as runtime_tool

  state = runtime_tool.get_runtime_provenance()
  assert state["revision"] == "b" * 40
  assert state["raw_tier_enabled"] is False


def test_snapshot_cannot_be_mutated_through_the_returned_dict(_no_snapshot):
  provenance.record_startup_snapshot({"revision": "c" * 40})
  provenance.startup_snapshot()["revision"] = "tampered"
  assert provenance.startup_snapshot()["revision"] == "c" * 40


# ---------------------------------------------------------------------------
# Real FastMCP stdio server over a real pipe
# ---------------------------------------------------------------------------


class StdioServer:
  """Drives ads_mcp.stdio exactly as a client does: newline JSON over pipes."""

  def __init__(self, env_extra):
    import os

    env = dict(os.environ)
    env.update(env_extra)
    # main() constructs the Ads client at startup as a credentials check.
    # Nothing here calls Google; the file just has to exist.
    env.setdefault(
        "GOOGLE_ADS_CREDENTIALS",
        os.path.expanduser(r"~/.claude/secrets/google-ads.yaml"),
    )
    self.proc = subprocess.Popen(
        [sys.executable, "-m", "ads_mcp.stdio"],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
    )
    self._id = 0
    self._replies = {}
    self._lock = threading.Lock()
    threading.Thread(target=self._read, daemon=True).start()
    threading.Thread(
        target=lambda: [None for _ in self.proc.stderr], daemon=True
    ).start()

  def _read(self):
    for line in self.proc.stdout:
      line = line.strip()
      if not line:
        continue
      try:
        msg = json.loads(line)
      except json.JSONDecodeError:
        continue
      if "id" in msg:
        with self._lock:
          self._replies[msg["id"]] = msg

  def call(self, method, params=None, notify=False, deadline=60.0):
    with self._lock:
      self._id += 1
      rid = self._id
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
      msg["params"] = params
    if not notify:
      msg["id"] = rid
    self.proc.stdin.write(json.dumps(msg) + "\n")
    self.proc.stdin.flush()
    if notify:
      return None
    end = time.monotonic() + deadline
    while time.monotonic() < end:
      with self._lock:
        if rid in self._replies:
          return self._replies.pop(rid)
      if self.proc.poll() is not None:
        pytest.fail("the MCP process exited while serving a request")
      time.sleep(0.02)
    return None

  def handshake(self):
    reply = self.call("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1"},
    }, deadline=90)
    assert reply is not None, "server never completed initialize"
    self.call("notifications/initialized", {}, notify=True)

  def close(self):
    try:
      self.proc.kill()
      self.proc.wait(timeout=10)
    except Exception:
      pass


def _git_pids():
  """Every git.exe currently running, so orphans are detectable."""
  if not sys.platform.startswith("win"):
    out = subprocess.run(["pgrep", "-x", "git"], capture_output=True, text=True)
    return {l for l in out.stdout.split() if l}
  out = subprocess.run(
      ["powershell", "-NoProfile", "-NonInteractive", "-Command",
       "(Get-CimInstance Win32_Process -Filter \"Name='git.exe'\")"
       ".ProcessId -join ','"],
      capture_output=True, text=True,
  ).stdout.strip()
  return {p for p in out.split(",") if p}


@pytest.fixture
def stdio_server():
  server = StdioServer({
      # Read-only posture: the gate does not require a pin, so this runs
      # against an ordinary checkout without needing a deployment.
      "ADS_MCP_ENABLE_MUTATIONS": "false",
      "ADS_MCP_ENABLE_RAW_MUTATIONS": "false",
  })
  server.handshake()
  yield server
  server.close()


def _tool(server, name, deadline=REQUEST_DEADLINE):
  return server.call(
      "tools/call", {"name": name, "arguments": {}}, deadline=deadline
  )


def test_provenance_over_a_real_stdio_pipe_returns_within_the_deadline(
    stdio_server
):
  """The incident, as a test. It hung here; it must not again."""
  git_before = _git_pids()

  started = time.monotonic()
  reply = _tool(stdio_server, "get_runtime_provenance")
  elapsed = time.monotonic() - started

  assert reply is not None, (
      "get_runtime_provenance did not answer within %.0fs -- this is the "
      "2026-08-31 hang" % REQUEST_DEADLINE
  )
  assert elapsed < REQUEST_DEADLINE
  assert "error" not in reply, reply.get("error")

  payload = json.loads(reply["result"]["content"][0]["text"])
  assert payload["verified_at_startup"] is True
  assert payload["process_identity_matches"] is True
  assert payload["raw_tier_enabled"] is False, "RAW must stay zero"

  # No git was spawned to answer it, and nothing was orphaned.
  assert _git_pids() <= git_before, "the request path spawned/leaked git"


def test_mcp_survives_and_still_serves_in_the_same_process(stdio_server):
  """It previously took the server down. Prove liveness and pid stability."""
  first = _tool(stdio_server, "get_runtime_provenance")
  assert first is not None
  pid_after_first = stdio_server.proc.pid

  # A harmless read tool, the GAQL-equivalent that needs no credentials.
  follow_up = _tool(stdio_server, "get_gaql_doc", deadline=30)
  assert follow_up is not None, "the server stopped serving after provenance"
  assert "error" not in follow_up, follow_up.get("error")

  assert stdio_server.proc.poll() is None, "the MCP process died"
  assert stdio_server.proc.pid == pid_after_first, "the server was respawned"


def test_repeated_provenance_calls_stay_bounded(stdio_server):
  """Three calls in a row must not accumulate cost or leak processes."""
  git_before = _git_pids()
  for _ in range(3):
    started = time.monotonic()
    reply = _tool(stdio_server, "get_runtime_provenance")
    assert reply is not None
    assert time.monotonic() - started < REQUEST_DEADLINE
  assert _git_pids() <= git_before


def test_startup_gate_still_fails_closed_over_the_real_transport():
  """Governed tier + no pin must refuse to serve, not start and hope."""
  server = StdioServer({
      "ADS_MCP_ENABLE_MUTATIONS": "true",
      "ADS_MCP_ENABLE_RAW_MUTATIONS": "false",
      "ADS_MCP_PINNED_REVISION": "",
  })
  try:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and server.proc.poll() is None:
      time.sleep(0.1)
    assert server.proc.poll() is not None, (
        "the governed tier started without a pinned runtime"
    )
    assert server.proc.returncode != 0
  finally:
    server.close()
