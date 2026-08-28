# Lo Media Governance Layer

This fork adds an approval-gate workflow on top of the upstream Google Ads
mutation tools. The upstream code is untouched; the governance layer lives
in `ads_mcp/governance/` and `ads_mcp/tools/mutations_gated.py`.

## What it does

Every keyword-flavored mutation goes through **propose → approve → apply**:

1. **`propose_*`** validates the target via GAQL, generates a 6-char code,
   and writes a human-readable approval block to
   `{CLIENT_ROOT}/pending_approvals/<code>.md`.
2. The human reads the block and tells Claude "approve `<code>`".
3. **`apply_keyword_mutation(code)`** re-reads the pending file, verifies
   its hash hasn't been tampered with, executes the Google Ads API call,
   appends a row to `{CLIENT_ROOT}/client_audit_log.md`, and archives the
   pending file to `applied_approvals/`.

The hash check prevents arg-drift: if Claude (or anything else) edits the
yaml spec block in the pending file after proposing, the code won't match
and apply refuses.

## V1 scope (keywords only)

Gated tools:

| Tool | What it does |
|---|---|
| `propose_pause_keyword` | Pauses an existing ad-group keyword |
| `propose_enable_keyword` | Re-enables a paused ad-group keyword |
| `propose_remove_keyword` | Hard-deletes an ad-group keyword |
| `propose_add_ad_group_negative` | Adds negatives at ad-group level |
| `propose_add_campaign_negative` | Adds negatives at campaign level |
| `propose_add_to_shared_negative_list` | Adds to a shared set (Master Negative List) |
| `apply_keyword_mutation(code)` | Executes a proposed mutation |
| `reject_keyword_mutation(code, note)` | Rejects + archives a pending proposal |
| `list_pending_approvals()` | Lists open proposals in the client queue |

## ⚠️ Raw (un-gated) upstream tools — now behind a second flag

The upstream raw mutation tools execute immediately with **no approval
gate, no audit row, and no AFTER-state check**. Twelve of them exist, and
several can start or increase spend on their first call
(`update_campaign_status`, `create_campaign_budget`, `create_search_campaign`).

**As of 2026-08-25 they require a second, separate opt-in:**

```
ADS_MCP_ENABLE_MUTATIONS=true        # governed propose/approve/apply
ADS_MCP_ENABLE_RAW_MUTATIONS=true    # + raw immediate execution
```

Both must be exactly `"true"`. Unset, empty, `"false"`, or malformed
(`"1"`, `"yes"`, `"TRUE!"`) all fail closed. Raw is a strict superset of
governed: raw tools can never load while the governed layer is off.

**Default rule:** Lo Media client work runs with
`ADS_MCP_ENABLE_RAW_MUTATIONS=false`. Use the gated `propose_*` /
`apply_*` flow. Raw tools are for emergencies only (e.g. reverting a
misfire) and require deliberately restarting the MCP with the flag on —
which is the point: it cannot happen by accident mid-session.

### The defect this replaced

Before this change the flag did not work at all. Tool registration happens
as an **import side effect** (`@mcp.tool()` at module scope), and
`mutations/__init__.py` eagerly imported every raw tool module. Because
`planning.py` — a read-only module loaded unconditionally — imports
`mutations.common`, that chain registered all twelve raw tools in **every**
configuration. Verified 2026-08-25: with `ADS_MCP_ENABLE_MUTATIONS` unset,
12 raw tools were exposed and 0 governed tools were — strictly the worst
posture available. The `tools = [...]` list in the entrypoints was assigned
but never consumed, so it controlled nothing.

The fix is structural: `mutations/__init__.py` no longer imports the tool
modules, and `ads_mcp/tools/loader.py` is the single place that imports
each tier, shared by both entrypoints. Regression cover lives in
`tests/test_raw_mutation_gate.py`, which asserts on the tools a freshly
imported server *actually registers* — not on a list.

## Validate-only probing

`validate_only_capability_check` (in `ads_mcp/tools/validation.py`) loads
with the **governed** tier and does not require raw mutations. It confirms
that credentials, developer token, and OAuth scope are authorized to submit
mutations, without writing anything:

- `validate_only` is a pinned module constant, asserted before every call;
  there is no code path that sets it False.
- Every operation is **zero-delta** — it rewrites the value it just read —
  so the net change is nil even if the flag were ignored.
- Probe targets prefer PAUSED campaigns.

It proves a mutation *could* be submitted. It is never evidence that one
*should* be, and it is not a substitute for the approval gate.

## Configuration

- `ADS_MCP_ENABLE_MUTATIONS=true` — enables the **governed** tier
  (`propose_*` / `apply_*`) plus `validate_only_capability_check`.
- `ADS_MCP_ENABLE_RAW_MUTATIONS=true` — additionally enables the **raw**
  immediate-execution tools. Requires the above. Defaults to false.
- `ADS_MCP_PINNED_REVISION=<40-char commit>` — the exact revision this
  deployment must be running. **Required whenever the governed tier is
  enabled.** See *Pinned runtime* below.
- `LO_AGENCY_CLIENT_ROOT=/path/to/client/folder` — default location for
  `pending_approvals/`, `applied_approvals/`, `rejected_approvals/`,
  and `client_audit_log.md`. Each `propose_*` / `apply_*` call may also
  override via `client_root=...` arg.

The resolved posture is printed to stderr at startup, e.g.
`[ads-mcp governance] GOVERNED ONLY -- propose/approve/apply; raw tools NOT exposed`.

## Pinned runtime

A proposal is only as trustworthy as the code that framed it, so the
governed tier refuses to serve from a runtime that cannot prove which
revision it is executing.

**The defect this replaced.** The MCP was launched with
`uv run --directory <developer working tree>`, so each instance imported
whatever happened to be checked out when it spawned. On 2026-08-26 five
concurrent sessions were found running four different revisions — one
predating the governance work entirely — and nothing in a running process
could report which revision it was. Answering "what is this session
running?" required inspecting OS process tables against a git reflog.

**How it works now.**

- `ads_mcp/scripts/deploy_runtime.py` cuts a separate git worktree in
  **detached HEAD** at one commit. Detached is the point: it follows no
  branch, so ordinary development cannot move it.
- The deployment writes a `RUNTIME_REVISION` stamp, so the process can
  state its revision without shelling out to git.
- `ADS_MCP_PINNED_REVISION` in the launch config states the revision the
  operator intends. At **serve time** the entrypoint refuses to start if
  the runtime is unpinned, unprovable, mismatched, or edited in place.
  The check sits in `main()`, not at import: importing the module to
  inspect the registry is legitimate; serving unverified is not.
- `get_runtime_provenance` (READ tier) lets any session prove what it is
  talking to. It is READ tier on purpose — provable revision must survive
  every mutation tier being switched off.

Read-only use may stay unpinned, so local development is unaffected. The
governed tier may not.

```bash
uv run -m ads_mcp.scripts.deploy_runtime --revision origin/lo-media-mutations
```

The script prints the exact `~/.claude.json` block to use. Deployment
takes effect on the next session spawn: a running MCP keeps the code it
already imported.

## File layout

```
{CLIENT_ROOT}/
├── pending_approvals/
│   └── A7K2P3.md          ← human-readable + hash-verified yaml
├── applied_approvals/
│   └── 2026-05-19_A7K2P3.md
├── rejected_approvals/
│   └── 2026-05-19_B9X1F2.md
└── client_audit_log.md    ← append-only ledger
```

## Reason codes

Every propose tool requires a `reason_code` from this vocabulary:

- `underperforming` — high spend, low/zero conversions
- `irrelevant_intent` — search terms not matching service
- `competitor_term` — competitor brand or product
- `low_quality_score` — Quality Score issue
- `budget_protection` — wide-match bleeding budget
- `negative_consolidation` — moving to shared list
- `client_request` — explicit ask
- `other` — requires `reason_detail` free-text

The reason gets written to the audit log row, making the ledger queryable
later (e.g., "all keywords paused for `underperforming` in Q2").

## Tests

```bash
python -m pytest tests/test_governance.py -v
```

Covers code determinism, hash tamper detection, archive flow, audit row
format, and reason validation. Mutation execution itself isn't tested here
— that needs full Google Ads client mocking, deferred to a later pass.
