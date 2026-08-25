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

"""Tests for server.py."""

import os
import sys
from unittest import mock

from ads_mcp import server


@mock.patch.dict(os.environ, {"USE_GOOGLE_OAUTH_ACCESS_TOKEN": "true"})
@mock.patch("ads_mcp.server.mcp_server")
@mock.patch("ads_mcp.server.get_ads_client")
@mock.patch("ads_mcp.server.update_views_yaml", new_callable=mock.Mock)
def test_main_with_oauth_env(
    mock_update_views, mock_get_ads_client, mock_mcp_server
):
  """Tests main function with USE_GOOGLE_OAUTH_ACCESS_TOKEN set."""
  with mock.patch("ads_mcp.server.asyncio.run"):
    server.main()

  mock_update_views.assert_called_once()
  mock_get_ads_client.assert_called_once()
  mock_mcp_server.run.assert_called_once_with(
      transport="streamable-http", show_banner=False
  )
  # Verify auth set (hard to verify exact type without exposing it better,
  # but we can check if it was accessed/set if we mock it differently,
  # or just rely on coverage hitting the line)


@mock.patch("ads_mcp.server.mcp_server")
@mock.patch("ads_mcp.server.get_ads_client")
@mock.patch("ads_mcp.server.update_views_yaml", new_callable=mock.Mock)
def test_main_no_env(mock_update_views, mock_get_ads_client, mock_mcp_server):
  """Tests main function with no env vars."""
  # pylint: disable=unused-argument
  with mock.patch("ads_mcp.server.asyncio.run"):
    server.main()

  mock_mcp_server.run.assert_called_once()
  mock_get_ads_client.assert_called_once()


def _reload_server(env: dict[str, str], *, clear: bool):
  """Re-imports server.py under a given environment and returns the module.

  Tool registration happens at import time, so the module must be evicted
  from sys.modules for the flag posture to be re-evaluated.
  """
  if "ads_mcp.server" in sys.modules:
    del sys.modules["ads_mcp.server"]

  with mock.patch.dict(os.environ, env, clear=clear):
    with (
        mock.patch("ads_mcp.tools._utils.get_ads_client"),
        mock.patch("ads_mcp.scripts.generate_views.update_views_yaml"),
    ):
      import ads_mcp.server as server_module  # pylint: disable=import-outside-toplevel, reimported

      return server_module


def test_mutations_disabled_by_default():
  """With no flags set, neither governed nor raw tiers load.

  Asserts on `loaded_tiers`, the loader's actual return value. The previous
  version of this test asserted on a `tools` list that was assigned but
  never consumed, so it passed while all twelve raw tools were in fact being
  registered by an import side effect. End-to-end registration is covered in
  tests/test_raw_mutation_gate.py.
  """
  server_module = _reload_server({}, clear=True)

  assert server_module.loaded_tiers["raw"] == []
  assert server_module.loaded_tiers["governed"] == []
  assert server_module.loaded_tiers["validate"] == []
  assert server_module.loaded_tiers["read"]


def test_mutations_enabled():
  """ADS_MCP_ENABLE_MUTATIONS=true loads the governed tier only."""
  server_module = _reload_server(
      {"ADS_MCP_ENABLE_MUTATIONS": "true"}, clear=False
  )

  assert "mutations_gated" in server_module.loaded_tiers["governed"]
  assert "gated_bidding" in server_module.loaded_tiers["governed"]
  # Validate-only probing survives without enabling raw mutations.
  assert server_module.loaded_tiers["validate"] == ["validation"]
  # Raw tier stays closed: it needs its own explicit opt-in.
  assert server_module.loaded_tiers["raw"] == []


def test_raw_mutations_require_second_flag():
  """Raw tools load only when BOTH flags are explicitly "true"."""
  server_module = _reload_server(
      {
          "ADS_MCP_ENABLE_MUTATIONS": "true",
          "ADS_MCP_ENABLE_RAW_MUTATIONS": "true",
      },
      clear=False,
  )

  assert "budget" in server_module.loaded_tiers["raw"]
  assert "campaign" in server_module.loaded_tiers["raw"]
  assert server_module.loaded_tiers["governed"]
