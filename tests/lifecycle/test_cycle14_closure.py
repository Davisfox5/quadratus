"""Cycle14 (PR73 6731607762): closure of a split execution truth and of the
same-author/auditor dead end.

- A1. A path-only NOT RUN item discharged by the harness's own complete
  passing run naming its file had a typed row still reading runner none,
  execution missing (cycle13 O59/O63/O64), and the row lowered R14-R17. The
  row now asks the same discharge decision the item uses.
- A2. A restatement written by the auditor's own seat could not be judged by
  anyone: the DONE declaration refused "lead is the auditor", and after a
  revision invalidated an earlier independent judgment nothing current could
  judge it (cycle13 O60). The judgment is now offered to an eligible
  independent seat already on the run, the recorded judge first, in one
  read-only call per judge on the shared judgment allowance.
- B. Repair tasks state the goal outcome and the causal class; the review
  lens reads reachable framework refusal sites and permitted forms.
"""
import re
from types import SimpleNamespace

import pytest

from quadratus import checkpoint
from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig
from tests.lifecycle import harness as H
from tests.lifecycle.test_done_declarations import (
    CLEAN,
    SUFFICIENT,
    _answering,
    _declaring,
)
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_proof_binding import CASE, _at, _plan, _proofs, _scope
from tests.lifecycle.test_reachable_evidence import _item, _lead
from tests.lifecycle.test_workflow_repair import Seen, _script

pytestmark = pytest.mark.requirements_ledger

#: A standard task: its lead (Sol) is the auditor's own seat.
PLAN_SOL = _plan(_scope(proofs=_proofs("R1"))).replace("KIND: backend simple", "KIND: backend standard")
ASKED = re.compile(r"^- (O\d+) \(reported by", re.MULTILINE)


def _judge(verdict=SUFFICIENT, seen=None):
    """The independent judge's one line per asked id; any other recheck call
    answers as a recheck does."""
    def recheck(call, replay):
        if call.task == "restatement-judgment":
            if seen is not None:
                seen.append(call)
            if callable(verdict):
                return verdict(call, replay)
            return "\n".join(f"{oid}: {verdict}" for oid in ASKED.findall(call.prompt))
        return "RFT1-1: RESOLVED shown now"
    return recheck


def _then_done(*plans):
    replies = iter(plans or (PLAN_SOL,))
    return lambda call, replay: next(replies, "DONE")


def _run(tmp_path, monkeypatch, name, *, judge, declare=None, **kw):
    kw.setdefault("max_tasks", 2)
    script = _script(Seen(), collaborator=lambda c, r: CLEAN, orchestrator=_then_done(), lead=_lead(),
                     recheck=judge, auditor=_answering)
    script.overrides["proof-binding"] = declare or _declaring()
    return H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, record_complete=False, **kw)


# -- A2 lifecycle: author == auditor at the DONE declaration --------------------------------------

def test_a_declaration_by_the_auditors_own_seat_is_judged_by_another_independent_seat(tmp_path, monkeypatch):
    asked = []
    replay = _run(tmp_path, monkeypatch, "judged", judge=_judge(seen=asked))
    record = H.result_json(replay)
    entry = _item(replay)
    (restated,) = entry["restatements"]
    auditor = record["requirements"]["audits"][-1]["auditor"]
    assert restated["author"] == auditor, "the restatement's author is the auditor's own seat"
    (decl,) = record["done_declarations"]
    assert decl["outcome"] == "declared" and decl["settled"] == [entry["oid"]]
    (call,) = asked
    assert call.vendor == "claude" and call.model == "opus", "the recorded independent reviewer"
    assert entry["oid"] in call.prompt and "You did not write these restatements" in call.prompt
    (judgment,) = record["restatement_judgments"]
    assert judgment["oids"] == [entry["oid"]] and judgment["outcome"] == "answered"
    assert judgment["sufficient"] == [entry["oid"]] and judgment["reply_artifact"]
    assert judgment["source"] == record["source_fingerprint"] and judgment["decision"]
    judged = entry["restated_judgment"]
    assert judged["verdict"] == "sufficient" and judged["by"] == judgment["judge"] != auditor
    (spent,) = [c for c in record["judgment_completions"] if c["task"] == "restatement-judgment"]
    assert spent["claimed"] and spent["role"] == "recheck"
    assert [c.task for c in replay.of("lead")] == ["t1"], "no extra task"
    (row,) = [o for o in record["evidence_obligations"] if o.get("oid") == entry["oid"]]
    assert row["execution"] == "ran", row
    assert replay.result.completed, replay.result.error


@pytest.mark.parametrize("name, verdict, outcome", [
    ("adverse", "INSUFFICIENT it never drives the socket path the item named", "answered"),
    ("no line", lambda c, r: "I looked at the tests.", "answered"),
    ("capped", lambda c, r: H.claude_cap("Reading the tests; not finished."), "capped: no verdict; held"),
    ("stopped", lambda c, r: H.claude_error("the call failed"), "stopped: "),
])
def test_an_adverse_or_missing_independent_judgment_leaves_the_item_held(tmp_path, monkeypatch, name, verdict,
                                                                         outcome):
    replay = _run(tmp_path, monkeypatch, name.replace(" ", "-"), judge=_judge(verdict))
    record = H.result_json(replay)
    entry = _item(replay)
    (judgment,) = record["restatement_judgments"]
    assert judgment["outcome"].startswith(outcome), judgment
    assert outcome == "answered" or judgment["outcome"].endswith("; held"), judgment
    assert not (entry.get("restated_judgment") or {}).get("by") == entry["restatements"][-1]["author"]
    if name == "adverse":
        assert judgment["insufficient"] == [entry["oid"]]
    if name == "no line":
        assert judgment["unanswered"] == [entry["oid"]]
    assert not (entry.get("restated_judgment") or {}).get("verdict") == "sufficient"
    assert not replay.result.completed, "the item stays held"
    assert [c.task for c in replay.of("lead")] == ["t1"], "no extra task"


# -- A2 lifecycle: a revision invalidates the independent judgment --------------------------------

def test_a_judgment_a_later_source_invalidated_is_asked_again_of_the_recorded_judge_never_reused(tmp_path,
                                                                                                   monkeypatch):
    """The O60 shape: Sol (the auditor) restated the item; Opus judged it
    SUFFICIENT; a repair by Sol then changed the source. The old judgment
    is stale and never reused; the new decision goes to the recorded judge,
    on the shared allowance, which the first judgment already spent, so the
    item stays held and the record says why."""
    from tests.lifecycle.test_done_declarations import ADVERSE
    from tests.lifecycle.test_lifecycle_matrix import FIXED
    asked, audits = [], []

    def lead(call, replay):
        if call.task == "t1":
            return _lead()(call, replay)
        H.write(call, {"app.py": FIXED + "\n# t2: handles negative operands\n"})
        return f"Repaired add.\nPROVES: P1 - {CASE}\nCHANGED: [\"app.py\"]"

    def auditor(call, replay):
        audits.append(call)
        if len(audits) == 1:
            return ADVERSE
        return _answering(call, replay)
    repair = PLAN_SOL.replace("Implement add in app.py.", "Repair add for negative operands.")
    script = _script(Seen(), collaborator=lambda c, r: CLEAN, orchestrator=_then_done(PLAN_SOL, "DONE", repair),
                     lead=lead, recheck=_judge(seen=asked), auditor=auditor)
    script.overrides["proof-binding"] = _declaring()
    replay = H.run(_at(tmp_path, "stale"), monkeypatch, script, files=FILES, record_complete=False, max_tasks=2)
    record = H.result_json(replay)
    entry = _item(replay)
    first, second = record["restatement_judgments"]
    assert first["outcome"] == "answered" and first["sufficient"] == [entry["oid"]]
    assert second["judge"] == first["judge"] == "claude:opus", "the recorded judge is asked again"
    assert second["source"] != first["source"] and second["decision"] != first["decision"]
    assert second["source"] == record["source_fingerprint"]
    assert second["outcome"] == "not attempted: the job's allowance is spent; held", second
    assert len(asked) == 1, "the shared allowance bought one judgment call"
    assert entry["restated_judgment"]["source"] == first["source"] != record["source_fingerprint"], "never reused"
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "no extra task"
    assert not replay.result.completed


def test_at_the_task_cap_the_auditors_own_restatement_is_judged_before_the_final_audit(tmp_path, monkeypatch):
    asked = []
    replay = _run(tmp_path, monkeypatch, "cap", judge=_judge(seen=asked), max_tasks=1)
    record = H.result_json(replay)
    (judgment,) = record["restatement_judgments"]
    assert judgment["site"] in ("cap", "final") and judgment["sufficient"] == [_item(replay)["oid"]], judgment
    roles = [(c.role, c.task) for c in replay.calls]
    assert roles.index(("recheck", "restatement-judgment")) < roles.index(("auditor", "audit"))
    assert len(asked) == 1 and [c.task for c in replay.of("lead")] == ["t1"], "no slot was added"
    assert replay.result.completed, replay.result.error


# -- A1 units: the typed row asks the item's own discharge decision -------------------------------

TEST_FILE = "tests/test_api.py"


def _a1(tmp_path, monkeypatch, *, item=TEST_FILE, run=None, check=None, entry=None, revision=1, owner=None):
    """A path-only NOT RUN item registered while the gate did not name its
    file (no runner), then one later check whose one command is ``run``."""
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=str(tmp_path), requirements_ledger=True))
    session._source_fingerprint = lambda: "S"
    session._current_gate = lambda: None
    session._environment_id = lambda: "E"
    revisions = {"R1": 1}
    session._requirement_revisions = lambda: dict(revisions)
    session._gate_can_run = lambda paths: False
    base = dict(task="t1", oid="O1", item=item, why="not in the gate", role="lead", cases=[], kind="not_run",
                binding=dict(kind="paths"), requirements=["R1"], after_check=0, executable=True, gate="",
                environment="E")
    base.update(entry or {})
    session.unexecuted_acceptance = [base]
    row = session._register_obligation(base, kind="not_run")
    assert row["runner"] is None, "registered with no runner, as cycle13's O59/O63/O64 were"
    if owner is not None:
        session.unexecuted_acceptance[0]["task"] = owner
    session.checks = [dict(dict(source="S", passed=True, environment="E", gate="", requirements={"R1": 1}),
                           **(check or {}))]
    command = dict(id="public-pytest", passed=True, required=True, filtered=False, tests=4, skipped=0,
                   cwd=".", files=[TEST_FILE, "tests/test_cli.py"], report_cases=[])
    command.update(run or {})
    monkeypatch.setattr(S, "_check_runs", lambda check, root=None: [command])
    revisions["R1"] = revision
    session._refresh_obligations()
    (row,) = session.evidence_obligations
    return session, row


def test_a_runnerless_row_is_discharged_by_the_items_own_complete_named_run(tmp_path, monkeypatch):
    session, row = _a1(tmp_path, monkeypatch)
    assert session._discharge_record(session.unexecuted_acceptance[0]) is not None, "the item reads discharged"
    assert row["runner"] == "gate" and row["execution"] == "ran", row
    assert list(row["receipts"]) == ["check-1"] and "public-pytest in check-1" in row["runner_basis"]
    assert row["source_digest"] == "S"


@pytest.mark.parametrize("name, kw", [
    ("filtered", dict(run=dict(filtered=True))),
    ("failed", dict(run=dict(passed=False), check=dict(passed=False))),
    ("skipping", dict(run=dict(skipped=1))),
    ("no cases", dict(run=dict(tests=0))),
    ("optional command", dict(run=dict(required=False))),
    ("unrelated file", dict(run=dict(files=["tests/test_other.py"]))),
    ("production path", dict(item="app.py")),
    ("other source", dict(check=dict(source="S0"))),
    ("other gate", dict(check=dict(gate="G2"))),
    ("other environment", dict(check=dict(environment="E2"))),
    ("superseded revision", dict(revision=2)),
    ("check before the report", dict(entry=dict(after_check=1))),
    ("stored marker only", dict(run=dict(files=["tests/test_other.py"]),
                                entry=dict(discharge_state="discharged", discharged_at=0, discharged_run="public-pytest"))),
    ("another task's item", dict(owner="t9")),
])
def test_a_runnerless_row_gets_no_runner_without_the_items_own_discharge(tmp_path, monkeypatch, name, kw):
    session, row = _a1(tmp_path, monkeypatch, **kw)
    assert row["runner"] is None and row["execution"] != "ran", (name, row)
    assert "runner_basis" not in row


# -- A2 units: who may judge, the plan's refusal split, once per decision, persistence ------------

RESTATED = dict(task="t1", oid="O2", item="app.py", why="outside my paths", role="lead", cases=[CASE],
                kind="not_run", binding=dict(kind="case", cases=[CASE]), requirements=["R1"], after_check=0,
                executable=False, restatements=[dict(task="t1", role="proof-binding", author="openai:sol",
                                                     cases=[CASE], source="S")])


def _a2(tmp_path, *, auditor="openai:sol", available=lambda key: True, entries=None, reply=None, limit=1):
    calls = []

    def invoke(key, prompt, **_kw):
        calls.append((key, prompt))
        return reply(prompt) if reply else "O2: SUFFICIENT test_add drives the reported path"
    session = Session("goal", ArtifactStore(tmp_path / "a"), invoke,
                      config=SessionConfig(project=str(tmp_path), max_judgment_completions=limit))
    session._source_fingerprint = lambda: "S"
    session._current_gate = lambda: None
    session._environment_id = lambda: "E"
    session.checks = [dict(source="S", passed=True, environment="E", gate="")]
    session.unexecuted_acceptance = [dict(e) for e in (entries if entries is not None else [RESTATED])]
    session._standing_not_run = lambda: [e for e in session.unexecuted_acceptance
                                         if not session._restated_judged(e)]
    session._discharge_record = lambda e: dict(index=0, run="check", cases=list(e.get("cases") or []))
    session._auditor = lambda: auditor
    session._available = available
    session._invoke_model = lambda key, prompt, **kw: invoke(key, prompt)
    session._judgment_binding = lambda spec, peer, role: dict(identity="I", source="S", task=spec.task_id)
    session._budget = lambda: None
    return session, calls


def test_an_independent_judge_is_another_vendors_available_seat_already_on_the_run(tmp_path):
    session, _ = _a2(tmp_path)
    trust = list(session.brain_trust)
    assert session._independent_judge("openai:sol") in trust
    assert not session._independent_judge("openai:sol").startswith(("openai:", "grok:"))
    assert session._independent_judge("openai:sol", ["xai:grok", "openai:luna", "claude:opus"]) == "claude:opus", \
        "a worker seat or the author's own vendor is never a judge"
    assert session._independent_judge("claude:opus", ["claude:opus"]) != "claude:opus", "never the author"
    session._available = lambda key: False
    assert session._independent_judge("openai:sol") is None, "an unavailable seat is no judge"


def test_one_call_per_judge_for_all_its_items_asked_once_per_decision(tmp_path):
    second = dict(RESTATED, oid="O3", item="cli.py")
    session, calls = _a2(tmp_path, entries=[RESTATED, second],
                         reply=lambda p: "O2: SUFFICIENT drives it\nO3: INSUFFICIENT never reaches the CLI path")
    (record,) = session._judge_restatements("done")
    assert len(calls) == 1 and calls[0][0] not in ("openai:sol",) and record["oids"] == ["O2", "O3"]
    assert record["sufficient"] == ["O2"] and record["insufficient"] == ["O3"] and record["unanswered"] == []
    assert "O2" in calls[0][1] and "O3" in calls[0][1] and "You did not write these restatements" in calls[0][1]
    assert session.unexecuted_acceptance[0]["restated_judgment"]["by"] == record["judge"]
    assert session._judge_restatements("final") == [] and len(calls) == 1, "O3's adverse decision is not re-asked"


def test_without_an_eligible_judge_or_allowance_the_item_stays_held_and_says_why(tmp_path):
    session, calls = _a2(tmp_path, available=lambda key: key == "openai:sol")
    (record,) = session._judge_restatements("done")
    assert record["judge"] is None and "no eligible independent seat" in record["outcome"] and calls == []
    assert session._judge_restatements("final") == [] and session.restatement_judgments == [record], "recorded once"
    session, calls = _a2(tmp_path / "off", limit=0)
    (record,) = session._judge_restatements("done")
    assert record["outcome"] == "not attempted: turned off; held" and calls == []
    assert not session._restated_judged(session.unexecuted_acceptance[0])


def test_an_auditor_that_did_not_write_the_restatement_judges_it_itself(tmp_path):
    session, calls = _a2(tmp_path, auditor="claude:opus")
    assert session._judge_restatements("done") == [] and calls == [], "the audit's own route, unchanged"


def test_a_self_judgment_line_is_never_read(tmp_path):
    session, calls = _a2(tmp_path)
    session._judge_restated("openai:sol", "O2: SUFFICIENT I wrote it and it is fine")
    assert not session.unexecuted_acceptance[0].get("restated_judgment")


def test_the_record_survives_a_resume_and_a_merged_childs_decision_is_not_asked_again(tmp_path):
    import inspect
    session, calls = _a2(tmp_path, limit=0)
    (asked,) = session._judge_restatements("done")
    state = checkpoint.capture_state(session)
    assert state["restatement_judgments"] == [asked] and asked["keys"]["O2"]
    resumed, calls = _a2(tmp_path / "resumed")
    checkpoint.restore_state(resumed, state)
    assert resumed.restatement_judgments == [asked]
    assert resumed._judge_restatements("done") == [] and calls == [], "spent across the resume"
    # A parallel child that asked the same item on the same source: the
    # merge carries its record whole and the decision is spent in the parent.
    parent, calls = _a2(tmp_path / "parent")
    child = SimpleNamespace(restatement_judgments=[dict(asked, outcome="answered")])
    parent.restatement_judgments.extend(dict(d) for d in child.restatement_judgments)
    assert parent._judge_restatements("done") == [] and calls == [], "spent after the merge"
    merge = inspect.getsource(Session._run_batch)
    assert 'self.restatement_judgments.extend(dict(d) for d in getattr(child, "restatement_judgments"' in merge


def test_the_declaration_plan_offers_the_auditors_own_seat_only_when_another_can_judge(tmp_path, monkeypatch):
    from tests.test_done_declarations_units import _session
    session, run = _session(tmp_path, lead="openai:sol")
    monkeypatch.setattr(S, "_check_runs", lambda check, root=None: [run])
    monkeypatch.setattr(S, "_complete_run", lambda r: True)
    monkeypatch.setattr(S, "_run_passed_cases", lambda r: set(r.get("report_cases") or ()))
    plan, why = session._done_declaration_plan({"R1": "NOT RUN"})
    assert why == "" and list(plan) == ["t1"] and plan["t1"]["lead"] == "openai:sol", why
    session._independent_judge = lambda *a, **k: None
    plan, why = session._done_declaration_plan({"R1": "NOT RUN"})
    assert plan == {} and "is the auditor" in why and "no other independent seat is eligible" in why


# -- B: repair outcome and the mapping read, stated where the planner and reviewers read them -----

def test_repair_tasks_state_the_goal_outcome_and_the_causal_class_and_reviews_read_refusal_sites():
    import inspect
    rule = " ".join(S._REPAIR_OUTCOME_RULE.split())
    assert "the outcome the goal entitles the caller to and the class of cause" in rule
    assert "never a narrower implementation condition that one reproduction happened to show" in rule
    sources = {name: " ".join(inspect.getsource(getattr(Session, name)).split())
               for name in ("_requirements_satisfied", "_open_findings_prompt")}
    for name, text in sources.items():
        assert "_REPAIR_OUTCOME_RULE" in text, name
    lens = " ".join(S._ERROR_BOUNDARY_LENS.split())
    assert "where the framework or server itself can refuse that input before the code's own handling runs" in lens
    assert "or that refuses a form the goal accepts, does not meet the requirement" in lens
    assert "not an exhaustive search" in lens
    for word in ("505", "HTTP/", "version", "http.server", "BaseHTTPRequestHandler"):
        assert word not in lens and word not in rule, word


# -- A1 lifecycle: row, lowering, result.json and the outside checker agree ------------------------

NAMED_GATE = H.GATE.replace(" -m pytest -q ", " -m pytest -q tests/test_app.py ")
PATH_ONLY = "NOT RUN: tests/test_app.py - I did not run it before closing"


@pytest.mark.parametrize("gate, ran", [(NAMED_GATE, True), (H.GATE, False)], ids=["named-file", "discovery"])
def test_a_path_only_report_the_effective_gate_ran_whole_agrees_everywhere(tmp_path, monkeypatch, gate, ran):
    """cycle13 O59/O63/O64: registration read the gate as not naming the
    file (``_gate_can_run`` False here, standing for the reading that
    differed from the later effective gate). A gate naming the file whole
    then ran it complete and passing: the item, its typed row, the audit's
    lowering, result.json and tools/truth_check agree it ran. A discovery
    gate names no file, so the same report stays truthful debt everywhere."""
    from tests.lifecycle.test_done_declarations import PLAN1
    from tools.truth_check import _not_run_cleared
    monkeypatch.setattr(Session, "_gate_can_run", lambda self, paths: False)
    script = _script(Seen(), collaborator=lambda c, r: CLEAN, orchestrator=_then_done(PLAN1),
                     lead=_lead(note=PATH_ONLY), auditor=_answering)
    replay = H.run(_at(tmp_path, f"path-only-{ran}"), monkeypatch, script, files=FILES, check=gate,
                   record_complete=False, max_tasks=1)
    record = H.result_json(replay)
    (entry,) = [e for e in record["unexecuted_acceptance"] if e["item"] == "tests/test_app.py"]
    (row,) = [o for o in record["evidence_obligations"] if o.get("oid") == entry["oid"]]
    status = record["requirements"]["status"]["R1"]
    if ran:
        assert entry["discharge_state"] == "discharged" and entry["discharged_cases"] == []
        assert row["runner"] == "gate" and row["execution"] == "ran" and row["runner_basis"], row
        assert status == "met (audited)", status
        assert _not_run_cleared(entry, record["checks"], record)
        assert replay.result.completed, replay.result.error
    else:
        assert entry["discharge_state"] == "standing" and row["runner"] is None and row["execution"] != "ran"
        assert "runner_basis" not in row and status.startswith("NOT MET"), status
        assert not _not_run_cleared(entry, record["checks"], record)
        assert not replay.result.completed
