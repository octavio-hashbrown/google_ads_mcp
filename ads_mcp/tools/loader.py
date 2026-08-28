"""Single place where tool tiers are loaded, shared by both entrypoints.

`stdio.py` and `server.py` previously carried duplicated gating blocks. Two
copies of a security control drift; this module is the one implementation
both call, so a tier can never be enabled in one transport and not the other.

Registration happens by import side effect (`@mcp.tool()` at module scope),
so "loading a tier" means "importing its modules". A tier that is not
imported is not registered and is invisible to the client -- there is no
second registry to also remember to update.

Tiers, in increasing order of authority:

  READ       always loaded. GAQL, account list, docs, Keyword Planner.
  VALIDATE   loaded with the governed tier. validate_only probes only;
             hardcoded so it cannot commit.
  GOVERNED   ADS_MCP_ENABLE_MUTATIONS=true. propose -> approve -> apply.
  RAW        ADS_MCP_ENABLE_MUTATIONS=true AND
             ADS_MCP_ENABLE_RAW_MUTATIONS=true. Immediate execution.
"""

import sys

from ads_mcp.governance import flags


def load_tools(*, log: bool = True) -> dict[str, object]:
  """Imports the tool modules permitted by the current flag posture.

  Args:
      log: When True, writes a one-line posture summary to stderr. stdout is
          reserved for the MCP stdio transport and must never be written to.

  Returns:
      A dict describing what was loaded, for startup logging and tests.
  """
  # ---- READ tier: always available. ------------------------------------
  from ads_mcp.tools import accounts  # pylint: disable=g-import-not-at-top
  from ads_mcp.tools import docs  # pylint: disable=g-import-not-at-top
  from ads_mcp.tools import planning  # pylint: disable=g-import-not-at-top
  from ads_mcp.tools import reporting  # pylint: disable=g-import-not-at-top
  from ads_mcp.tools import runtime  # pylint: disable=g-import-not-at-top

  loaded = {
      "read": ["reporting", "accounts", "docs", "planning", "runtime"],
      "validate": [],
      "governed": [],
      "raw": [],
  }
  # Imported for side effects only. `runtime` is READ tier on purpose: a
  # session must be able to prove which revision it is talking to even
  # when every mutation tier is switched off.
  del accounts, docs, planning, reporting, runtime

  # ---- GOVERNED tier + validate-only probe. ----------------------------
  if flags.mutations_enabled():
    from ads_mcp.tools import (  # pylint: disable=g-import-not-at-top
        gated_ad_copy,
        gated_assets,
        gated_bidding,
        gated_conversions,
        gated_migration,
        mutations_gated,
        validation,
    )

    del (gated_ad_copy, gated_assets, gated_bidding, gated_conversions,
         gated_migration, mutations_gated)
    loaded["governed"] = [
        "mutations_gated", "gated_bidding", "gated_assets",
        "gated_conversions", "gated_migration", "gated_ad_copy",
    ]
    loaded["validate"] = ["validation"]
    del validation

  # ---- RAW tier: requires BOTH flags. ----------------------------------
  # This is the only place the raw tool modules are imported anywhere in the
  # codebase. `mutations/__init__.py` deliberately does not import them.
  if flags.raw_mutations_enabled():
    from ads_mcp.tools.mutations import ad  # pylint: disable=g-import-not-at-top
    from ads_mcp.tools.mutations import ad_group  # pylint: disable=g-import-not-at-top
    from ads_mcp.tools.mutations import budget  # pylint: disable=g-import-not-at-top
    from ads_mcp.tools.mutations import campaign  # pylint: disable=g-import-not-at-top
    from ads_mcp.tools.mutations import criterion  # pylint: disable=g-import-not-at-top

    del ad, ad_group, budget, campaign, criterion
    loaded["raw"] = ["ad", "ad_group", "budget", "campaign", "criterion"]

  if log:
    print(flags.describe_posture(), file=sys.stderr)
    if flags.raw_mutations_enabled():
      print(
          "[ads-mcp governance] WARNING: raw tools bypass the approval gate "
          "and can change live spend on first call.",
          file=sys.stderr,
      )

  return loaded
