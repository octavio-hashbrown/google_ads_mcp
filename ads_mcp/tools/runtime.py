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

  Three identities are reported independently and never inferred from
  one another, because an earlier version trusting the stamp alone would
  report one revision while executing a different one:

      pinned_revision: What ADS_MCP_PINNED_REVISION requires, if set.
      stamp_revision: What the deployment was cut from.
      git_head_revision: What is actually checked out right now.

  Other fields:
      revision: Best available identity, for display. Prefer comparing
          the three above.
      revision_source: "deployment_stamp", "git_working_tree" (a mutable
          developer checkout), or "unknown".
      matches_pin: True only when all three identities agree.
      working_tree_dirty: True when tracked files differ from HEAD.
          None means it could not be determined, which is not a pass.
      unexpected_untracked: Untracked paths other than the deployment
          stamp. These can shadow or extend what Python imports.
      detached_head: True when the deployment follows no branch.
      immutable: True only when stamped, still on that exact commit,
          still detached, unedited and with nothing added.
      governed_tier_enabled: Whether propose/approve/apply is loaded.
  """
  state = provenance.runtime_provenance()
  state["governed_tier_enabled"] = flags.mutations_enabled()
  state["raw_tier_enabled"] = flags.raw_mutations_enabled()
  return state
