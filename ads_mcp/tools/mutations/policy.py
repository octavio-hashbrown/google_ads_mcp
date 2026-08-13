# Copyright 2026 Lo Media Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Policy-violation parsing for keyword mutations.

Google documents exemption requests as a TWO-STEP process:

1. Submit the mutate. Google rejects offending operations and returns
   `PolicyViolationDetails`, each carrying a `PolicyViolationKey`
   (policy_name + violating_text) and an `is_exemptible` flag.
2. Resubmit ONLY the offending operations with exactly those keys set on
   `AdGroupCriterionOperation.exempt_policy_violation_keys`.

This module implements step 1's parsing. The keys handed to step 2 are
always the ones Google returned — never constructed locally, never
guessed. Two categories are deliberately never resubmitted:

- `is_exemptible=False` violations. No key will clear them.
- `PolicyFindingDetails` topic entries. These are findings, not keyed
  violations; `exempt_policy_violation_keys` does not apply to them.
"""

from typing import Any

from google.ads.googleads.errors import GoogleAdsException


def _has(proto_msg, field_name: str) -> bool:
  """Presence check for a singular field on a proto-plus message.

  proto-plus messages are always truthy, so `if msg.field:` is not a
  presence test. Drop to the underlying pb2 message, which does track
  presence for singular message fields and `optional` scalars.
  """
  try:
    return proto_msg._pb.HasField(field_name)  # pylint: disable=protected-access
  except (AttributeError, ValueError):
    return False


def operation_index(error) -> int | None:
  """Returns the index of the operation this error refers to, or None.

  Errors carry a `location.field_path_elements` trail such as
  `operations[3].create.keyword.text`. The element named "operations"
  holds the index we need to map a violation back to a specific keyword.
  """
  try:
    elements = error.location.field_path_elements
  except AttributeError:
    return None
  for element in elements:
    if element.field_name == "operations" and _has(element, "index"):
      return element.index
  return None


def _violation_finding(details) -> dict[str, Any]:
  """Builds a finding dict from a PolicyViolationDetails message."""
  key = details.key
  return {
      "kind": "violation",
      "policy_name": key.policy_name,
      "violating_text": key.violating_text,
      "external_policy_name": details.external_policy_name,
      "description": details.external_policy_description,
      "is_exemptible": bool(details.is_exemptible),
  }


def _topic_findings(details) -> list[dict[str, Any]]:
  """Builds finding dicts from a PolicyFindingDetails message."""
  out = []
  for entry in details.policy_topic_entries:
    out.append({
        "kind": "finding",
        "policy_name": entry.topic,
        "violating_text": "",
        "external_policy_name": entry.topic,
        "description": (
            f"Policy topic {entry.topic} "
            f"({entry.type_.name if hasattr(entry, 'type_') else ''})"
        ),
        # Topic findings are not key-exemptible. Certificates or an
        # in-UI appeal are the only routes, so never auto-resubmit.
        "is_exemptible": False,
    })
  return out


def parse_policy_errors(
    exception: GoogleAdsException,
) -> tuple[dict[int, list[dict[str, Any]]], list[str]]:
  """Splits a GoogleAdsException into per-operation policy findings.

  Args:
      exception: The exception raised by a failed mutate call.

  Returns:
      A 2-tuple of:
        - findings_by_index: {operation_index: [finding, ...]}. Findings
          for errors with no resolvable operation index are filed under
          key -1.
        - other_errors: Human-readable strings for every error that was
          NOT a policy violation/finding (quota, auth, bad field, etc).
          A non-empty list means the failure was not purely about policy
          and the caller must not treat it as exemptible.
  """
  findings_by_index: dict[int, list[dict[str, Any]]] = {}
  other_errors: list[str] = []

  for error in exception.failure.errors:
    index = operation_index(error)
    slot = index if index is not None else -1
    details = getattr(error, "details", None)

    if details is not None and _has(details, "policy_violation_details"):
      findings_by_index.setdefault(slot, []).append(
          _violation_finding(details.policy_violation_details)
      )
      continue

    if details is not None and _has(details, "policy_finding_details"):
      findings_by_index.setdefault(slot, []).extend(
          _topic_findings(details.policy_finding_details)
      )
      continue

    other_errors.append(error.message)

  return findings_by_index, other_errors


def exemptible_keys(findings: list[dict[str, Any]]) -> list[dict[str, str]]:
  """Returns the exemptible keys from a finding list, as plain dicts.

  Only `kind == "violation"` findings with `is_exemptible=True` produce a
  key. Everything else is dropped, which is what keeps a non-exemptible
  violation from ever being resubmitted.
  """
  return [
      {
          "policy_name": f["policy_name"],
          "violating_text": f["violating_text"],
      }
      for f in findings
      if f["kind"] == "violation" and f["is_exemptible"]
  ]


def blocking_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Returns the findings that no exemption key can clear."""
  return [f for f in findings if not f["is_exemptible"]]


def describe(findings: list[dict[str, Any]]) -> str:
  """One-line human summary of a finding list, for logs and reports."""
  parts = []
  for f in findings:
    flag = "exemptible" if f["is_exemptible"] else "NOT exemptible"
    parts.append(f"{f['policy_name']} ({flag})")
  return "; ".join(parts) if parts else "(none)"
