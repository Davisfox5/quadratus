"""Blueprint stage A+B through the real controller (Davis, 2026-10-17).

The three measured failures of the controller-reset job, each replayed
below the vendor CLI with the typed records in the loop, plus the durable
store around real check runs and restarts:

- a bundled recheck no longer keeps fixed findings open: each finding
  settles by its own id, a new defect raised on a recheck opens as its own
  finding, and a basis-less concern is carried to the final audit by id;
- a NOT RUN item the lead's own sandbox could not execute is a typed
  evidence obligation routed to the harness's gate, discharged only by a
  receipt bound to the current source, gate, runner, environment and
  requirement revisions, and judged sufficient only by the audit;
- the whole remaining path is costed before a task's first call: the
  mandatory stages refuse, the correction route is recorded and shown.

Whole-run replays (tests.lifecycle.harness); every check is the real gate.
"""

import json
import shlex
import sys
from pathlib import Path

import pytest

from quadratus import checkpoint, jobstore
from quadratus.run_budget import RunLimits
from tests.lifecycle import harness as H
from tests.lifecycle.replies import judged, judging_scenarios, scenario_text
from tests.lifecycle.test_finish_capacity import DOCS_JUDGED, PLAN_1, PLAN_2, _budget, _script
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script, judging
from tests.lifecycle.test_step2_recovery import (
    SIMPLE_SHOWN,
    _episode,
    _interrupt_after,
    _simple,
    simple_proofs,
)

#: The gate names every test file whole, as an operator's check does when it
#: lists its files (the whole-file marker is root's WholeFileGateSuite, a
#: helper outside this engine path): a NOT RUN item naming a file is
#: discharged only by a run that names it as a whole operand.
GATE_FILES = (f"{shlex.quote(sys.executable)} -m pytest -q tests/test_app.py tests/test_http.py "
              "--quadratus-report={report}")
TWO_BLOCKING = ("BLOCKING: README lacks an example | basis: R2 | impact: correctness\n"
                "BLOCKING: README wording contradicts the goal | impact: contract\n"
                "R2: MET - README.md")


def _findings(replay):
    return {r["fid"]: r for r in H.result_json(replay)["review_findings"]}


def _obligations(replay):
    return H.result_json(replay)["evidence_obligations"]


def _recheck_script(replies, **kw):
    """t2's reviewer raises two blocking findings; each recheck call answers
    the next scripted reply."""
    def recheck(call, replay):
        n = len(replay.of("recheck"))
        return replies[min(n, len(replies)) - 1]
    script = _script([PLAN_1, PLAN_2], t2_review=kw.pop("t2_review", TWO_BLOCKING), **kw)
    script.overrides["recheck"] = recheck
    return script


# -- A: per-id findings ----------------------------------------------------------------------

@pytest.mark.requirements_ledger
def test_a_mixed_recheck_closes_the_fixed_finding_keeps_the_other_and_opens_the_new_one(tmp_path, monkeypatch):
    """The controller-reset shape: one reply fixes one finding, keeps one
    and raises one. Each settles alone; the kept and the new one buy the fix
    round, the second recheck closes both by id, and the run completes."""
    replay = H.run(tmp_path, monkeypatch, _recheck_script([
        "RFT2-1: RESOLVED the example is in README.md now\nRFT2-2: UNRESOLVED the wording still says sum of two\n"
        "NEW: README claims add(1, 2) is 4 | basis: README.md | impact: correctness\nNOTE: rename the heading",
        "RFT2-2: RESOLVED reworded\nRFT2-3: INVALIDATED README.md shows add(1, 2) is 3",
    ]), files=FILES, max_tasks=2, record_complete=False)
    assert replay.result.completed, replay.result.error
    findings = _findings(replay)
    assert {f: r["state"] for f, r in findings.items()} == {"RFT2-1": "resolved", "RFT2-2": "resolved",
                                                            "RFT2-3": "invalidated"}
    assert findings["RFT2-1"]["basis"] == {"kind": "requirement", "ref": "R2"}
    assert findings["RFT2-2"]["impact"] == "contract" and findings["RFT2-2"]["basis"]["kind"] == "none"
    assert findings["RFT2-3"]["basis"] == {"kind": "evidence", "ref": "README.md"} and findings["RFT2-3"]["role"] == "recheck"
    assert [h["state"] for h in findings["RFT2-2"]["history"]] == ["unresolved", "resolved"]
    assert all(h["role"] == "recheck" and h["author"] == "openai:sol" for h in findings["RFT2-2"]["history"])
    prompts = [c.prompt for c in replay.of("recheck")]
    assert "Expected ids: RFT2-1, RFT2-2." in prompts[0] and "Expected ids: RFT2-2, RFT2-3." in prompts[1]
    assert len(replay.of("revision")) == 2, "the kept and the new finding bought exactly one fix round"
    assert H.result_json(replay)["judgment_gaps"] == []


@pytest.mark.requirements_ledger
def test_a_kept_finding_blocks_acceptance_while_the_fixed_one_stays_closed(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _recheck_script([
        "RFT2-1: RESOLVED example added\nRFT2-2: UNRESOLVED still contradicts the goal",
        "RFT2-2: UNRESOLVED still contradicts the goal",
    ]), files=FILES, max_tasks=2, record_complete=False)
    # RFT2-2 names no basis and claims no immediate risk, so it does not stop
    # the run: it is carried to the audit by id, nobody closes it there, and
    # the requirement it touches is not met. RFT2-1 stays closed throughout.
    assert not replay.result.completed and replay.result.error.startswith("RequirementsUnmet")
    assert "RFT2-2" in replay.result.error and "RFT2-1" not in replay.result.error
    findings = _findings(replay)
    assert findings["RFT2-1"]["state"] == "resolved" and findings["RFT2-2"]["state"] == "unresolved"
    assert findings["RFT2-2"]["audit_carried"]
    audit = H.result_json(replay)["requirements"]["audits"][-1]
    assert audit["findings_carried"] == ["RFT2-2"] and audit["verdicts"]["R2"]["met"] is False


@pytest.mark.requirements_ledger
def test_a_duplicate_or_missing_line_settles_nothing_and_is_not_a_fix_round(tmp_path, monkeypatch):
    """R24's shape per id: a reply that judges one finding twice and the
    other not at all. Neither is resolved, neither buys a fix round, both
    are recorded as not re-checked by id, and the run stops on them."""
    replay = H.run(tmp_path, monkeypatch, _recheck_script([
        "RFT2-1: RESOLVED\nRFT2-1: RESOLVED again\nI believe RFT2-2 is fine as well.",
    ]), files=FILES, max_tasks=2, record_complete=False)
    assert not replay.result.completed and replay.result.error.startswith("FindingsOpen")
    findings = _findings(replay)
    assert findings["RFT2-1"]["history"][-1]["state"] == "unjudged" and "duplicate" in findings["RFT2-1"]["history"][-1]["why"]
    assert findings["RFT2-2"]["history"][-1]["why"] == "no line judged it"
    assert len(replay.of("revision")) == 1, "no fix round was bought by a reply that judged nothing validly"
    gaps = H.result_json(replay)["judgment_gaps"]
    assert gaps and gaps[0]["kind"] == "recheck" and "RFT2-1" in gaps[0]["why"] and "RFT2-2" in gaps[0]["why"]


@pytest.mark.requirements_ledger
def test_a_basis_less_concern_is_carried_to_the_audit_by_id_and_judged_there(tmp_path, monkeypatch):
    """A concern with no basis and no immediate risk does not stop the run:
    it reaches the final audit by id. The audit invalidates it on evidence
    and the run completes; an audit that leaves it unjudged leaves the
    requirement unmet."""
    review = "BLOCKING: the example might mislead users | impact: other\nR2: MET - README.md"
    script = _recheck_script(["RFT2-1: UNRESOLVED it still might"], t2_review=review)
    script.overrides["auditor"] = judging_scenarios(judging(lambda call, replay: (
        "R1: MET - app.py\nR2: MET - README.md\nRFT2-1: INVALIDATED README.md shows add(1, 2) returning 3"),
        reading=DOCS_JUDGED), *simple_proofs("R1"), basis=SIMPLE_SHOWN)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert replay.result.completed, replay.result.error
    audit = replay.of("auditor")[-1].prompt
    assert "Carried to you by id" in audit and "RFT2-1 (t2, raised by openai:sol, impact other)" in audit
    finding = _findings(replay)["RFT2-1"]
    assert finding["state"] == "invalidated" and finding["history"][-1]["role"] == "audit"
    assert finding["history"][-1]["author"] == "openai:sol"


@pytest.mark.requirements_ledger
def test_an_unjudged_carried_concern_leaves_its_requirement_unmet(tmp_path, monkeypatch):
    review = "BLOCKING: the example might mislead users | impact: other\nR2: MET - README.md"
    script = _recheck_script(["RFT2-1: UNRESOLVED it still might"], t2_review=review)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert not replay.result.completed
    audit = H.result_json(replay)["requirements"]["audits"][-1]
    assert audit["verdicts"]["R2"]["met"] is False and "RFT2-1" in audit["verdicts"]["R2"]["why"]
    assert _findings(replay)["RFT2-1"]["state"] == "unresolved"


@pytest.mark.requirements_ledger
def test_an_immediate_risk_holds_the_close_whatever_its_basis(tmp_path, monkeypatch):
    review = "BLOCKING: the import drops rows on a retry | impact: data-loss\nR2: MET - README.md"
    replay = H.run(tmp_path, monkeypatch, _recheck_script(["RFT2-1: UNRESOLVED still drops rows"], t2_review=review),
                   files=FILES, max_tasks=2, record_complete=False)
    # Held in the last slot, the finding goes to the cap's completion
    # decision (close family, 2026-10-26); the requirements check refuses
    # MET over it, and no judge but its own can close it.
    assert not replay.result.completed and replay.result.error.startswith("RequirementsUnmet")
    assert "RFT2-1" in replay.result.error
    finding = _findings(replay)["RFT2-1"]
    assert finding["holds_effects"] and not finding["audit_carried"] and finding["basis"]["kind"] == "none"
    assert finding["state"] == "unresolved" and not finding["closed"]


@pytest.mark.requirements_ledger
def test_a_post_merge_recheck_settles_each_deferred_finding_by_id(tmp_path, monkeypatch):
    """Two parallel children; t2's reviewer leaves one finding open in its
    copy. After the merge the reviewer settles it by id; a second reply form
    (the old bare RESOLVED) still reads for the one finding asked about."""
    batch = ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\nPARALLEL\n"
             + _simple("app.py", "Implement add in app.py.", "R1") + "\n---\n"
             + _simple("README.md", "Document add in README.md.", "R2"))

    def recheck(call, replay):
        if "now merged" in call.prompt or "merged source" in call.prompt:
            return "RFT2-1: RESOLVED the merged README has the example"
        return "RFT2-1: UNRESOLVED no example yet"
    script = _script([batch], t2_review="BLOCKING: README lacks an example | basis: R2 | impact: correctness\n"
                                        "R2: MET - README.md")
    script.overrides["recheck"] = recheck
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert replay.result.completed, replay.result.error
    finding = _findings(replay)["RFT2-1"]
    assert [h["state"] for h in finding["history"]] == ["unresolved", "unresolved", "resolved"]
    batches = H.result_json(replay)["parallel_batches"]
    rechecks = batches[-1]["post_merge_rechecks"][0]["rechecked"]
    assert rechecks[0]["resolved"] == ["RFT2-1"] and rechecks[0]["verdict"] == "resolved"


# -- B: evidence obligations through the real gate ---------------------------------------------

HTTP_TEST = '''import http.client
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from app import add


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = str(add(1, 2)).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_real_http_server_answers_add():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/add")
        response = conn.getresponse()
        assert response.status == 200 and response.read() == b"3"
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
'''
HTTP_FILES = {**FILES, "tests/test_http.py": HTTP_TEST}
HTTP_SHOWN = ("the project check executed tests/test_app.py::test_add and tests/test_http.py::"
              "test_real_http_server_answers_add on the delivered source, which is this scenario")


def _http_script(not_run_item):
    plan = ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: the HTTP server answers /add with the sum\n"
            + _simple("app.py", "Implement add in app.py so the HTTP test passes.", "R1, R2"))

    def orchestrator(call, replay):
        return plan if len(replay.of("orchestrator")) == 1 else "DONE"

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return (f'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\n'
                f'PROVES: P2 - tests/test_http.py::test_real_http_server_answers_add\n'
                f'NOT RUN: {not_run_item}\nCHANGED: ["app.py"]')
    item = not_run_item.split(" - ", 1)[0].strip()

    def auditor(call, replay):
        # This fixture's own judgments: its two declared scenarios and the
        # NOT RUN item it scripted, each only where the prompt lists it
        # executed (replies.judged).
        return judged(call.prompt, {"R1": "MET - app.py", "R2": "MET - tests/test_http.py"},
                      approve={**{scenario_text(p): HTTP_SHOWN for p in simple_proofs("R1, R2")},
                               item: "the gate ran tests/test_http.py whole on the delivered source, which is the "
                                     "item the lead could not run"})
    return Script(orchestrator=orchestrator, lead=lead,
                  collaborator=lambda call, replay: "NO FINDINGS\nR1: MET - app.py\nR2: MET - tests/test_http.py",
                  auditor=auditor, **{"requirements-review": lambda call, replay: "COMPLETE"})


@pytest.mark.requirements_ledger
def test_a_not_run_http_test_is_an_obligation_the_gate_executes_and_the_audit_judges(tmp_path, monkeypatch):
    """The lead's sandbox could not bind a port and said so. The obligation
    names the gate as its runner; the real gate runs the real local HTTP
    server test whole; the receipt binds source, gate, runner, environment
    and requirement revisions; the audit's MET on R2 is the sufficiency
    judgment, by the auditor, bound to exactly that receipt."""
    replay = H.run(tmp_path, monkeypatch, _http_script("tests/test_http.py - the sandbox denied binding 127.0.0.1"),
                   files=HTTP_FILES, check=GATE_FILES, max_tasks=1, lead_runs_commands=False, record_complete=False)
    assert replay.result.completed, replay.result.error
    rows = {o["kind"]: o for o in _obligations(replay)}
    typed = rows["not_run"]
    assert typed["runner"] == "gate" and typed["execution"] == "ran" and typed["receipts"]
    assert typed["sufficiency"] == "sufficient" and typed["judged_by"] == "openai:sol"
    assert typed["established"] and typed["sufficiency_current"]
    assert [list(r) for r in typed["requirements"]] == [["R1", 1], ["R2", 1]]
    assert typed["source_digest"] == H.result_json(replay)["checks"][-1]["source"]
    audit = replay.of("auditor")[-1].prompt
    assert "Evidence obligations" in audit and f"{typed['oid']} (t1; R1, R2)" in audit and "runner gate" in audit
    # The real gate ran the real server: its receipt names the test file whole.
    last = H.result_json(replay)["checks"][-1]
    assert last["passed"] and any("test_http.py" in r.get("command", "") for r in last.get("receipts", []))


@pytest.mark.requirements_ledger
def test_a_not_run_item_no_runner_can_execute_is_a_reported_prerequisite_and_the_audit_stays_lowered(tmp_path,
                                                                                                        monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _http_script("real HTTP server acceptance - the sandbox denied binding"),
                   files=HTTP_FILES, check=GATE_FILES, max_tasks=1, lead_runs_commands=False, record_complete=False)
    assert not replay.result.completed
    typed = next(o for o in _obligations(replay) if o["kind"] == "not_run")
    assert typed["runner"] is None and typed["execution"] == "missing" and not typed["executable"]
    audit = H.result_json(replay)["requirements"]["audits"][-1]
    assert audit["verdicts"]["R2"]["met"] is False and "reported NOT RUN" in audit["verdicts"]["R2"]["why"]
    assert "NONE (prerequisite missing)" in replay.of("auditor")[-1].prompt


@pytest.mark.requirements_ledger
def test_a_receipt_on_another_source_does_not_discharge_and_the_declared_acceptance_binds_to_the_task_check(
        tmp_path, monkeypatch):
    """Two serial tasks. t1's declared scenario is bound by its lead to the
    case the task's check names as passed and is discharged by that check;
    after t2 edits README.md the source moves, so the obligation is stale
    until the gate runs on the new source, and the final record shows it
    executed on the final source. t2's scenario names README.md, which no
    test case shows: its lead binds nothing, and no green check discharges
    it (proof/1: a scenario is discharged only by its own named executed
    case, and a documentation scenario with no case stays honestly
    unexecuted)."""
    # The shared lead already binds P1 to test_add; a second identical line
    # would be an invalid (duplicate) declaration under the strict protocol.
    script = _script([PLAN_1, PLAN_2], t2_review="NO FINDINGS\nR2: MET - README.md")
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert replay.result.completed, replay.result.error
    final = H.result_json(replay)["source_fingerprint"]
    rows = {o["task"]: o for o in _obligations(replay)}
    assert {o["kind"] for o in rows.values()} == {"acceptance"} and len(rows) == 2
    t1, t2 = rows["t1"], rows["t2"]
    assert t1["binding"] == "bound" and t1["cases"] == ["tests/test_app.py::test_add"]
    assert t1["execution"] == "ran" and t1["source_digest"] == final and t1["established"]
    assert t2["binding"].startswith("reading claim") and t2["execution"] == "missing" and t2["cases"] == []
    assert t2["runner"] is None and not t2["executable_claim"]
    # Established only by the audit's own explicit per-obligation line on
    # the final source (this file's scripted audit writes one); never by a
    # check, and never by the MET on R2 alone.
    assert t2["sufficiency"] == "sufficient" and t2["judged_source"] == final and t2["established"]
    assert H.result_json(replay)["requirements"]["audits"][-1]["acceptance"]["R2"]["met"] is True
    assert all(o["gate"] and o["environment"] and o["policy"] == "review/2" for o in rows.values())


# -- C: the remaining path is costed before the first call -------------------------------------------

def _token_limits(total):
    return RunLimits(max_calls=20, max_reported_tokens=total, wall_seconds=600, max_concurrent_workers=2,
                     reserve_tokens_per_call=200_000, max_tokens_per_call=500_000)


@pytest.mark.requirements_ledger
def test_a_route_the_tokens_cannot_pay_refuses_the_task_before_its_first_call_and_a_payable_one_runs(
        tmp_path, monkeypatch):
    """The controller-reset numbers in miniature. With 1.5M total, t1's
    route (one editing call at the 500k ceiling, one review at the 200k
    reserve, the planning call that follows at the 200k reserve since
    2026-10-28: 900k, and no close-out since 2026-10-21) fits the 1,099,780
    left after the two 200k holds and the 220 of intake, so t1 is admitted
    whole and runs; no terminal route is recorded, nothing is relinquished
    (root's admission finding on f4fe5b7: a route merely recorded was
    still started, and ended short of its judgments; a route is started
    only as admitted). The revision and the recheck are not on the route:
    a correction is admitted when a finding selects it. With 2.1M total
    both tasks' routes fit and the run completes. With 1.0M total the
    mandatory stages themselves do not fit: the shortfall names them."""
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2], t2_review="NO FINDINGS\nR2: MET - README.md"),
                   files=FILES, max_tasks=2, record_complete=False, limits=_token_limits(1_500_000))
    result = H.result_json(replay)
    admissions = [a for a in result["plan_admissions"] if a["task"] == "t1"]
    assert [(a["scope"], a["feasible"]) for a in admissions] == [("mandatory", True), ("full", True)]
    assert admissions[1]["stages"] == ["lead draft(editing/write)", "review by claude:opus(reserve)",
                                       "next planning call(reserve)"], "a slot follows t1"
    assert "close-out" not in json.dumps(result["plan_admissions"])
    assert [(c.task, c.role) for c in replay.calls if c.task == "t1"] == [("t1", "lead"), ("t1", "collaborator")]
    assert result["terminal_route"] is None and result["not_started"] == [] and not replay.of("closeout")
    assert "t1" in [json.loads(t)["task"] for t in replay.artifact_texts("factual-closeout")]
    assert (replay.project / "app.py").read_text() == FIXED, "t1 wrote"
    assert "committed to end after" not in (replay.result.error or "")
    assert "goal" in _budget(replay)["finish_protection"]["started"]

    (tmp_path / "paid").mkdir()
    paid = H.run(tmp_path / "paid", monkeypatch, _script([PLAN_1, PLAN_2], t2_review="NO FINDINGS\nR2: MET - README.md"),
                 files=FILES, max_tasks=2, record_complete=False, limits=_token_limits(2_100_000))
    assert paid.result.completed, paid.result.error
    rows = {(a["task"], a["scope"]): a for a in H.result_json(paid)["plan_admissions"]}
    assert rows[("t1", "full")]["feasible"] and rows[("t2", "full")]["feasible"]
    assert rows[("t2", "full")]["stages"] == ["lead draft(editing/write)", "review by openai:sol(reserve)"]
    assert "Plan admission for" not in paid.of("auditor")[-1].prompt, "an admitted route is not a note"

    # With 1.0M total the 600k left after the two holds cannot pay even the
    # first task's mandatory stages (one editing call at the 500k ceiling
    # and one review at the 200k reserve): t1 is not started before any
    # call of its own, the shortfall named in tokens, and nothing is written.
    (tmp_path / "short").mkdir()
    short = H.run(tmp_path / "short", monkeypatch, _script([PLAN_1, PLAN_2], t2_review="NO FINDINGS\nR2: MET - README.md"),
                  files=FILES, max_tasks=2, record_complete=False, limits=_token_limits(1_000_000))
    assert not [c for c in short.calls if c.task in ("t1", "t2")]
    result = H.result_json(short)
    assert [n["task"] for n in result["not_started"]] == ["t1"]
    admission = [a for a in result["plan_admissions"] if a["task"] == "t1"][-1]
    assert admission["scope"] == "mandatory" and not admission["feasible"] and admission["short"].get("tokens")
    assert (short.project / "app.py").read_text() == FILES["app.py"], "nothing was written"
    assert _budget(short)["stop_reason"] == ""


# -- D: the durable store around real effects and a restart ---------------------------------------

@pytest.mark.requirements_ledger
def test_every_check_is_a_settled_operation_in_the_committed_generation(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2], t2_review="NO FINDINGS\nR2: MET - README.md"),
                   files=FILES, max_tasks=2, record_complete=False)
    assert replay.result.completed, replay.result.error
    run_dir = Path(replay.result.run_dir)
    manifest = jobstore.read_manifest(run_dir)
    assert manifest is not None and manifest.clean and {"budget", "checkpoint", "authority"} <= set(manifest.records)
    assert any(name.startswith("operations-") for name in manifest.records), "the session's own effects are committed"
    result = H.result_json(replay)
    assert 0 < result["job_store"]["generation"] <= manifest.number and result["job_store"]["refused"] is None
    ops = result["operations"]
    assert ops and all(o["status"] == "settled" and o["result_digest"] for o in ops)
    assert [o["purpose"] for o in ops].count("check") == len(result["checks"])
    budget, problem = jobstore.read_budget(run_dir)
    assert problem is None and not budget["operations"]["started"]
    assert len(budget["operations"]["settled"]) == budget["reserved_attempts"]
    assert checkpoint.load(run_dir)["sealed"] is True


def test_an_interrupted_run_resumes_from_the_committed_generation_and_an_unsettled_effect_refuses(tmp_path, monkeypatch):
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1, DECL_T2
    from tests.lifecycle.test_step2_recovery import _plan
    _interrupt_after(monkeypatch, 1)
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1, DECL_T2)), files=FILES, max_tasks=2,
                   record_complete=False)
    assert "KeyboardInterrupt" in replay.result.error
    first = Path(replay.result.run_dir)
    manifest = jobstore.read_manifest(first)
    assert manifest.clean and "checkpoint" in manifest.records
    # The export is a copy: a stale export changes nothing the resume reads.
    (first / "budget.json").write_text(json.dumps(dict(jobstore.read_budget(first)[0], in_flight=3)))
    assert jobstore.read_budget(first)[0]["in_flight"] == 0
    resumed = _episode(replay, resume_from=first)
    assert resumed.completed, resumed.error
    assert json.loads((Path(resumed.run_dir) / "lineage.json").read_text())["episode"] == 2

    # A second parent whose committed generation holds a started effect.
    _interrupt_after(monkeypatch, 1)
    (tmp_path / "again").mkdir()
    again = H.run(tmp_path / "again", monkeypatch, Script(orchestrator=_plan(DECL_T1, DECL_T2)), files=FILES,
                  max_tasks=2, record_complete=False)
    parent = Path(again.result.run_dir)
    store = jobstore.JobStore(parent)
    store.publish({"operations-x": [dict(opid="OPx-9", purpose="check", status="started", owner_id="x")]},
                  session_unsettled=("x", ["OPx-9"]))
    with pytest.raises(checkpoint.CheckpointRefused, match="never settled"):
        _episode(again, resume_from=parent)
