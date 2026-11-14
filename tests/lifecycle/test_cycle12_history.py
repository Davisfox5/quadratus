"""Cycle11 terminal (root's reconciliation, PR73 6726933807): run
20261108T191017Z-f99f6e43 ended at the 8-task cap with R16 NOT MET.

Each audit found one more defect of R16 and R18 (post-commit reads, then
boundary evidence, then a malformed ordinary header), and nothing carried an
earlier audit's question to the repair's lead, its reviewer or the next
audit: ``requirement_audits`` held them and no prompt showed them. The
boundary caveat ("never a refusal the goal does not state") reached the lead
and reviewer but not the auditor's executed-error rule.

Now: earlier audits' own NOT MET reasons for the requirements a call is
about are shown, bounded and labelled as historical questions, to the lead,
revision, reviewer, rechecks and later audits; the shared lens covers a
parser that accepts what the requirement calls invalid; the auditor's rule
keeps a representation the goal leaves unspecified a candidate risk; a NOT
MET's basis is asked for and recorded as provenance only; and a reviewer may
note the input layers it checked (observational). No call, gate, cap or
authority is added.

Real Session and controller with scripted providers and the project's own
pytest gate, and units on a hand-set record."""
import json

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script
from tests.lifecycle.test_proof_binding import CASE, PROOF_R1, SHOWN, _at, _plan, _proofs, _scope
from tests.lifecycle.test_workflow_repair import CODE_FINDING, MET, Seen, _script

pytestmark = pytest.mark.requirements_ledger

HEAD = "## Earlier audits of these requirements (historical questions, not current findings)"
EXAMPLE = "add(-1, -2) returns 3"
FIRST = ("R1: NOT MET - add drops negative operands; basis: \"returns the sum\" is false for negatives; evidence: "
         f"source reading; at app.py add; example: {EXAMPLE}")


def _session(tmp_path, requirements):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal: add(a, b) returns the sum", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=tmp_path, allow_writes=True, requirements_ledger=True))
    session.memory.ledger.requirements.update(requirements)
    return session


# -- 1. the repair's lead, revision, reviewer, recheck and the next audit see the question --------------

def _repair(tmp_path, monkeypatch, name, *, second, max_tasks=2):
    seen = Seen()
    audits = []
    repair = (f"KIND: backend simple\nSCOPE: {json.dumps(_scope(proofs=_proofs('R1')))}\n"
              "Repair add for negatives in app.py.\nCOVERS: R1")

    def orchestrator(call, replay):
        seen.keep("orchestrator", call)
        calls = len(replay.of("orchestrator"))
        if calls == 1:
            return _plan(_scope(proofs=_proofs("R1")))
        if calls == 2 or any(c.task == "t2" for c in replay.of("lead")):
            return "DONE"
        return repair

    def lead(call, replay):
        seen.keep(f"lead:{call.task}", call)
        body = FIXED if call.task == "t1" else "def add(a, b):\n    # negatives too\n    return a + b\n"
        H.write(call, {"app.py": body})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        seen.keep(f"collaborator:{call.task}", call)
        return (CODE_FINDING + MET) if call.task == "t2" else "NO FINDINGS" + MET

    def revision(call, replay):
        seen.keep("revision", call)
        return "Nothing more to change.\nCHANGED: []"

    def recheck(call, replay):
        seen.keep("recheck", call)
        return "RFT2-1: RESOLVED - test_add covers it"

    def verdict(call, replay):
        audits.append(call)
        return FIRST if len(audits) == 1 else second
    script = _script(seen, orchestrator=orchestrator, lead=lead, collaborator=collaborator, revision=revision,
                     recheck=recheck, auditor=judging_scenarios(verdict, PROOF_R1, basis=SHOWN))
    replay = H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=max_tasks,
                   record_complete=False)
    return replay, seen, audits


def test_an_audits_question_reaches_the_repair_and_the_next_audit_judges_it_afresh(tmp_path, monkeypatch):
    """On f14d03a no prompt after the first audit carried its question."""
    replay, seen, audits = _repair(tmp_path, monkeypatch, "carried", second=f"R1: MET - {CASE}")
    for role in ("lead:t2", "collaborator:t2", "revision", "recheck"):
        prompt = seen.prompts[role][0]
        assert HEAD in prompt and f"Example: {EXAMPLE}" in prompt, role
        assert "- R1: audit 1 by " in prompt, role
    assert "on this source" in seen.prompts["lead:t2"][0], "the draft starts on the source the audit judged"
    assert "an earlier source" in seen.prompts["recheck"][0], "after the revision it is history of another source"
    assert HEAD not in seen.prompts["lead:t1"][0], "nothing before any audit"
    second = audits[1].prompt
    assert HEAD in second and "Judge each requirement on the current source yourself" in second
    assert "Basis as stated: \"returns the sum\" is false for negatives" in second
    record = H.result_json(replay)
    first = record["requirements"]["audits"][0]
    assert first["stated_reasons"]["R1"] == FIRST.split(" - ", 1)[1]
    assert first["bases"]["R1"]["stated"] and first["bases"]["R1"]["quoted_in"] == ["R1"]
    assert len(audits) == 2, "no completion call for a basis"
    assert record["requirements"]["status"]["R1"].startswith("met") and replay.result.completed, replay.result.error


def test_an_adverse_second_audit_stands_and_its_question_is_kept_as_history(tmp_path, monkeypatch):
    replay, seen, audits = _repair(tmp_path, monkeypatch, "adverse",
                                   second="R1: NOT MET - add(-1, -2) still returns 3 on the repaired source",
                                   max_tasks=3)
    record = H.result_json(replay)
    assert record["requirements"]["status"]["R1"].startswith("NOT MET") and not replay.result.completed
    assert record["requirements"]["audits"][-1]["bases"]["R1"] == dict(
        basis="", evidence="", quoted=[], quoted_in=[], stated=False)
    refusal = [p for p in seen.prompts["orchestrator"] if "(no basis stated by the auditor; the verdict stands)" in p]
    assert refusal, "the missing basis is labelled for the planner and the verdict stays adverse"


# -- 2. a parallel child reads its parent's audits ---------------------------------------------------

def test_a_parallel_child_after_an_audit_sees_the_question_for_what_it_covers(tmp_path, monkeypatch):
    seen = Seen()

    def block(path, covers, proofs):
        scope = dict(permitted_paths=[path], intended_result=f"{path} written", acceptance=[f"{path} meets the task"],
                     max_lines=20, proofs=proofs)
        return f"KIND: backend simple\nSCOPE: {json.dumps(scope)}\nWrite {path}.\nCOVERS: {covers}"
    batch = ("PARALLEL\n" + block("app.py", "R1", _proofs("R1")) + "\n---\n"
             + block("README.md", "R2", _proofs("R2", location="README.md", evidence="reading")))
    first = _plan(_scope(proofs=_proofs("R1")), covers="R1, R2",
                  requirements="R1: add(a, b) returns the sum\nR2: README documents add")
    first = first.replace(json.dumps(_scope(proofs=_proofs("R1"))), json.dumps(dict(
        _scope(proofs=_proofs("R1") + [dict(_proofs("R2", location="README.md", evidence="reading")[0], id="P2")]),
        permitted_paths=["app.py", "tests/test_app.py", "README.md"])))
    replies = iter([first, "DONE", batch])

    def orchestrator(call, replay):
        return next(replies, "DONE")

    def lead(call, replay):
        seen.keep(f"lead:{call.task}", call)
        if "Write README.md." in call.prompt:
            H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
            return 'Documented add.\nCHANGED: ["README.md"]'
        H.write(call, {"app.py": FIXED if call.task == "t1" else FIXED + "# negatives too\n",
                       **({"README.md": "# app\n\n`add(a, b)` returns the sum.\n"} if call.task == "t1" else {})})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: {json.dumps(["app.py", "README.md"] if call.task == "t1" else ["app.py"])}'

    def collaborator(call, replay):
        seen.keep(f"collaborator:{call.task}", call)
        return "NO FINDINGS\nR1: MET - app.py\nR2: MET - README.md"
    audits = []

    def auditor(call, replay):
        audits.append(call)
        docs = H.obligation_ids(call.prompt, modality="reading", requirement="R2")
        r1 = FIRST if len(audits) == 1 else f"R1: MET - {CASE}"
        return (f"{r1}\nR2: MET - README.md" + "".join(f"\n{o}: SUFFICIENT - README.md names add" for o in docs))
    script = Script(orchestrator=orchestrator, lead=lead, collaborator=collaborator,
                    auditor=judging_scenarios(auditor, PROOF_R1, basis=SHOWN),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(_at(tmp_path, "parallel"), monkeypatch, script, files=FILES, max_tasks=4, record_complete=False)
    children = {t: p for t, ps in seen.prompts.items() for p in ps[:1] if t.startswith("lead:") and t != "lead:t1"}
    app_child = next(p for p in children.values() if "Write app.py." in p)
    readme_child = next(p for p in children.values() if "Write README.md." in p)
    assert HEAD in app_child and f"Example: {EXAMPLE}" in app_child
    assert HEAD not in readme_child, "a child sees only the questions about what it covers"
    assert len(audits) >= 2 and HEAD in audits[-1].prompt
    assert len([c for c in replay.of("lead") if c.task != "t1"]) == 2, "the batch's two children ran"


# -- 3. the history's reading: own words only, deduplicated, bounded, labelled ------------------------

def _audit(number, rid="R1", why="it fails", *, met=False, stated=True, source="S1", revision=1, failed=False):
    return dict(auditor="openai:sol", source=source, reply_artifact=f"art{number}", failed=failed,
                verdicts={rid: dict(met=met, why=why)}, stated=[rid] if stated and not met else [],
                stated_reasons={rid: why} if stated and not met else {}, revisions={rid: revision},
                bases={})


def _stored(session):
    """Each hand-set audit's reply stored as the audit stores it, so the
    history's pointer is checked against the words it names (cycle20: an
    id that holds no such text is shown as unavailable)."""
    for audit in session.requirement_audits:
        if audit.get("verdicts"):
            text = "\n".join(f"{r}: NOT MET - {v['why']}" for r, v in audit["verdicts"].items())
            audit["reply_artifact"] = session.store.put(text, kind="audit-reply:openai:sol", author="openai:sol").id


def test_only_the_auditors_own_reasons_are_shown_deduplicated_with_their_provenance(tmp_path):
    session = _session(tmp_path, {"R1": "add returns the sum"})
    session.requirement_audits = [
        _audit(1, why="x is wrong; example: add(1, 1) gives 3"),
        _audit(2, why="x is wrong; example: add(1, 1) gives 3"),
        _audit(3, why="MET claimed without a file", stated=False),
        _audit(4, why="audit failed", failed=True),
        dict(auditor="openai:sol", result="void: the tree changed"),
    ]
    _stored(session)
    history = session._audit_history(["R1"])
    assert history.count("- R1: audit") == 1 and "Stated again by audit(s) 2" in history
    assert "MET claimed without a file" not in history and "audit failed" not in history
    art1 = session.requirement_audits[0]["reply_artifact"]
    assert f"reply artifact {art1}" in history and "requirement revision 1" in history


def test_a_long_reason_never_cuts_the_example_and_omissions_are_counted(tmp_path):
    session = _session(tmp_path, {"R1": "add returns the sum"})
    example = "add(" + "9" * 300 + ", 1) returns a negative number"
    session.requirement_audits = [_audit(n, why=f"case {n} " + "z" * 2000 + f"; example: {example}")
                                  for n in range(1, 6)]
    _stored(session)
    history = session._audit_history(["R1"])
    assert history.count(f"Example: {example}.") == 3, "the example is whole on every shown entry"
    assert f"more characters in artifact {session.requirement_audits[4]['reply_artifact']}" in history
    assert "(2 earlier audit entries not shown here, for 1 requirement(s): R1. Each audit's whole reply" in history


def test_stale_source_revised_requirement_later_met_and_rulings_are_shown_as_such(tmp_path):
    session = _session(tmp_path, {"R1": "add returns the sum"})
    session._requirement_revision_record["R1"] = [2, "digest-of-an-older-text"]
    session.memory.ledger.decisions["R1"] = "R1 - integers only; spellings are unspecified"
    session.memory.ledger.rulings.append("R1 accepts '+1' as 1")
    session.requirement_audits = [_audit(1, why="spelling +1 is accepted", revision=1),
                                  _audit(2, met=True, why="tests/test_app.py::test_add")]
    history = session._audit_history(["R1"])
    assert "an earlier source S1" in history
    assert "revised since (now 3): judge the current wording" in history
    assert "Audit 2 later found R1 MET on another source." in history
    assert "Current decision naming R1 (binds over any older condition)" in history
    assert "Current ruling 1 naming R1 (binds over any older condition): R1 accepts '+1' as 1" in history


def test_reading_history_changes_no_finding_owner_or_status(tmp_path):
    session = _session(tmp_path, {"R1": "add returns the sum"})
    session.requirement_audits = [_audit(1)]
    session.review_findings = [dict(fid="RFT1-1", author="anthropic:opus", closed=True, state="resolved",
                                    requirement=["R1", 1], history=[], claim="c", text="BLOCKING: c")]
    session.memory.ledger.requirement_status["R1"] = "met (audited)"
    before = json.dumps([session.review_findings, dict(session.memory.ledger.requirement_status)], sort_keys=True)
    assert "- R1: audit 1" in session._audit_history(["R1"])
    assert json.dumps([session.review_findings, dict(session.memory.ledger.requirement_status)],
                      sort_keys=True) == before
    assert session._audit_history(["R2"]) == "" and session._audit_history([]) == ""


def test_history_survives_a_json_round_trip_as_the_checkpoint_carries_it(tmp_path):
    from quadratus import checkpoint
    assert "boundary_notes" in checkpoint._CARRIED and "boundary_notes" in checkpoint._ADDED_LATER
    session = _session(tmp_path, {"R1": "add returns the sum"})
    session.requirement_audits = [_audit(1, why="x; example: add(1, 1) gives 3")]
    restored = _session(tmp_path, {"R1": "add returns the sum"})
    restored.requirement_audits = json.loads(json.dumps(session.requirement_audits))
    assert restored._audit_history(["R1"]) == session._audit_history(["R1"])


# -- 4. basis is provenance; the verdict is the auditor's --------------------------------------------

@pytest.mark.parametrize("why, stated, quoted_in, label", [
    ("wrong; basis: \"returns the sum\" fails for negatives; evidence: executed reproduction", True, ["R1", "goal"], ""),
    ("wrong; at app.py add", False, [], "(no basis stated by the auditor; the verdict stands)"),
    ("wrong; basis: \"must reject +1\" per the CLI contract", True, [],
     "(its quoted basis was not found in the goal, the requirement or a ruling; recorded only)"),
])
def test_a_basis_is_recorded_as_provenance_and_never_changes_the_verdict(tmp_path, why, stated, quoted_in, label):
    session = _session(tmp_path, {"R1": "add(a, b) returns the sum"})
    basis = session._not_met_basis("R1", why)
    assert basis["stated"] is stated and basis["quoted_in"] == quoted_in
    session.requirement_audits = [dict(_audit(1, why=why), bases={"R1": basis})]
    assert session._basis_label("R1") == ((" " + label) if label else "")
    assert session.requirement_audits[0]["verdicts"]["R1"]["met"] is False, "the NOT MET is untouched"


# -- 5. the lens and the rules are goal-bounded at every seat ----------------------------------------

def test_the_lens_and_the_auditors_rule_bound_refusals_to_the_goal(tmp_path):
    from quadratus.session import _ERROR_BOUNDARY_LENS, _EXECUTED_ERROR_RULE
    lens = " ".join(_ERROR_BOUNDARY_LENS.split())
    assert "can also accept input the requirement calls invalid" in lens
    assert "never treat an input the goal leaves unspecified as one that must be refused" in lens
    rule = " ".join(_EXECUTED_ERROR_RULE.split())
    assert "where the goal or the requirement makes them invalid" in rule
    assert "is a candidate risk to check, never a rejection to require" in rule
    import inspect

    from quadratus.session import Session
    audit = " ".join(inspect.getsource(Session._audit_requirements).split())
    assert "basis: <the words of the goal, the " in audit and "evidence: <executed reproduction | source reading" in audit


# -- 6. boundary notes are observations --------------------------------------------------------------

ERR = "Invalid input returns 400 and changes nothing"


def test_boundary_notes_are_recorded_bounded_and_shown_to_the_audit_as_observations(tmp_path):
    session = _session(tmp_path, {"R1": ERR, "R2": "add returns the sum", "R3": "Unknown ids return 404"})
    reply = "NO FINDINGS\nR1: MET - app.py:3\n" + "".join(
        f"BOUNDARY R1: layer {i} - tests/test_app.py::test_x (executed)\n" for i in range(12))
    session._record_boundary_notes("openai:sol", "collaborator", reply, task="t1")
    assert len(session.boundary_notes) == 8 and session.boundary_notes[0]["requirement"] == "R1"
    from quadratus.session import _clean_goal_review
    assert _clean_goal_review(reply, ["R1"]), "a note never makes a clean review unfinished"
    block = session._boundary_notes_block(["R1", "R2", "R3"])
    assert "## Input layers reviewers said they checked (observations, not proof)" in block
    assert "- R1: openai:sol (collaborator, t1" in block and "- R3: none stated" in block
    assert "R2" not in block.split("proof)", 1)[1], "a requirement that is not about errors is not listed"
    assert session._boundary_notes_block(["R2"]) == ""


def test_the_reviewer_is_offered_the_note_only_for_an_error_requirement(tmp_path):
    from quadratus.session import _BOUNDARY_NOTE_RULE
    session = _session(tmp_path, {"R1": ERR, "R2": "add returns the sum"})
    session._covered_texts = lambda: [("R1", ERR)]
    assert _BOUNDARY_NOTE_RULE in session._goal_authority()
    session._covered_texts = lambda: [("R2", "add returns the sum")]
    assert _BOUNDARY_NOTE_RULE not in session._goal_authority()


# -- 3b. one shared bound, framing and metadata included (root review of b54a28c) -------------------

def test_many_requirements_and_their_metadata_stay_inside_one_bound_with_every_omission_counted(tmp_path):
    """Root's reproduction: 23 requirements, one 500-character reason and a
    400-character example each, rendered 24,515 characters on b54a28c."""
    from quadratus.session import _HISTORY_TOTAL_CHARS
    rids = [f"R{n}" for n in range(1, 24)]
    session = _session(tmp_path, {rid: f"{rid} refuses invalid input" for rid in rids})
    session.memory.ledger.decisions["R2"] = "R2 - spellings are unspecified, so '+1' stays accepted"
    session.memory.ledger.rulings.append("R3 is judged on the HTTP layer only. " + "x" * 900)
    example = "PATCH /t/1 with " + "y" * 380
    session.requirement_audits = [dict(
        auditor="openai:sol", source="S1", reply_artifact="art1", failed=False,
        verdicts={rid: dict(met=False, why="w") for rid in rids}, stated=list(rids),
        stated_reasons={rid: f"{rid} " + "z" * 496 + f"; example: {example}" for rid in rids},
        revisions={rid: 1 for rid in rids}, bases={})]
    history = session._audit_history(rids)
    assert len(history) <= _HISTORY_TOTAL_CHARS, len(history)
    assert "historical questions, not current findings" in history and "transfers no finding or owner" in history
    shown = [rid for rid in rids if f"\n- {rid}: audit 1" in history]
    assert 0 < len(shown) < len(rids)
    hidden = len(rids) - len(shown)
    assert (f"({hidden} earlier audit entries not shown here, for {hidden} requirement(s): " in history
            and "Each audit's whole reply is kept as a run artifact (audits 1-1, kind audit-reply)" in history)
    assert f"and {hidden - 8} more" in history, "the omission line names a bounded list"
    assert history.count(f"Example: {example}.") == len(shown), "every shown entry keeps its whole example"
    if "R2" in shown:
        assert "Current decision naming R2 (binds over any older condition)" in history
    if "R3" in shown:
        assert "Current ruling 1 naming R3 binds over any older condition; it is" in history
        assert "x" * 50 not in history, "a long ruling is named, never cut"


def test_a_requirement_is_never_shown_without_what_supersedes_it(tmp_path):
    from quadratus.session import _HISTORY_TOTAL_CHARS
    rids = ["R1", "R2"]
    session = _session(tmp_path, {"R1": "refuses invalid input", "R2": "refuses invalid input"})
    session.memory.ledger.rulings.extend([f"R2 ruling {i}: " + "q" * 500 for i in range(12)])
    session.requirement_audits = [dict(
        auditor="openai:sol", source="S1", reply_artifact="art1", failed=False,
        verdicts={rid: dict(met=False, why="w") for rid in rids}, stated=list(rids),
        stated_reasons={rid: f"{rid} fails; example: input {rid}" for rid in rids}, revisions={}, bases={})]
    history = session._audit_history(rids)
    assert len(history) <= _HISTORY_TOTAL_CHARS
    assert "- R1: audit 1" in history
    assert "- R2: audit 1" not in history and "none shown for R2" in history, \
        "R2's older condition is not shown when its rulings do not all fit"
