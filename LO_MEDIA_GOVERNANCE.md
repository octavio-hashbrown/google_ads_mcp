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

## ⚠️ Raw (un-gated) upstream tools are still available

By design choice, this fork **keeps the upstream raw mutation tools active**
when `ADS_MCP_ENABLE_MUTATIONS=true`. Tools like `create_keywords`,
`create_negative_campaign_keywords`, `remove_campaign_criterion` etc.
execute immediately with **no approval gate**.

**Default rule:** for Lo Media client work, always prefer the gated
`propose_*` / `apply_*` flow. The raw tools exist for emergencies (e.g.,
revert a misfire) where the propose round-trip would slow things down.
Use of raw tools should still be announced to the client in your normal
audit-execution rhythm.

## Configuration

- `ADS_MCP_ENABLE_MUTATIONS=true` — enables all mutation tools (raw + gated)
- `LO_AGENCY_CLIENT_ROOT=/path/to/client/folder` — default location for
  `pending_approvals/`, `applied_approvals/`, `rejected_approvals/`,
  and `client_audit_log.md`. Each `propose_*` / `apply_*` call may also
  override via `client_root=...` arg.

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
