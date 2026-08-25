"""Fail-closed feature flags for the Lo Media governance layer.

Two distinct permission tiers, deliberately separated:

  Tier 1 — GOVERNED (`ADS_MCP_ENABLE_MUTATIONS=true`)
      Exposes the propose -> approve -> apply workflow. Nothing in this
      tier can write to Google Ads without a hash-verified approval file
      that a human created by saying "approve <CODE>".

  Tier 2 — RAW (`ADS_MCP_ENABLE_MUTATIONS=true` AND
                `ADS_MCP_ENABLE_RAW_MUTATIONS=true`)
      Exposes the upstream immediate-execution tools. These write to a
      live account the moment they are called: no proposal, no approval
      code, no audit row, no AFTER-state verification. Several can start
      or increase spend on their first call.

Tier 2 is a strict superset of Tier 1: raw tools require BOTH variables,
so there is no configuration in which raw tools load while the governed
layer is off.

Parsing is deliberately strict. A value counts as true only when it is
exactly "true" after trimming whitespace and lowercasing. Unset, empty,
malformed ("yes", "1", "TRUE!", "maybe"), or explicitly false all
resolve to False. There is no code path in which an unparseable value
opens a gate -- ambiguity fails closed.
"""

import os

ENABLE_MUTATIONS_VAR = "ADS_MCP_ENABLE_MUTATIONS"
ENABLE_RAW_MUTATIONS_VAR = "ADS_MCP_ENABLE_RAW_MUTATIONS"

_TRUE = "true"


def _strict_true(var_name: str) -> bool:
  """Returns True only for an exact, unambiguous "true".

  Args:
      var_name: Environment variable to read.

  Returns:
      True when the trimmed, lowercased value is exactly "true".
      False for every other input, including unset and malformed values.
  """
  raw = os.getenv(var_name)
  if raw is None:
    return False
  return raw.strip().lower() == _TRUE


def mutations_enabled() -> bool:
  """True when the governed propose/approve/apply tier is enabled."""
  return _strict_true(ENABLE_MUTATIONS_VAR)


def raw_mutations_enabled() -> bool:
  """True only when BOTH mutation flags are explicitly "true".

  The compound condition is the safety property: enabling raw tools is
  never a single-variable mistake, and raw tools can never load while
  the governed layer is off.
  """
  return mutations_enabled() and _strict_true(ENABLE_RAW_MUTATIONS_VAR)


def posture() -> dict[str, object]:
  """Returns the resolved flag posture, for startup logging and tests."""
  return {
      ENABLE_MUTATIONS_VAR: os.getenv(ENABLE_MUTATIONS_VAR),
      ENABLE_RAW_MUTATIONS_VAR: os.getenv(ENABLE_RAW_MUTATIONS_VAR),
      "governed_mutations_enabled": mutations_enabled(),
      "raw_mutations_enabled": raw_mutations_enabled(),
  }


def describe_posture() -> str:
  """One-line human-readable posture summary for stderr at startup."""
  if raw_mutations_enabled():
    tier = "RAW ENABLED -- immediate-execution tools are LIVE (no approval gate)"
  elif mutations_enabled():
    tier = "GOVERNED ONLY -- propose/approve/apply; raw tools NOT exposed"
  else:
    tier = "READ ONLY -- no mutation tools exposed"
  return f"[ads-mcp governance] {tier}"
