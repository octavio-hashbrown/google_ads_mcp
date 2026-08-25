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

"""Raw (un-gated) mutation tools for the Google Ads API.

SAFETY -- READ BEFORE ADDING AN IMPORT HERE.

Tool registration in this codebase happens as an *import side effect*: the
`@mcp.tool()` decorators in `ad`, `ad_group`, `budget`, `campaign`, and
`criterion` run at module import time and attach themselves to the shared
`mcp_server` singleton. Nothing consults a registry list afterwards.

This package therefore deliberately does NOT import those five modules.
Importing them here would register all twelve immediate-execution tools for
*any* process that touches this package -- and read-only modules do touch it:
`planning.py` imports `mutations.common` for its Google Ads client helper,
which is enough to pull in this `__init__`.

That exact chain was a live defect (found 2026-08-25): with
`ADS_MCP_ENABLE_MUTATIONS` unset, all 12 raw spend-capable tools were still
exposed while zero governed tools were, because `planning.py` -> `common` ->
this `__init__` -> the tool modules registered them unconditionally.

Only `common` and `policy` are imported here. Both are pure helpers and
register no tools -- verified by test, not by assumption
(`tests/test_raw_mutation_gate.py::test_helper_modules_register_no_tools`).

The five tool modules are imported -- and therefore registered -- in exactly
one place: `ads_mcp/tools/loader.py`, and only when
`ADS_MCP_ENABLE_MUTATIONS=true` AND `ADS_MCP_ENABLE_RAW_MUTATIONS=true`.
`loader.load_tools()` is called by both entrypoints, `ads_mcp/stdio.py` and
`ads_mcp/server.py`, so neither transport carries its own copy of the gate.
See `ads_mcp/governance/flags.py` for the flag semantics.
"""

from ads_mcp.tools.mutations import common
from ads_mcp.tools.mutations import policy

# NOTE: `ad`, `ad_group`, `budget`, `campaign`, and `criterion` are
# intentionally absent. See the module docstring. Adding them re-opens the
# gate for every importer, including read-only tools.
__all__ = [
    "common",
    "policy",
]
