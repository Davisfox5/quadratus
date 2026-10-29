"""The evidence lifecycle batch (Davis, 2026-10-29) through the real
controller with scripted replies: a lead's NOT RUN report bound to a case
or scenario identity and discharged by the harness's own gate executing
it on the delivered source; an unbound, mismatched or partial report held;
sufficiency read from the auditor's own verdicts so a NOT RUN on one
requirement never lowers a sibling the auditor passed; a requirement that
names a command cited by the harness's exact receipt and never by a
model's word; the finish withholding a challenge that cannot clear
execution debt; and the independent record checker validating the saved
binding itself. Every claim here is a record the harness wrote."""
import importlib.util
import json
from pathlib import Path

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script, covered_met
from tests.lifecycle.test_proof_binding import CASE, _at, _plan, _proofs, _scope, _script
from tests.lifecycle.test_step2_evidence import _CHALLENGE, _ratio_run

pytestmark = pytest.mark.requirements_ledger

_SPEC = importlib.util.spec_from_file_location(
    "truth_check", Path(__file__).resolve().parents[2] / "tools" / "truth_check.py")
truth = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(truth)

NOT_RUN_WHY = "the sandbox denies sockets"


def _lead_not_run(item, *, proves=(CASE,), extra_files=None):
    def lead(call, replay):
        files = {"app.py": FIXED, **(extra_files or {})}
        H.write(call, files)
        built = "".join(f"\nPROVES: P1 - {case}" for case in proves)
        return f"Implemented add.{built}\nNOT RUN: {item} - {NOT_RUN_WHY}\nCHANGED: {json.dumps(sorted(files))}"
    return lead


def _not_run_rows(replay, task="t1"):
    return [e for e in H.result_json(replay)["unexecuted_acceptance"] if e["task"] == task]


def _not_run_obligations(replay, task="t1"):
    return [o for o in H.result_json(replay)["evidence_obligations"] if o["task"] == task and o["kind"] == "not_run"]


# -- 1. a NOT RUN report bound to an identity is discharged by the gate executing it ----------------

@pytest.mark.parametrize("item, kind", [(CASE, "case"), ("P1", "scenario"), ("test_add", "case")])
def test_a_not_run_report_the_gate_then_executes_whole_is_discharged_and_the_requirement_stands(
        tmp_path, monkeypatch, item, kind):
    replay = H.run(tmp_path, monkeypatch, _script(_plan(_scope(proofs=_proofs("R1"))), _lead_not_run(item)),
                   files=FILES, max_tasks=1, record_complete=False)
    assert replay.result.completed, replay.result.error
    [row] = _not_run_rows(replay)
    assert row["item"] == item and row["why"] == NOT_RUN_WHY, "the report is kept as written"
    assert row["binding"]["kind"] == kind and row["cases"] == [CASE] and row["binding"]["problem"] == ""
    assert row["gate"] and row["environment"], "the identity a discharging check must match is on the record"
    [obligation] = _not_run_obligations(replay)
    assert obligation["runner"] == "gate" and obligation["cases"] == [CASE] and obligation["execution"] == "ran"
    assert obligation["receipts"], "discharged by a named harness receipt"
    result = H.result_json(replay)
    assert result["checks"][-1]["passed"] and result["checks"][-1]["source"] == result["source_fingerprint"]
    assert CASE in (result["checks"][-1]["report"] or {}).get("passed_cases", [])
    audit = result["requirements"]["audits"][-1]
    assert audit["lowered"] == [] and audit["verdicts"]["R1"]["met"] is True
    assert "reported NOT RUN" not in replay.of("auditor")[-1].prompt, "a discharged item is no standing debt"
    assert truth.judge(replay.result.run_dir, replay.project)["verdict"] == truth.VERIFIED, \
        "the independent checker validates the saved binding itself"


@pytest.mark.parametrize("item, problem", [
    ("tests/test_app.py::test_absent", "is not defined where it says"),
    ("P2", "P2 is not a scenario this task declares"),
    ("P1 and P2", "P2 is not a scenario this task declares"),
    ("P10", "P10 is not a scenario this task declares"),
    ("TestNope.test_add", "is not defined in any test file"),
    ("tests/test_app.py::test_add_more", "is not defined where it says"),
    ("the browser scenario", "names no scenario, case or path"),
])
def test_a_not_run_report_bound_to_nothing_is_held_and_the_finish_withholds_the_challenge(
        tmp_path, monkeypatch, item, problem):
    replay = H.run(tmp_path, monkeypatch, _script(_plan(_scope(proofs=_proofs("R1"))), _lead_not_run(item)),
                   files=FILES, max_tasks=1, record_complete=False)
    assert not replay.result.completed
    [row] = _not_run_rows(replay)
    assert row["cases"] == [] and problem in row["binding"]["problem"], row["binding"]
    [obligation] = _not_run_obligations(replay)
    assert obligation["runner"] is None and obligation["execution"] != "ran", "a prerequisite, never a pass"
    result = H.result_json(replay)
    assert result["checks"][-1]["passed"], "the green gate discharged nothing"
    audit = result["requirements"]["audits"][-1]
    assert audit["verdicts"]["R1"]["met"] is False
    assert any("reported NOT RUN by the covering task" in r["reason"] for r in audit["lowered"]), audit["lowered"]
    assert "[not bound to any case: " in replay.of("auditor")[-1].prompt
    routes = [r for r in result["finish_routing"] if r["requirement"] == "R1"]
    assert routes and all(r["kind"] == "execution debt" and r["challengeable"] is False for r in routes), routes
    assert any(r["site"] == "final challenge" for r in routes)
    assert not any(_CHALLENGE in c.prompt for c in replay.of("orchestrator")), "no challenge can clear it"
    verdict = truth.judge(replay.result.run_dir, replay.project)
    assert verdict["verdict"] != truth.VERIFIED
    assert any("reported NOT RUN" in r for r in verdict["unverified"]), verdict


def test_a_genuine_dispute_is_still_offered_the_challenge(tmp_path, monkeypatch):
    """The offer, not the outcome: whether the served proof runs needs a
    scratch sandbox, which tests/lifecycle/test_step2_evidence.py covers
    where one exists."""
    replay = _ratio_run(tmp_path, monkeypatch)
    routes = [r for r in H.result_json(replay)["finish_routing"] if r["requirement"] == "R1"]
    assert routes and routes[0]["kind"] == "finding" and routes[0]["challengeable"] is True
    assert "a false positive is legitimate" in routes[0]["route"]
    assert any(_CHALLENGE in c.prompt for c in replay.of("orchestrator"))


# -- 2. the independent checker validates the saved binding, never an engine mark -------------------

def _tampered(replay, mutate):
    record = json.loads(Path(replay.result.run_dir, "result.json").read_text())
    [entry] = [e for e in record["unexecuted_acceptance"] if e["task"] == "t1"]
    mutate(record, entry)
    return truth._not_run_cleared(entry, record["checks"], record)


def test_the_checker_rejects_forged_stale_or_mismatched_bindings(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(_plan(_scope(proofs=_proofs("R1"))), _lead_not_run("P1")),
                   files=FILES, max_tasks=1, record_complete=False)
    assert replay.result.completed, replay.result.error
    assert _tampered(replay, lambda record, entry: None), "the genuine record clears"
    rejected = {
        "other case": lambda record, entry: entry.__setitem__("cases", ["tests/test_app.py::test_other"]),
        "suffix case": lambda record, entry: entry.__setitem__("cases", ["tests/test_app.py::test_add_more"]),
        "scenario row of another task": lambda record, entry: entry.__setitem__("task", "t9"),
        "scenario the item does not name": lambda record, entry: entry.__setitem__("item", "P2"),
        "extra scenario the row does not declare": lambda record, entry: entry.__setitem__("item", "P1 and P2"),
        "stale source": lambda record, entry: record.__setitem__("source_fingerprint", "0" * 24),
        "another gate": lambda record, entry: entry.__setitem__("gate", "other"),
        "another environment": lambda record, entry: entry.__setitem__("environment", "elsewhere"),
        "report after the check": lambda record, entry: entry.__setitem__("after_check", len(record["checks"])),
        "no binding": lambda record, entry: entry.__setitem__("binding", None),
        "malformed case id": lambda record, entry: entry.__setitem__("cases", ["test_add"]),
        "empty cases": lambda record, entry: entry.__setitem__("cases", []),
        "acceptance row cases differ": lambda record, entry: [o.__setitem__("cases", ["tests/test_app.py::test_x"])
                                                               for o in record["evidence_obligations"]
                                                               if o.get("kind") == "acceptance"],
        "the check did not pass": lambda record, entry: [c.__setitem__("passed", False) for c in record["checks"]],
        "a skip in the run": lambda record, entry: [c["report"]["counts"].__setitem__("skipped", 1)
                                                    for c in record["checks"] if c.get("report")],
        "unknown counts": lambda record, entry: [c.__setitem__("report", None) for c in record["checks"]],
        "filtered command": lambda record, entry: [c.__setitem__("command", c["command"] + " -k add")
                                                   for c in record["checks"]],
        "engine mark alone": lambda record, entry: (entry.__setitem__("cases", ["tests/test_app.py::test_other"]),
                                                    entry.__setitem__("discharged_at", 0)),
    }
    for name, mutate in rejected.items():
        assert not _tampered(replay, mutate), name


# -- 3. sufficiency is the auditor's own, never a flag lowered for another record -----------------

def test_a_not_run_on_one_requirement_never_lowers_a_sibling_the_auditor_passed(tmp_path, monkeypatch):
    """finding-lifecycle-02: O4 bound R3 and R9; R9 was lowered for a NOT
    RUN item and the implied sufficiency then lowered R3, which the auditor
    had passed. Here t1's one scenario binds R1 and R2, t2 covers R2 and
    reports a NOT RUN it binds to nothing: R2 is lowered, R1 stands, and
    the shared row is sufficient on the auditor's own two MET lines."""
    requirements = "R1: add(a, b) returns the sum\nR2: add handles two numbers"
    shared = [dict(id="P1", requirements=["R1", "R2"], operation="add(1, 2)", state="nothing stored",
                   expect="3 is returned", location="tests/test_app.py")]
    plans = [_plan(_scope(proofs=shared), "R1, R2", requirements),
             "KIND: backend simple\nSCOPE: " + json.dumps(_scope(proofs=_proofs("R2"))) + "\nTidy add in app.py.\nCOVERS: R2"]

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return plans[n - 1] if n <= 2 else "DONE"

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'
        H.write(call, {"app.py": FIXED + "\n\n# tidy\n"})
        return f'Tidied add.\nPROVES: P1 - {CASE}\nNOT RUN: tests/test_app.py::test_absent - {NOT_RUN_WHY}\nCHANGED: ["app.py"]'

    script = Script(orchestrator=orchestrator, lead=lead,
                    collaborator=lambda c, r: "NO FINDINGS\n" + covered_met(c.prompt),
                    auditor=lambda c, r: f"R1: MET - {CASE}\nR2: MET - {CASE}",
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert not replay.result.completed
    result = H.result_json(replay)
    audit = result["requirements"]["audits"][-1]
    assert audit["verdicts"]["R1"]["met"] is True, audit
    assert audit["verdicts"]["R2"]["met"] is False and [r["requirement"] for r in audit["lowered"]] == ["R2"], audit
    assert "reported NOT RUN by the covering task: t2" in audit["verdicts"]["R2"]["why"]
    [shared_row] = [o for o in result["evidence_obligations"] if o["task"] == "t1" and o["kind"] == "acceptance"]
    assert sorted(r[0] for r in shared_row["requirements"]) == ["R1", "R2"]
    assert shared_row["execution"] == "ran" and shared_row["sufficiency"] == "sufficient"
    assert shared_row["sufficiency_source"] == "implied from the auditor's own verdicts on R1 MET, R2 MET"
    assert result["requirements"]["status"]["R1"].lower().startswith("met")


# -- 4. a command the requirement names is cited by the harness's exact receipt ------------------

_RUN_REQ = "R2: the test suite is run with `python3 -m pytest -q` before completion is declared"


def _two_requirement_plan(second=_RUN_REQ, proves_p2=CASE):
    requirements = "R1: add(a, b) returns the sum\n" + second
    plan = _plan(_scope(proofs=_proofs("R1", "R2")), "R1, R2", requirements)

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nPROVES: P2 - {proves_p2}\nCHANGED: ["app.py"]'
    return plan, lead


def _command_script(plan, lead):
    return Script(orchestrator=lambda c, r: plan if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                  collaborator=lambda c, r: "NO FINDINGS\n" + covered_met(c.prompt),
                  auditor=lambda c, r: f"R1: MET - {CASE}\nR2: MET - the harness ran the suite before completion",
                  **{"requirements-review": lambda c, r: "COMPLETE"})


def test_a_command_requirement_is_met_on_the_harness_s_own_exact_receipt(tmp_path, monkeypatch):
    plan, lead = _two_requirement_plan()
    replay = H.run(tmp_path, monkeypatch, _command_script(plan, lead), files=FILES, max_tasks=1, record_complete=False)
    assert replay.result.completed, replay.result.error
    audit = H.result_json(replay)["requirements"]["audits"][-1]
    assert audit["verdicts"]["R2"]["met"] is True and audit["lowered"] == []
    receipt = audit["citation_receipts"]["R2"]
    assert receipt["id"].startswith("check-") and receipt["source"] == H.result_json(replay)["source_fingerprint"]
    assert "-m pytest -q" in receipt["command"] and "{report}" not in receipt["command"]
    assert "harness receipt check-" in audit["verdicts"]["R2"]["why"]


def test_a_command_the_harness_never_ran_is_not_cited_and_a_receipt_never_stands_in_for_behaviour(tmp_path,
                                                                                                    monkeypatch):
    plan, lead = _two_requirement_plan("R2: the suite is run with `python3 -m unittest discover -s tests` before completion")
    other = H.run(_at(tmp_path, "other"), monkeypatch, _command_script(plan, lead), files=FILES, max_tasks=1,
                  record_complete=False)
    assert not other.result.completed
    audit = H.result_json(other)["requirements"]["audits"][-1]
    assert audit["verdicts"]["R2"]["met"] is False and audit["citation_receipts"] == {}
    why = audit["verdicts"]["R2"]["why"]
    assert "MET claimed without a file or test that exists in the project" in why
    assert "the harness has no receipt for `python3 -m unittest discover -s tests`" in why
    assert "pytest" not in why, "never the harness's own command"

    plan, lead = _two_requirement_plan(proves_p2="tests/test_app.py::test_absent")
    unshown = H.run(_at(tmp_path, "unshown"), monkeypatch, _command_script(plan, lead), files=FILES, max_tasks=1,
                    record_complete=False)
    assert not unshown.result.completed
    audit = H.result_json(unshown)["requirements"]["audits"][-1]
    assert "R2" in audit["citation_receipts"], "the receipt is a citation"
    assert audit["verdicts"]["R2"]["met"] is False
    assert any(r["requirement"] == "R2" and "evidence obligation not executed" in r["reason"] for r in audit["lowered"]), \
        "and never a substitute for the behaviour the scenario claims"


# -- 5. a parallel child's report comes back bound and the merge gate discharges it ----------------

def _block(kind, path, text, covers, location="tests/test_app.py"):
    scope = json.dumps({"permitted_paths": [path], "intended_result": f"{path} written",
                        "acceptance": [f"{path} meets the task"], "max_lines": 20,
                        "proofs": H.proofs_for(covers, location)})
    return f"KIND: {kind}\nSCOPE: {scope}\n{text}\nCOVERS: {covers}"


def test_a_parallel_child_s_bound_not_run_is_discharged_by_the_parent_s_merge_gate(tmp_path, monkeypatch):
    plan = ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\nPARALLEL\n"
            + _block("architect complex", "app.py", "Implement add in app.py.", "R1") + "\n---\n"
            + _block("backend simple", "README.md", "Document add in README.md.", "R2"))

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return f'Implemented add.\nPROVES: P1 - {CASE}\nNOT RUN: P1 - {NOT_RUN_WHY}\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\nadd(a, b) returns the sum.\n"})
        return f'Documented add.\nPROVES: P1 - {CASE}\nCHANGED: ["README.md"]'

    script = Script(orchestrator=lambda c, r: plan if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                    collaborator=lambda c, r: "NO FINDINGS\n" + covered_met(c.prompt),
                    auditor=lambda c, r: f"R1: MET - {CASE}\nR2: MET - README.md",
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    assert replay.result.completed, replay.result.error
    [row] = _not_run_rows(replay)
    assert row["binding"]["kind"] == "scenario" and row["cases"] == [CASE]
    [obligation] = _not_run_obligations(replay)
    assert obligation["runner"] == "gate" and obligation["cases"] == [CASE] and obligation["execution"] == "ran"
    result = H.result_json(replay)
    assert result["checks"][-1]["source"] == result["source_fingerprint"]
    assert result["requirements"]["audits"][-1]["lowered"] == []
