"""R20 on c7e9449: t2's one scope trim was told only the change's total and
spent 8 rounds and 373,366 reported tokens, more than the draft it trimmed,
while the harness already held the per-file counts and the diff. Every trim
now carries what the harness measured: per-file counts split into code and
tests, and the task's diff whole or stated as omitted, filtered by file
identity to what every scope over the task permits. Whether that saves
rounds is a hypothesis; these controls pin what reaches the prompt.

And the terminal record says when what was left could not admit an
ordinary call (``budget_note``), as an observation, never a stop.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadratus.config import Settings
from quadratus.project import Project
from quadratus.scope import TaskScope
from quadratus.session import _TRIM_DIFF_MAX_CHARS, Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, Script
from tests.lifecycle.test_step2_recovery import _BLOATED, _FINDING, _R1, _TYPED, DECL_SIMPLE, _plan

MEASURED = "## What the harness measured of this change"


def _session(root: Path, *, excludes=(), outer=None):
    session = Session.__new__(Session)
    session.project = str(root)
    session.config = SimpleNamespace(project_excludes=tuple(excludes), default_scope=outer)
    session._outcome = None
    return session


def _evidence(tmp_path, before_files, after_files, scope, *, excludes=(), outer=None):
    root = tmp_path / "project"
    root.mkdir()
    for name, data in before_files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    excluded = [str(root / e) for e in excludes]
    before = Project(root, exclude=excluded).contents()
    for name, data in after_files.items():
        path = root / name
        if data is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    session = _session(root, excludes=excluded, outer=outer)
    spec = SimpleNamespace(scope=scope)
    return session._trim_evidence(spec, before), session, spec, before, root


def test_only_files_every_scope_permits_reach_the_trim_on_both_sides(tmp_path):
    """Filtered by file identity before rendering, on both sides: a changed
    file outside the scope, a forbidden one inside a permitted glob, one an
    operator limit forbids, an excluded tree, a .env file and a name that
    looks like a diff header never appear, by name or by content."""
    scope = TaskScope(permitted_paths=["app.py", "tests/*", "lib/*"], forbidden_paths=["lib/secret.py"],
                      max_lines=40)
    outer = TaskScope(permitted_paths=["*", "*/*"], forbidden_paths=["tests/private_case.py"])
    before = {"app.py": b"x = 1\n", "outside.py": b"o = 1\n", "lib/secret.py": b"s = 1\n",
              "tests/test_app.py": b"def test_x():\n    pass\n", "tests/private_case.py": b"p = 1\n",
              "vendor/keep.py": b"v = 1\n", ".env": b"TOKEN=one\n"}
    after = {"app.py": b"x = 2\n", "outside.py": b"OUTSIDE-SENTINEL\n", "lib/secret.py": b"SECRET-SENTINEL\n",
             "tests/test_app.py": b"def test_x():\n    assert True\n", "tests/private_case.py": b"PRIVATE-SENTINEL\n",
             "vendor/keep.py": b"EXCLUDED-SENTINEL\n", ".env": b"TOKEN=ENV-SENTINEL\n",
             "x\n+++ b/outside.py": b"HEADER-SENTINEL\n"}
    text, *_ = _evidence(tmp_path, before, after, scope, excludes=["vendor"], outer=outer)
    assert MEASURED in text
    assert "- app.py: 2 line(s), code, changed" in text
    assert "- tests/test_app.py: 2 line(s), test, changed" in text
    assert "Code lines: 2; test lines: 2" in text
    for leak in ("outside.py", "OUTSIDE", "secret", "SECRET", "private_case", "PRIVATE", "vendor", "EXCLUDED",
                 ".env", "ENV-SENTINEL", "HEADER-SENTINEL"):
        assert leak not in text, leak


def test_deleted_added_and_binary_files_are_measured_and_counts_match_the_scope_report(tmp_path):
    scope = TaskScope(permitted_paths=["*.py", "*.bin"], max_lines=40)
    before = {"gone.py": b"a = 1\nb = 2\n", "kept.py": b"k = 1\n", "blob.bin": b"\x00\x01"}
    after = {"gone.py": None, "kept.py": b"k = 2\n", "new.py": b"n = 1\n", "blob.bin": b"\xff\xfe"}
    text, session, spec, before_map, root = _evidence(tmp_path, before, after, scope)
    assert "- gone.py: 2 line(s), code, deleted" in text and "--- a/gone.py\n+++ /dev/null" in text
    assert "- new.py: 1 line(s), code, added" in text
    assert "- blob.bin: 0 line(s), code, changed" in text and "Binary file changed: blob.bin" in text
    report = scope.assess(Project(root).diff(before_map))
    assert f"Code lines: {report.code_lines}; test lines: {report.test_lines}" in text, \
        "the same counts the stop reads"


def test_an_oversized_diff_is_omitted_whole_never_cut(tmp_path):
    scope = TaskScope(permitted_paths=["app.py"], max_lines=40)
    big = "".join(f"value_{i} = {'x' * 60}\n" for i in range(_TRIM_DIFF_MAX_CHARS // 40)).encode()
    text, *_ = _evidence(tmp_path, {"app.py": b"x = 1\n"}, {"app.py": big}, scope)
    assert "- app.py:" in text, "counts are always given"
    assert "```diff" not in text and "value_0" not in text, "no prefix of the diff"
    assert f"over the {_TRIM_DIFF_MAX_CHARS:,}-character bound for this prompt, so it is not shown" in text


def test_the_trim_reads_the_source_as_it_is_now(tmp_path):
    """Measured fresh at the trim, not from an earlier measurement."""
    scope = TaskScope(permitted_paths=["app.py"], max_lines=40)
    text, session, spec, before, root = _evidence(tmp_path, {"app.py": b"x = 1\n"}, {"app.py": b"x = 2\n"}, scope)
    assert "+x = 2" in text
    (root / "app.py").write_text("x = 3\n")
    later = session._trim_evidence(spec, before)
    assert "+x = 3" in later and "+x = 2" not in later


def test_a_review_only_scope_or_no_change_carries_nothing(tmp_path):
    text, *_ = _evidence(tmp_path, {"app.py": b"x = 1\n"}, {"app.py": b"x = 2\n"},
                         TaskScope(permitted_paths=["app.py"], review_only=True))
    assert text == ""


# -- the three trim sites, whole controller ------------------------------------------------

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("site", ["finished-draft", "capped-draft", "revision"])
def test_every_trim_site_carries_the_measured_change(tmp_path, monkeypatch, site):
    """Old/new: on c7e9449 no trim prompt carries the per-file counts or the
    diff. Now the finished-draft trim, the capped-draft trim and the
    revision trim each do, with no added call and the stop, target and
    allowance as before."""
    declared = "REQUIREMENTS:\nR1: " + _R1 + "\n" + DECL_SIMPLE + "\nCOVERS: R1"

    def lead(call, replay):
        H.write(call, {"app.py": _TYPED if site == "revision" else _BLOATED})
        if site == "capped-draft":
            return H.grok_ok("Still adding helpers", stop="cancelled", num_turns=8)
        return 'Implemented add.\nCHANGED: ["app.py"]'

    def revision(call, replay):
        if "Bring the change down" in call.prompt:
            H.write(call, {"app.py": _TYPED})
            return 'Trimmed.\nCHANGED: ["app.py"]'
        H.write(call, {"app.py": _BLOATED})
        return 'Revised.\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        if site == "revision" and not replay.of("revision"):
            return _FINDING + "\nR1: NOT MET - TypeError\nREVIEW: COMPLETE"
        return "NO FINDINGS\nR1: MET - app.py:5"

    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=_plan(declared), lead=lead, revision=revision, collaborator=collaborator,
        recheck=lambda c, r: "RESOLVED", auditor=lambda c, r: "R1: MET - app.py:5",
        **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=1, settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    trims = [c for c in replay.calls if c.role == "revision" and "Bring the change down" in c.prompt]
    assert len(trims) == 1, [c.role for c in replay.calls]
    prompt = trims[0].prompt
    assert MEASURED in prompt and "- app.py:" in prompt and ", code, changed" in prompt
    assert "```diff" in prompt and "+def _unused_0():" in prompt
    assert "Aim for about" in prompt, "the target is unchanged"


# -- B: the terminal budget note -------------------------------------------------------------

def test_the_terminal_record_says_when_the_rest_could_not_admit_an_ordinary_call():
    """From the same terminal snapshot; never a stop or a failure fact; says
    a shaped summary call reserves less; says when usage went unreported."""
    from quadratus.project_run import _budget_fields

    def budget(used, reserve=200_000, unknown=0, stop=""):
        snap = dict(limits=dict(max_reported_tokens=2_000_000, reserve_tokens_per_call=reserve),
                    reported_tokens=used, unknown_usage_attempts=unknown, stop_reason=stop)
        return SimpleNamespace(snapshot=lambda: snap)

    note = _budget_fields(budget(1_858_053))["budget_note"]
    assert note["remaining_reported_tokens"] == 141_947 and note["ordinary_call_reserve"] == 200_000
    assert note["stop_reason"] == "" and note["summary_call_reserve"] == 64_000
    assert note["text"] == (
        "At the end of the run 141,947 reported tokens remained, below the ordinary-call token reservation of "
        "200,000. The token remainder covers the 64,000-token reservation for a summary-only call whose CLI "
        "enforces one turn. This is a token-capacity observation, not the run's stop or an admission decision; "
        "other limits and workflow conditions are not assessed.")
    tight = _budget_fields(budget(2_000_000 - 50_000))["budget_note"]
    assert "The token remainder does not cover the 64,000-token reservation" in tight["text"]
    for reserve in (600, 64_000):
        # At or under the summary allowance the shaped reservation equals
        # the ordinary one (root on b4d8398).
        same = _budget_fields(budget(2_000_000 - 500, reserve=reserve))["budget_note"]
        assert same["summary_call_reserve"] == reserve
        assert f"does not cover the {reserve:,}-token reservation" in same["text"] and "less" not in same["text"]
    assert _budget_fields(budget(1_000_000))["budget_note"] is None, "room left: no note"
    assert _budget_fields(budget(1_858_053, reserve=0))["budget_note"] is None, "no reserve configured"
    assert "may be lower" in _budget_fields(budget(1_900_000, unknown=2))["budget_note"]["text"]
    assert _budget_fields(None) == {"budget": None, "budget_note": None}


def _starved_run(tmp_path, monkeypatch, where, total):
    """One run of the reserve-starved scenario under ``total`` tokens: a
    600-token reserve, no finish hold, one simple task, DONE, the audit."""
    from quadratus.run_budget import RunLimits
    declared = "REQUIREMENTS:\nR1: " + _R1 + "\n" + DECL_SIMPLE + "\nCOVERS: R1"

    def lead(call, replay):
        H.write(call, {"app.py": _TYPED})
        return 'Implemented add.\nCHANGED: ["app.py"]'
    where.mkdir()
    return H.run(where, monkeypatch, Script(
        orchestrator=_plan(declared), lead=lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py:5",
        auditor=lambda c, r: "R1: MET - app.py:5", **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=1, record_complete=False,
        limits=RunLimits(max_calls=120, max_reported_tokens=total, wall_seconds=600,
                         max_concurrent_workers=2, reserve_tokens_per_call=600))


@pytest.mark.requirements_ledger
def test_a_reserve_starved_terminal_keeps_its_own_stop_and_outcome(tmp_path, monkeypatch):
    """Whole controller: the note is metadata beside the run's own result;
    completed, error and the typed outcome are what they would have been.
    Every call is admitted and the run ends with 500, under the 600 reserve.

    Restaged on the admitted route (root's cap reconciliation on bc421f8):
    t1's selected route (the draft and the review, each at the 600 reserve
    here: 1,200; a correction is admitted only when a finding selects it,
    admission/2) is admitted at dispatch only from what the allowance holds
    after the calls already made, so the harness's default 110-token calls
    cannot both admit the route and end 500 under the reserve (that
    refusal is kept below). With each call reporting 590 (under the
    reserve; test-local H.USAGE), the measured spend is 3,540 over six
    calls; the tight allowance of used + 500 = 4,040 admits the route at
    dispatch (2,860 left against 1,200), every later call starts with at
    least 1,090, and the final audit ends with 500. Same calls, same
    admitted route, same stop and outcome as the roomy run; the receipts
    are asserted, not assumed."""
    # The finish hold (Davis, 2026-10-16) would refuse an earlier ordinary
    # call; this case pins the limit itself, so the hold is off here and is
    # controlled in tests/lifecycle/test_finish_capacity.py.
    monkeypatch.setattr("quadratus.session.Session._protect_finish", lambda self: None)
    monkeypatch.setattr(H, "USAGE", {"input_tokens": 580, "output_tokens": 10})
    roomy = _starved_run(tmp_path, monkeypatch, tmp_path / "roomy", 6_000_000)
    used = H.result_json(roomy)["budget"]["reported_tokens"]
    assert used == 590 * len(roomy.calls) == 3_540, (used, len(roomy.calls))
    tight = _starved_run(tmp_path, monkeypatch, tmp_path / "tight", used + 500)
    result = H.result_json(tight)
    assert [c.role for c in tight.calls] == [c.role for c in roomy.calls], "the same calls, every one admitted"
    admitted = [a for a in result["plan_admissions"] if a["task"] == "t1" and a["scope"] == "full"]
    assert admitted and admitted[-1]["feasible"] and len(admitted[-1]["stages"]) == 2, admitted
    assert "2 call(s), 1,200 tokens" in admitted[-1]["detail"] and "2,860 tokens" in admitted[-1]["detail"]
    assert used + 500 - 590 * admitted[-1]["at_attempt"] == 2_860 >= 1_200, "the route was paid from what was left"
    assert result["not_started"] == [] and result["finish_refusals"] == []
    assert H.result_json(roomy)["budget_note"] is None
    assert result["budget_note"]["remaining_reported_tokens"] == 500
    assert "does not cover the 600-token reservation" in result["budget_note"]["text"]
    assert (tight.result.completed, tight.result.error) == (roomy.result.completed, roomy.result.error)
    assert result["workflow"]["run"] == H.result_json(roomy)["workflow"]["run"], "no new fact"
    assert json.dumps(result["budget"]["stop_reason"]) == json.dumps(H.result_json(roomy)["budget"]["stop_reason"])


@pytest.mark.requirements_ledger
def test_an_allowance_that_cannot_pay_the_route_never_reaches_the_reserve_starved_terminal(tmp_path, monkeypatch):
    """The original fixture's raw failure on bc421f8, kept as the control:
    with the harness's default 110-token calls, the tight allowance of
    used + 500 holds 940 at dispatch against t1's two-call route at the
    600 reserve (1,200), so the task is NOT STARTED before any task call,
    the shortfall is named, no reserve-starved terminal is reached and no
    budget note is written. The run's own stop stands: incomplete,
    RequirementsUnmet, nothing written."""
    monkeypatch.setattr("quadratus.session.Session._protect_finish", lambda self: None)
    roomy = _starved_run(tmp_path, monkeypatch, tmp_path / "roomy", 6_000_000)
    used = H.result_json(roomy)["budget"]["reported_tokens"]
    assert used == 110 * len(roomy.calls)
    tight = _starved_run(tmp_path, monkeypatch, tmp_path / "tight", used + 500)
    result = H.result_json(tight)
    assert not tight.result.completed and "RequirementsUnmet" in (tight.result.error or "")
    assert [n["task"] for n in result["not_started"]] == ["t1"] and not tight.of("lead")
    (refusal,) = [r for r in result["finish_refusals"] if r["task"] == "t1"]
    assert "2 call(s), 1,200 tokens" in refusal["detail"] and "short by tokens" in refusal["detail"], refusal
    assert result["budget_note"] is None
    assert (tight.project / "app.py").read_text() == FILES["app.py"]


@pytest.mark.parametrize("exhausted", ["calls", "wall"])
def test_the_budget_note_is_token_capacity_never_an_admission_prediction(tmp_path, exhausted):
    """Root on 38ec681: with every call used, or the wall clock spent, the
    note said a summary call could still be admitted while RunBudget refuses
    it. The note now states token capacity only, says other limits are not
    assessed, and reading it changes nothing in the budget."""
    from quadratus.project_run import _budget_fields
    from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits
    limits = RunLimits(max_calls=17, max_reported_tokens=2_000_000, wall_seconds=1800,
                       max_concurrent_workers=2, reserve_tokens_per_call=200_000)
    carried = dict(calls=17 if exhausted == "calls" else 10, input_tokens=1_858_053, output_tokens=0,
                   elapsed_seconds=1800 if exhausted == "wall" else 100)
    budget = RunBudget(limits, carried=carried)
    def state():
        return {k: v for k, v in budget.snapshot().items() if k != "elapsed_seconds"}
    before = state()
    note = _budget_fields(budget)["budget_note"]
    assert "The token remainder covers the 64,000-token reservation" in note["text"]
    assert "not the run's stop or an admission decision; other limits and workflow conditions are not assessed" \
        in note["text"]
    assert "could" not in note["text"] and "admitted" not in note["text"]
    assert state() == before, "metadata only: reading the note changes nothing in the budget"
    with pytest.raises(RunBudgetExceeded, match="call_limit" if exhausted == "calls" else "wall_deadline"):
        budget.reserve(expected_tokens=64_000)
