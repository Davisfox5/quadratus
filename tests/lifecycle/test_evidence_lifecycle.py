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
import sys
from pathlib import Path

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import judged
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script, covered_met
from tests.lifecycle.test_proof_binding import CASE, _at, _plan, _proofs, _scope
from tests.lifecycle.test_step2_evidence import _CHALLENGE, _ratio_run

pytestmark = pytest.mark.requirements_ledger

_SPEC = importlib.util.spec_from_file_location(
    "truth_check", Path(__file__).resolve().parents[2] / "tools" / "truth_check.py")
truth = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(truth)

NOT_RUN_WHY = "the sandbox denies sockets"
#: The proof_binding fixture's declared scenario (_proofs("R1")), as the
#: obligations block renders it: the row these cases judge by name.
ADD_SCENARIO = "P1: add is called for R1; given two tickets exist; expect the sum is returned; in tests/test_app.py"
SHOWN = "tests/test_app.py::test_add executed on the delivered source and returned the sum, exactly this scenario"


def _auditor(*not_run_items, verdicts=None):
    """A case-authored audit: MET on R1 citing the executed case, this
    fixture's own SUFFICIENT line for the add scenario, and one for each
    NOT RUN item the case expects the gate to have executed (its row is
    listed executed only then; an item still held has no executed row and
    gets no line). Nothing else is judged."""
    def auditor(call, replay):
        approve = {ADD_SCENARIO: SHOWN}
        for item in not_run_items:
            approve[item] = f"the gate executed {CASE} whole on the delivered source, which is what the item names"
        return judged(call.prompt, verdicts or {"R1": f"MET - {CASE}"}, approve=approve)
    return auditor


def _script(plan, lead, **kw):
    return Script(orchestrator=lambda c, r: plan if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                  collaborator=kw.pop("collaborator", lambda c, r: "NO FINDINGS\nR1: MET - app.py"),
                  auditor=kw.pop("auditor", _auditor()),
                  **{"requirements-review": lambda c, r: "COMPLETE"}, **kw)


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

#: A second configured command beside the harness gate makes the production
#: shape (GateSuite: aggregate command 'gate suite', report None, the
#: evidence in per-command receipts), which the larger runs use.
SUITE_EXTRA = [[sys.executable, "-m", "pytest", "-q", "tests/test_app.py", "--quadratus-report={report}"]]


@pytest.mark.parametrize("item, kind", [(CASE, "case"), ("P1", "scenario"), ("test_add", "case")])
@pytest.mark.parametrize("suite", [False, True], ids=["direct", "suite"])
def test_a_not_run_report_the_gate_then_executes_whole_is_discharged_and_the_requirement_stands(
        tmp_path, monkeypatch, item, kind, suite):
    replay = H.run(tmp_path, monkeypatch, _script(_plan(_scope(proofs=_proofs("R1"))), _lead_not_run(item),
                                                  auditor=_auditor(item)),
                   files=FILES, max_tasks=1, record_complete=False, extra_checks=SUITE_EXTRA if suite else ())
    if suite:
        last = H.result_json(replay)["checks"][-1]
        assert last["command"] == "gate suite" and last["report"] is None and len(last["receipts"]) >= 2
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
    reports = [result["checks"][-1]["report"]] + [r.get("report") for r in result["checks"][-1]["receipts"]]
    assert any(CASE in (r or {}).get("passed_cases", []) for r in reports)
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
    replay = H.run(tmp_path, monkeypatch, _script(_plan(_scope(proofs=_proofs("R1"))), _lead_not_run("P1"),
                                                  auditor=_auditor("P1")),
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
        "direct report not parsed": lambda record, entry: [c["report"].__setitem__("state", "truncated")
                                                            for c in record["checks"] if c.get("report")],
        "filtered command": lambda record, entry: [c.__setitem__("command", c["command"] + " -k add")
                                                   for c in record["checks"]],
        "engine mark alone": lambda record, entry: (entry.__setitem__("cases", ["tests/test_app.py::test_other"]),
                                                    entry.__setitem__("discharged_at", 0)),
        "explicit case rejected, file ran other cases": lambda record, entry: (
            entry.__setitem__("item", "tests/test_app.py::test_absent"),
            entry.__setitem__("binding", dict(kind="case", scenario=None, cases=["tests/test_app.py::test_absent"], problem="")),
            entry.__setitem__("cases", ["tests/test_app.py::test_absent"])),
        "parameter instance the run never showed": lambda record, entry: (
            entry.__setitem__("item", "tests/test_app.py::test_add[not-real]"),
            entry.__setitem__("binding", dict(kind="case", scenario=None, cases=["tests/test_app.py::test_add[not-real]"], problem="")),
            entry.__setitem__("cases", ["tests/test_app.py::test_add[not-real]"])),
        "mixed claim bound on one grammar": lambda record, entry: (
            entry.__setitem__("item", "P1 and tests/test_app.py::test_absent"),
            entry.__setitem__("binding", dict(kind="mixed", scenario="P1", cases=[CASE], problem=""))),
        "binding key present but empty": lambda record, entry: entry.__setitem__("binding", {}),
        "binding key missing on an identity claim": lambda record, entry: entry.pop("binding"),
        "kind paths forged on an explicit case": lambda record, entry: (
            entry.__setitem__("item", "tests/test_app.py::test_absent"),
            entry.__setitem__("binding", dict(kind="paths", scenario=None, cases=[], problem="")),
            entry.__setitem__("cases", [])),
        "a spaced parameter instance the run never showed": lambda record, entry: (
            entry.__setitem__("item", "tests/test_app.py::test_add[missing instance]"),
            entry.__setitem__("binding", dict(kind="case", scenario=None, cases=["tests/test_app.py::test_add[missing instance]"], problem="")),
            entry.__setitem__("cases", ["tests/test_app.py::test_add[missing instance]"])),
        "a suffixed id read as its prefix": lambda record, entry: (
            entry.__setitem__("item", "tests/test_app.py::test_add.extra"),
            entry.__setitem__("binding", dict(kind="case", scenario=None, cases=[CASE], problem="")),
            entry.__setitem__("cases", [CASE])),
    }
    for name, mutate in rejected.items():
        assert not _tampered(replay, mutate), name

    def path_only(record, entry):
        # A path-only item (binding kind paths, as the engine records it):
        # cleared by a later check of the right identity whose command names
        # the file whole and whose own report shows a case of it passed.
        entry["item"] = "tests/test_app.py"
        entry["binding"] = dict(kind="paths", scenario=None, cases=[], problem="")
        entry["cases"] = []
        for check in record["checks"]:
            check["command"] = check["command"] + " tests/test_app.py"
    assert _tampered(replay, path_only), "the supported file-only route, with every identity check"

    def path_only_not_operand(record, entry):
        path_only(record, entry)
        for check in record["checks"]:
            check["command"] = check["command"].replace(" tests/test_app.py", "")
    assert not _tampered(replay, path_only_not_operand), "a path the command did not name whole is not shown"

    def path_only_other_source(record, entry):
        path_only(record, entry)
        record["source_fingerprint"] = "0" * 24
    assert not _tampered(replay, path_only_other_source)

    def historical(record, entry):
        # A record written before bindings or identities existed: no binding,
        # gate or environment keys; a path-only claim is held to the final
        # source and the check's own passed cases of that file.
        for key in ("binding", "cases", "gate", "environment"):
            entry.pop(key)
        entry["item"] = "tests/test_app.py"
        for check in record["checks"]:
            check["command"] = check["command"] + " tests/test_app.py"
    assert _tampered(replay, historical), "a historical path-only record keeps its honest reading"

    def suite(record, entry, **receipt):
        # The production shape: the aggregate carries no command line and no
        # report; the evidence is one required receipt per command.
        for check in record["checks"]:
            report = check["report"]
            check["receipts"] = [dict(id="transport", status="passed", required=True,
                                      command=check["command"] + " tests/test_app.py", cwd=".", selection="",
                                      report=report, tests=1, **receipt)]
            check["command"], check["report"] = "gate suite", None

    def suite_case(record, entry, **receipt):
        suite(record, entry, **receipt)
    assert _tampered(replay, suite_case), "an exact-case claim clears on a genuine suite receipt"

    def suite_paths(record, entry, **receipt):
        suite(record, entry, **receipt)
        entry["item"] = "tests/test_app.py"
        entry["binding"] = dict(kind="paths", scenario=None, cases=[], problem="")
        entry["cases"] = []
    assert _tampered(replay, suite_paths), "a path-only claim clears on a genuine suite receipt"

    def receipt_mutation(mutate_receipt, paths=False):
        def apply(record, entry):
            (suite_paths if paths else suite_case)(record, entry)
            for check in record["checks"]:
                mutate_receipt(check["receipts"][0])
        return apply
    import copy
    suite_rejected = {
        "optional receipt": lambda r: r.__setitem__("required", False),
        "failed receipt": lambda r: r.__setitem__("status", "failed"),
        "receipt selection": lambda r: r.__setitem__("selection", "-k add"),
        "receipt filter in argv": lambda r: r.__setitem__("command", r["command"] + " -k add"),
        "receipt cwd": lambda r: r.__setitem__("cwd", "tests"),
        "receipt skip": lambda r: r["report"]["counts"].__setitem__("skipped", 1),
        "receipt count missing": lambda r: r["report"]["counts"].pop("skipped"),
        "receipt no report": lambda r: r.__setitem__("report", None),
        "receipt empty cases": lambda r: r["report"].__setitem__("passed_cases", []),
        "receipt contradictory failure": lambda r: r["report"]["counts"].__setitem__("failed", 1),
        "receipt names another file": lambda r: r.__setitem__("command", r["command"].replace(" tests/test_app.py", " tests/other.py")),
        "receipt report not parsed": lambda r: r["report"].__setitem__("state", "invalid"),
        "receipt report state missing": lambda r: r["report"].pop("state"),
    }
    for name, mutate_receipt in suite_rejected.items():
        for paths in (False, True):
            if name == "receipt names another file" and not paths:
                continue
            assert not _tampered(replay, receipt_mutation(copy.deepcopy(mutate_receipt), paths)), (name, paths)

    def historical_identity_claim(record, entry):
        for key in ("binding", "cases", "gate", "environment"):
            entry.pop(key)
        entry["item"] = "tests/test_app.py::test_absent"
        for check in record["checks"]:
            check["command"] = check["command"] + " tests/test_app.py"
    assert not _tampered(replay, historical_identity_claim), "an identity claim never rides file execution"


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

    shared_text = "P1: add(1, 2); given nothing stored; expect 3 is returned; in tests/test_app.py"
    t2_text = "P1: add is called for R2; given two tickets exist; expect the sum is returned; in tests/test_app.py"

    def auditor(call, replay):
        return judged(call.prompt, {"R1": f"MET - {CASE}", "R2": f"MET - {CASE}"},
                      approve={shared_text: SHOWN, t2_text: SHOWN})
    script = Script(orchestrator=orchestrator, lead=lead,
                    collaborator=lambda c, r: "NO FINDINGS\n" + covered_met(c.prompt), auditor=auditor,
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
    assert shared_row["sufficiency_source"].startswith("explicit line by "), "the case's own line for that row"
    assert result["requirements"]["status"]["R1"].lower().startswith("met")


def test_an_audit_with_verdicts_and_no_row_line_leaves_the_executed_row_unjudged(tmp_path, monkeypatch):
    """The engine side of the fixture contract: a MET on R1 judges nothing
    about the executed row that binds it; unjudged, the row lowers R1."""
    script = _script(_plan(_scope(proofs=_proofs("R1"))), _lead_not_run("P1"),
                     auditor=lambda c, r: f"R1: MET - {CASE}")
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    assert not replay.result.completed
    result = H.result_json(replay)
    [row] = [o for o in result["evidence_obligations"] if o["task"] == "t1" and o["kind"] == "acceptance"]
    assert row["execution"] == "ran" and row["sufficiency"] == "unjudged" and not row["judged_by"]
    audit = result["requirements"]["audits"][-1]
    assert audit["verdicts"]["R1"]["met"] is False
    assert any("unjudged" in r["reason"] for r in audit["lowered"]), audit["lowered"]
    prompt = replay.of("auditor")[-1].prompt
    assert "An executed test row is judged only by your own line" in prompt
    assert "Each executed acceptance obligation listed above needs its own line" in prompt


# -- 4. a command the requirement names is cited by the harness's exact receipt ------------------

_RUN_REQ = "R2: the test suite is run with `python3 -m pytest -q` before completion is declared"


def _two_requirement_plan(second=_RUN_REQ, proves_p2=CASE):
    requirements = "R1: add(a, b) returns the sum\n" + second
    plan = _plan(_scope(proofs=_proofs("R1", "R2")), "R1, R2", requirements)

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nPROVES: P2 - {proves_p2}\nCHANGED: ["app.py"]'
    return plan, lead


P2_SCENARIO = "P2: add is called for R2; given two tickets exist; expect the sum is returned; in tests/test_app.py"


def _command_script(plan, lead, approve_p2=True):
    def auditor(call, replay):
        approve = {ADD_SCENARIO: SHOWN}
        if approve_p2:
            approve[P2_SCENARIO] = SHOWN
        return judged(call.prompt, {"R1": f"MET - {CASE}", "R2": "MET - the harness ran the suite before completion"},
                      approve=approve)
    return Script(orchestrator=lambda c, r: plan if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                  collaborator=lambda c, r: "NO FINDINGS\n" + covered_met(c.prompt), auditor=auditor,
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
    unshown = H.run(_at(tmp_path, "unshown"), monkeypatch, _command_script(plan, lead, approve_p2=False), files=FILES,
                    max_tasks=1, record_complete=False)
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

    def auditor(call, replay):
        return judged(call.prompt, {"R1": f"MET - {CASE}", "R2": "MET - README.md"},
                      approve={"P1: tests/test_app.py exercises R1; given the project as delivered; expect R1 holds "
                               "on the delivered source; in tests/test_app.py": SHOWN,
                               "P1: tests/test_app.py exercises R2; given the project as delivered; expect R2 holds "
                               "on the delivered source; in tests/test_app.py": SHOWN,
                               "P1": f"the parent's merge gate executed {CASE} whole on the merged source"})
    script = Script(orchestrator=lambda c, r: plan if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                    collaborator=lambda c, r: "NO FINDINGS\n" + covered_met(c.prompt), auditor=auditor,
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
