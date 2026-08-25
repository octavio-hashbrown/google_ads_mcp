# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the raw-vs-governed mutation permission split.

Regression cover for a live defect found 2026-08-25: with
`ADS_MCP_ENABLE_MUTATIONS` unset, all twelve raw immediate-execution tools
were still exposed while zero governed tools were. Registration happens as an
import side effect (`@mcp.tool()` at module scope), and `planning.py` -- a
read-only module loaded unconditionally -- imports `mutations.common`, which
pulled in `mutations/__init__.py`, which imported every raw tool module.

The tests below assert on the tools a *freshly imported* server actually
registers, not on the flag-parsing helpers alone. Each case runs in a
subprocess so module-level import side effects cannot leak between cases.
"""

import json
import os
import subprocess
import sys

import pytest

from ads_mcp.governance import flags

# The twelve upstream tools that write to Google Ads the moment they are
# called: no proposal, no approval code, no audit row.
RAW_TOOL_NAMES = frozenset({
    "create_responsive_search_ad",
    "create_ad_group",
    "update_ad_group_status",
    "create_campaign_budget",
    "create_search_campaign",
    "update_campaign_status",
    "update_campaign_geo_target_type",
    "create_keywords",
    "create_negative_campaign_keywords",
    "create_geo_targeting",
    "remove_campaign_criterion",
    "exclude_geo_targets",
})

# Tools that must stay available in the normal governed configuration.
REQUIRED_READ_TOOLS = frozenset({
    "execute_gaql",
    "list_accessible_accounts",
    "generate_keyword_ideas",
    "generate_keyword_historical_metrics",
})

_LIST_TOOLS_SNIPPET = """
import asyncio, importlib, json
importlib.import_module("ads_mcp.{entry}")
from ads_mcp.coordinator import mcp_server

async def main():
    tools = await mcp_server.list_tools()
    print("@@@" + json.dumps(sorted(t.name for t in tools)))

asyncio.run(main())
"""


def _registered_tools(entry: str, env_overrides: dict[str, str | None]) -> set[str]:
  """Imports an entrypoint in a subprocess and returns its registered tools."""
  env = dict(os.environ)
  for key, value in env_overrides.items():
    if value is None:
      env.pop(key, None)
    else:
      env[key] = value
  # The entrypoints construct a Google Ads client only inside main(), which we
  # never call -- but _utils still needs a credentials path to exist if any
  # import touches it, so leave whatever the ambient environment provides.
  proc = subprocess.run(
      [sys.executable, "-c", _LIST_TOOLS_SNIPPET.format(entry=entry)],
      capture_output=True,
      text=True,
      check=False,
      env=env,
  )
  if proc.returncode != 0:
    pytest.fail(f"listing tools failed for {entry}:\n{proc.stderr[-2000:]}")
  marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("@@@")]
  assert marker, f"no tool listing emitted:\n{proc.stdout[-2000:]}"
  return set(json.loads(marker[-1][3:]))


# ---------------------------------------------------------------------------
# Flag parsing -- fail closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [None, "", "false", "False", "FALSE", "0", "1", "yes", "on", "TRUE!",
     "maybe", "true false"],
)
def test_raw_flag_rejects_non_true_values(monkeypatch, value):
  """Unset, empty, false, and malformed values must all fail closed."""
  monkeypatch.setenv(flags.ENABLE_MUTATIONS_VAR, "true")
  if value is None:
    monkeypatch.delenv(flags.ENABLE_RAW_MUTATIONS_VAR, raising=False)
  else:
    monkeypatch.setenv(flags.ENABLE_RAW_MUTATIONS_VAR, value)
  assert flags.raw_mutations_enabled() is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "tRuE", "  true  "])
def test_raw_flag_accepts_exact_true(monkeypatch, value):
  """Only an unambiguous "true" opens the gate."""
  monkeypatch.setenv(flags.ENABLE_MUTATIONS_VAR, "true")
  monkeypatch.setenv(flags.ENABLE_RAW_MUTATIONS_VAR, value)
  assert flags.raw_mutations_enabled() is True


@pytest.mark.parametrize("mutations", [None, "", "false"])
def test_raw_requires_governed_flag_too(monkeypatch, mutations):
  """RAW is a superset of GOVERNED: it can never load on its own."""
  if mutations is None:
    monkeypatch.delenv(flags.ENABLE_MUTATIONS_VAR, raising=False)
  else:
    monkeypatch.setenv(flags.ENABLE_MUTATIONS_VAR, mutations)
  monkeypatch.setenv(flags.ENABLE_RAW_MUTATIONS_VAR, "true")
  assert flags.raw_mutations_enabled() is False


# ---------------------------------------------------------------------------
# Import hygiene -- the actual defect.
# ---------------------------------------------------------------------------


def test_helper_modules_register_no_tools():
  """`mutations.common` and `.policy` must stay tool-free.

  Read-only modules import `mutations.common`. If a tool were ever added to
  it -- or to `policy` -- that tool would register for every importer,
  including the read tier.
  """
  import inspect

  from ads_mcp.tools.mutations import common, policy

  for module in (common, policy):
    source = inspect.getsource(module)
    assert "@mcp.tool" not in source, (
        f"{module.__name__} registers a tool; it is imported by read-only "
        "code and would leak that tool into the read tier."
    )


def test_mutations_package_does_not_import_tool_modules():
  """`mutations/__init__.py` must not eagerly import raw tool modules.

  This is the exact regression: importing the package registered all twelve
  raw tools for anything that touched it.
  """
  import inspect

  from ads_mcp.tools import mutations

  source = inspect.getsource(mutations)
  for module_name in ("ad", "ad_group", "budget", "campaign", "criterion"):
    assert f"import {module_name}\n" not in source, (
        f"mutations/__init__.py imports {module_name}, which re-registers "
        "raw tools for every importer including read-only modules."
    )


def test_importing_read_only_module_registers_no_raw_tools():
  """Importing the read tier alone must not pull in raw tools."""
  tools = _registered_tools(
      "tools.planning",
      {flags.ENABLE_MUTATIONS_VAR: None, flags.ENABLE_RAW_MUTATIONS_VAR: None},
  )
  assert not (tools & RAW_TOOL_NAMES), (
      f"read-only import leaked raw tools: {sorted(tools & RAW_TOOL_NAMES)}"
  )


# ---------------------------------------------------------------------------
# End-to-end tool exposure, both entrypoints.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["stdio", "server"])
def test_no_flags_exposes_no_mutations(entry):
  """Fail closed: nothing set means read-only."""
  tools = _registered_tools(
      entry,
      {flags.ENABLE_MUTATIONS_VAR: None, flags.ENABLE_RAW_MUTATIONS_VAR: None},
  )
  assert not (tools & RAW_TOOL_NAMES)
  assert not [t for t in tools if t.startswith(("propose_", "apply_"))]
  assert REQUIRED_READ_TOOLS <= tools


@pytest.mark.parametrize("entry", ["stdio", "server"])
@pytest.mark.parametrize("raw_value", [None, "false"])
def test_governed_config_hides_raw_tools(entry, raw_value):
  """The normal configuration: governed workflow on, raw tools absent."""
  tools = _registered_tools(
      entry,
      {flags.ENABLE_MUTATIONS_VAR: "true",
       flags.ENABLE_RAW_MUTATIONS_VAR: raw_value},
  )
  assert not (tools & RAW_TOOL_NAMES), (
      f"raw tools exposed without opt-in: {sorted(tools & RAW_TOOL_NAMES)}"
  )
  # Governed workflow intact.
  assert "apply_mutation" in tools
  assert "propose_pause_keyword" in tools
  assert "list_pending_approvals" in tools
  # Validate-only capability preserved without enabling raw mutations.
  assert "validate_only_capability_check" in tools
  # Reads and Keyword Planner intact.
  assert REQUIRED_READ_TOOLS <= tools


@pytest.mark.parametrize("entry", ["stdio", "server"])
def test_both_flags_expose_raw_tools(entry):
  """Explicit double opt-in is the only way to get raw tools."""
  tools = _registered_tools(
      entry,
      {flags.ENABLE_MUTATIONS_VAR: "true",
       flags.ENABLE_RAW_MUTATIONS_VAR: "true"},
  )
  assert RAW_TOOL_NAMES <= tools
  assert "apply_mutation" in tools
  assert REQUIRED_READ_TOOLS <= tools


# ---------------------------------------------------------------------------
# The validate-only probe must not be able to commit.
# ---------------------------------------------------------------------------


def test_validation_module_pins_validate_only_true():
  """`validation.py` must never set validate_only from an argument."""
  import ast
  import inspect

  from ads_mcp.tools import validation

  source = inspect.getsource(validation)
  assert "_VALIDATE_ONLY = True" in source
  assert "request.validate_only = _VALIDATE_ONLY" in source

  # Parse rather than grep: the module docstring legitimately contains the
  # prose "validate_only=true", which is not an assignment. The AST sees
  # only real bindings.
  def _is_pinned(node) -> bool:
    if isinstance(node, ast.Constant) and node.value is True:
      return True
    return isinstance(node, ast.Name) and node.id == "_VALIDATE_ONLY"

  def _describe(node) -> str:
    return ast.dump(node) if not isinstance(node, ast.Constant) else repr(node.value)

  tree = ast.parse(source)
  checked = 0
  for node in ast.walk(tree):
    # Keyword form: some_call(validate_only=X)
    if isinstance(node, ast.keyword) and node.arg == "validate_only":
      checked += 1
      assert _is_pinned(node.value), (
          f"validation.py passes validate_only={_describe(node.value)}; it "
          "must only ever be the pinned constant."
      )
    # Assignment form: request.validate_only = X  /  _VALIDATE_ONLY = X
    if isinstance(node, ast.Assign):
      for target in node.targets:
        name = getattr(target, "attr", None) or getattr(target, "id", None)
        if name in ("validate_only", "_VALIDATE_ONLY"):
          checked += 1
          assert _is_pinned(node.value), (
              f"validation.py assigns {name} = {_describe(node.value)}; it "
              "must only ever be the pinned constant."
          )

  assert checked >= 2, (
      "expected to find the pinned constant and at least one request "
      f"assignment, found {checked}"
  )
