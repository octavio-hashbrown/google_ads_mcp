"""Approval-code lifecycle: collisions, retirement and supersession.

These run against a real filesystem under tmp_path -- real directories,
real files, real O_EXCL, real threads. Nothing here fakes the storage
layer, because the defect these tests exist for was a storage-layer
defect: `write_proposal` computed a code and wrote it without ever
asking whether that code had already been used.

The 2026-09-01 incident: re-proposing a retired Cop Call operation minted
`D62741` again -- the exact code stamped "must NOT be retried" -- and
wrote it straight into pending_approvals. Deterministic codes are the
intended design; not consulting the lifecycle was the hole.

One collision test uses a REAL truncated-hash collision found by search,
not a planted file, so it exercises the same condition production would
hit rather than a mock of it.
"""

import concurrent.futures
import pathlib

import pytest
from fastmcp.exceptions import ToolError

from ads_mcp.governance import approval


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _root(tmp_path: pathlib.Path) -> pathlib.Path:
  for name in (
      approval.PENDING_DIRNAME,
      approval.APPLIED_DIRNAME,
      approval.REJECTED_DIRNAME,
  ):
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
  return tmp_path


def _propose(root: pathlib.Path, spec: dict, **kwargs) -> dict:
  return approval.write_proposal(
      root,
      tool_name="attach_call_asset_to_campaign",
      customer_id="7849914897",
      operations_human=["Attach call asset to campaign"],
      reason_code="client_request",
      reason_detail="Step A of the approved phone remediation.",
      spec=spec,
      **kwargs,
  )


SPEC_A = {"op": "attach", "campaign": "22581514720", "asset": "328461156792"}
SPEC_B = {"op": "attach", "campaign": "22581514720", "asset": "330416214481"}


def _retire(root: pathlib.Path, code: str, note: str = "retired") -> pathlib.Path:
  """Archives a pending proposal the way the real reject path does."""
  return approval.archive_proposal(root, code, outcome="rejected", note=note)


def _apply(root: pathlib.Path, code: str) -> pathlib.Path:
  return approval.archive_proposal(root, code, outcome="applied", note="done")


def _find_real_truncated_collision() -> tuple[dict, dict, str]:
  """Two DIFFERENT specs whose 6-hex codes genuinely collide.

  Deterministic linear search -- no randomness, so the pair is stable
  across runs. sha1 truncated to 24 bits collides after a few thousand
  tries, which is exactly why the code must never be assumed unique.
  """
  seen: dict[str, int] = {}
  for i in range(2_000_000):
    spec = {"op": "attach", "probe": i}
    code = approval._generate_code(
        {"tool": "attach_call_asset_to_campaign",
         "customer_id": "7849914897",
         "spec": spec}
    )
    if code in seen:
      return {"op": "attach", "probe": seen[code]}, spec, code
    seen[code] = i
  raise AssertionError("no truncated-hash collision found")


# ---------------------------------------------------------------------------
# determinism is preserved
# ---------------------------------------------------------------------------


def test_same_operation_still_yields_the_same_code(tmp_path):
  """Idempotency is the feature. It must survive the fix."""
  root = _root(tmp_path)
  first = _propose(root, SPEC_A)
  second = _propose(root, SPEC_A)
  assert first["code"] == second["code"]


def test_identical_retry_returns_the_existing_file_unmodified(tmp_path):
  root = _root(tmp_path)
  first = _propose(root, SPEC_A)
  path = pathlib.Path(first["proposal_path"])
  before = path.read_bytes()
  stamp = path.stat().st_mtime_ns

  second = _propose(root, SPEC_A)

  assert second["proposal_path"] == first["proposal_path"]
  assert path.read_bytes() == before
  assert path.stat().st_mtime_ns == stamp, "the file was rewritten"


def test_no_random_nonce_leaks_into_the_code(tmp_path):
  """Two separate client roots, same operation, same code."""
  a = _root(tmp_path / "a")
  b = _root(tmp_path / "b")
  assert _propose(a, SPEC_A)["code"] == _propose(b, SPEC_A)["code"]


# ---------------------------------------------------------------------------
# collisions across every lifecycle
# ---------------------------------------------------------------------------


def test_rejected_code_is_never_reissued(tmp_path):
  """The Cop Call incident, reproduced then prevented."""
  root = _root(tmp_path)
  code = _propose(root, SPEC_A)["code"]
  retired = _retire(root, code, note="must NOT be retried")

  with pytest.raises(ToolError, match="already used by a rejected"):
    _propose(root, SPEC_A)

  assert not (root / approval.PENDING_DIRNAME / f"{code}.md").exists()
  assert retired.is_file(), "the retired artifact must survive untouched"


def test_applied_code_is_never_reissued(tmp_path):
  root = _root(tmp_path)
  code = _propose(root, SPEC_A)["code"]
  applied = _apply(root, code)

  with pytest.raises(ToolError, match="already used by an? applied"):
    _propose(root, SPEC_A)

  assert applied.is_file()


def test_pending_collision_with_a_different_operation_refuses(tmp_path):
  """A real truncated-hash collision, not a planted file."""
  root = _root(tmp_path)
  spec_one, spec_two, code = _find_real_truncated_collision()
  assert spec_one != spec_two

  first = _propose(root, spec_one)
  assert first["code"] == code
  before = pathlib.Path(first["proposal_path"]).read_bytes()

  with pytest.raises(ToolError, match="truncated-hash collision"):
    _propose(root, spec_two)

  assert pathlib.Path(first["proposal_path"]).read_bytes() == before


def test_archived_collision_names_the_occupying_path(tmp_path):
  root = _root(tmp_path)
  code = _propose(root, SPEC_A)["code"]
  retired = _retire(root, code)
  with pytest.raises(ToolError) as excinfo:
    _propose(root, SPEC_A)
  assert str(retired) in str(excinfo.value)
  assert "supersedes=" in str(excinfo.value), "must say how to proceed"


def test_date_prefixed_archive_filenames_are_still_found(tmp_path):
  """Archived files gain a YYYY-MM-DD_ prefix; the lookup must see it."""
  root = _root(tmp_path)
  code = _propose(root, SPEC_A)["code"]
  retired = _retire(root, code)
  assert retired.name.endswith(f"_{code}.md")
  assert retired.name != f"{code}.md"
  found = approval.locate_code(root, code)
  assert [lifecycle for lifecycle, _ in found] == ["rejected"]


def test_unreadable_occupant_is_not_treated_as_free(tmp_path):
  root = _root(tmp_path)
  code = approval._generate_code(
      {"tool": "attach_call_asset_to_campaign",
       "customer_id": "7849914897", "spec": SPEC_A}
  )
  squatter = root / approval.PENDING_DIRNAME / f"{code}.md"
  squatter.write_text("# PROPOSED MUTATION\n\nno yaml block here\n",
                      encoding="utf-8")

  with pytest.raises(ToolError, match="could not be read"):
    _propose(root, SPEC_A)

  assert "no yaml block here" in squatter.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# supersession
# ---------------------------------------------------------------------------


def test_valid_supersession_earns_a_new_deterministic_code(tmp_path):
  root = _root(tmp_path)
  original = _propose(root, SPEC_A)["code"]
  retired = _retire(root, original)

  replacement = _propose(root, SPEC_A, supersedes=original)

  assert replacement["code"] != original
  assert retired.read_text(encoding="utf-8").count("REJECTED note") == 1
  body = replacement["block"]
  assert f"Supersedes:** `{original}`" in body
  assert "(revision 1)" in body


def test_supersession_is_reproducible(tmp_path):
  """Same supersession twice -> same code. No nonce, no clock."""
  root_a = _root(tmp_path / "a")
  root_b = _root(tmp_path / "b")
  codes = []
  for root in (root_a, root_b):
    original = _propose(root, SPEC_A)["code"]
    _retire(root, original)
    codes.append(_propose(root, SPEC_A, supersedes=original)["code"])
  assert codes[0] == codes[1]


def test_revision_increments_along_the_chain(tmp_path):
  root = _root(tmp_path)
  first = _propose(root, SPEC_A)["code"]
  _retire(root, first)
  second = _propose(root, SPEC_A, supersedes=first)["code"]
  _retire(root, second)
  third = _propose(root, SPEC_A, supersedes=second)
  assert "(revision 2)" in third["block"]
  assert len({first, second, third["code"]}) == 3


def test_supersedes_missing_artifact_refuses(tmp_path):
  root = _root(tmp_path)
  with pytest.raises(ToolError, match="names no artifact"):
    _propose(root, SPEC_A, supersedes="ABC123")


def test_supersedes_a_still_pending_proposal_refuses(tmp_path):
  """Two approvable codes for one operation is exactly what to avoid."""
  root = _root(tmp_path)
  original = _propose(root, SPEC_A)["code"]
  with pytest.raises(ToolError, match="still PENDING"):
    _propose(root, SPEC_A, supersedes=original)


def test_supersession_must_be_the_same_operation(tmp_path):
  root = _root(tmp_path)
  original = _propose(root, SPEC_A)["code"]
  _retire(root, original)
  with pytest.raises(ToolError, match="not the same operation"):
    _propose(root, SPEC_B, supersedes=original)


def test_applied_supersession_without_evidence_refuses(tmp_path):
  root = _root(tmp_path)
  original = _propose(root, SPEC_A)["code"]
  _apply(root, original)
  with pytest.raises(ToolError, match="requires supersedes_evidence"):
    _propose(root, SPEC_A, supersedes=original)


def test_applied_supersession_with_evidence_is_allowed_and_recorded(tmp_path):
  root = _root(tmp_path)
  original = _propose(root, SPEC_A)["code"]
  _apply(root, original)

  evidence = "GAQL 2026-09-01: campaign_asset returns no ENABLED 914 link."
  replacement = _propose(
      root, SPEC_A, supersedes=original, supersedes_evidence=evidence
  )

  assert replacement["code"] != original
  assert evidence in replacement["block"]


def test_evidence_is_hash_protected(tmp_path):
  """Evidence is lifecycle metadata: hashed, so it cannot be edited
  after the fact, but excluded when comparing operations."""
  root_a = _root(tmp_path / "a")
  root_b = _root(tmp_path / "b")
  codes = []
  for root, evidence in ((root_a, "checked one way"), (root_b, "checked another")):
    original = _propose(root, SPEC_A)["code"]
    _apply(root, original)
    codes.append(
        _propose(root, SPEC_A, supersedes=original,
                 supersedes_evidence=evidence)["code"]
    )
  assert codes[0] != codes[1], "evidence must change the code"


def test_superseding_replacement_is_itself_protected(tmp_path):
  """A replacement, once retired, cannot be minted again either."""
  root = _root(tmp_path)
  original = _propose(root, SPEC_A)["code"]
  _retire(root, original)
  replacement = _propose(root, SPEC_A, supersedes=original)["code"]
  _retire(root, replacement)

  with pytest.raises(ToolError, match="already used by a rejected"):
    _propose(root, SPEC_A, supersedes=original)


# ---------------------------------------------------------------------------
# concurrency and overwrite refusal -- real threads, real filesystem
# ---------------------------------------------------------------------------


def test_concurrent_identical_proposals_write_exactly_one_file(tmp_path):
  root = _root(tmp_path)
  barrier = __import__("threading").Barrier(8)

  def worker():
    barrier.wait()
    return _propose(root, SPEC_A)

  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    results = [f.result() for f in
               [pool.submit(worker) for _ in range(8)]]

  codes = {r["code"] for r in results}
  assert len(codes) == 1
  written = list((root / approval.PENDING_DIRNAME).glob("*.md"))
  assert len(written) == 1, written


def test_concurrent_colliding_proposals_let_exactly_one_through(tmp_path):
  root = _root(tmp_path)
  spec_one, spec_two, _ = _find_real_truncated_collision()
  barrier = __import__("threading").Barrier(2)

  def worker(spec):
    barrier.wait()
    try:
      return ("ok", _propose(root, spec)["code"])
    except ToolError as e:
      return ("refused", str(e))

  # Both must be submitted before either result is awaited: the barrier
  # makes them collide on purpose, and waiting on one first would
  # deadlock the other before it ever reached the write.
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    futures = [pool.submit(worker, spec_one), pool.submit(worker, spec_two)]
    outcomes = [f.result(timeout=30) for f in futures]

  assert sorted(o[0] for o in outcomes) == ["ok", "refused"]
  assert len(list((root / approval.PENDING_DIRNAME).glob("*.md"))) == 1


def test_file_appearing_after_the_gate_is_still_refused(tmp_path, monkeypatch):
  """The O_EXCL backstop, in the window the pre-check cannot cover.

  Unit test, and it uses monkeypatch deliberately: the gate normally
  catches a collision before the write, so the only way to exercise the
  atomic-create backstop deterministically is to force the race rather
  than hope for it. Everything below the monkeypatch -- the create, the
  refusal, the untouched file -- is real filesystem behaviour.
  """
  root = _root(tmp_path)
  spec_one, spec_two, code = _find_real_truncated_collision()

  planted = root / approval.PENDING_DIRNAME / f"{code}.md"
  _propose(root, spec_one)
  assert planted.is_file()
  before = planted.read_bytes()

  real_resolver = approval._resolve_existing_code
  calls = {"n": 0}

  def _gate_sees_nothing(client_root, code_, spec_for_hash):
    calls["n"] += 1
    if calls["n"] == 1:
      return None  # the file has not appeared yet, as far as the gate knows
    return real_resolver(client_root, code_, spec_for_hash)

  monkeypatch.setattr(approval, "_resolve_existing_code", _gate_sees_nothing)

  with pytest.raises(ToolError, match="truncated-hash collision"):
    _propose(root, spec_two)

  assert planted.read_bytes() == before, "the racing write clobbered the file"


def test_existing_pending_file_is_never_overwritten(tmp_path):
  """Even a byte-identical retry must not rewrite the file on disk."""
  root = _root(tmp_path)
  result = _propose(root, SPEC_A)
  path = pathlib.Path(result["proposal_path"])
  sentinel = path.read_text(encoding="utf-8") + "\n<!-- operator note -->\n"
  path.write_text(sentinel, encoding="utf-8")

  # The spec block still verifies, so this is an identical retry.
  again = _propose(root, SPEC_A)

  assert again["code"] == result["code"]
  assert path.read_text(encoding="utf-8") == sentinel


# ---------------------------------------------------------------------------
# the guard is load-bearing at the real boundary
# ---------------------------------------------------------------------------


def test_round_trip_read_proposal_still_verifies_a_superseded_code(tmp_path):
  root = _root(tmp_path)
  original = _propose(root, SPEC_A)["code"]
  _retire(root, original)
  replacement = _propose(root, SPEC_A, supersedes=original)

  spec = approval.read_proposal(root, replacement["code"])

  assert spec["supersedes"] == original
  assert spec["proposal_revision"] == 1
  assert spec["spec"] == SPEC_A


def test_locate_code_reports_every_lifecycle_it_occupies(tmp_path):
  root = _root(tmp_path)
  code = _propose(root, SPEC_A)["code"]
  assert [lc for lc, _ in approval.locate_code(root, code)] == ["pending"]
  _retire(root, code)
  assert [lc for lc, _ in approval.locate_code(root, code)] == ["rejected"]
  assert approval.locate_code(root, "NOTACODE") == []
