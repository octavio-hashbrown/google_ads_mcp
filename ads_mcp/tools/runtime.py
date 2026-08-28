"""Read-only tool that lets a session prove what code it is talking to.

Without this, answering "which revision is this MCP running?" meant
inspecting operating-system process tables and correlating spawn times
against a git reflog. That is forensics, not governance, and it is not
available to the session that actually needs the answer.
"""

from ads_mcp.coordinator import mcp_server as mcp
from ads_mcp.governance import flags
from ads_mcp.governance import provenance


@mcp.tool()
def get_runtime_provenance() -> dict:
  """Reports the exact revision this MCP process is executing.

  Read-only. Touches no Google Ads account and takes no customer ID.

  Use this before trusting any governed proposal: a proposal is only as
  trustworthy as the code that framed it. `revision` is None when the
  runtime genuinely cannot prove what it is running -- that is reported
  honestly rather than guessed.

  Fields:
      revision: The commit this process loaded, or None if unprovable.
      revision_source: "deployment_stamp" (authoritative, written when
          the deployment was cut), "git_working_tree" (a developer
          checkout, mutable), or "unknown".
      pinned_revision: What ADS_MCP_PINNED_REVISION requires, if set.
      matches_pin: Whether the running code matches that pin.
      working_tree_dirty: True when tracked files differ from the
          revision, meaning the imported code is not the reviewed code.
      immutable: True only when cut by a deployment and unedited since.
      governed_tier_enabled: Whether propose/approve/apply is loaded.
  """
  state = provenance.runtime_provenance()
  state["governed_tier_enabled"] = flags.mutations_enabled()
  state["raw_tier_enabled"] = flags.raw_mutations_enabled()
  return state
