"""Lo Media governance layer for Google Ads mutations.

Adds an approval-gate workflow on top of raw mutation tools:

- Mutations are proposed (dry-run), generating a human-readable approval
  block and a 6-char confirmation code.
- The proposal is written to {CLIENT_ROOT}/pending_approvals/{code}.md.
- Apply re-reads the proposal, verifies its hash hasn't been tampered with,
  executes the mutation, and appends a row to {CLIENT_ROOT}/client_audit_log.md.

Per-client root is resolved from the `client_root` arg passed by the caller,
falling back to the LO_AGENCY_CLIENT_ROOT env var.
"""
