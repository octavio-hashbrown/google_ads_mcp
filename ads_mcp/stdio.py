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

"""The server for the Google Ads API MCP."""

import asyncio
import sys

from ads_mcp.coordinator import mcp_server
from ads_mcp.scripts.generate_views import update_views_yaml
from ads_mcp.tools import loader
from ads_mcp.tools._utils import get_ads_client
import dotenv

dotenv.load_dotenv()


# Tool tiers are loaded by ads_mcp.tools.loader, which is the single
# implementation shared by both entrypoints. Registration happens by import
# side effect, so a tier that loader does not import is not registered.
#
#   READ      always on.
#   VALIDATE  with governed tier; validate_only only, cannot commit.
#   GOVERNED  ADS_MCP_ENABLE_MUTATIONS=true       -> propose/approve/apply.
#   RAW       + ADS_MCP_ENABLE_RAW_MUTATIONS=true -> immediate execution.
#
# Both flags are required for RAW. Anything unset or malformed fails closed.
# See ads_mcp/governance/flags.py.
loaded_tiers = loader.load_tools()


def main():
  """Initializes and runs the MCP server."""
  asyncio.run(update_views_yaml())  # Check and update docs resource
  get_ads_client()  # Check Google Ads credentials
  print("mcp server starting...", file=sys.stderr)
  mcp_server.run(
      transport="stdio",
      show_banner=False,
  )  # Initialize and run the server


if __name__ == "__main__":
  main()
