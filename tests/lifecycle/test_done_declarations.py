"""Cycle05 (PR73 6715806056), the finish that settles what is settleable.

Cycle04 ended INCOMPLETE on two kinds of held work that needed no edit: NOT
RUN items its owning lead could restate as existing cases, routed through a
whole editing task; and an audit whose prompt asked for O21/O50/O51/O55 and
got a reply that omitted all four while marking their requirements MET (the
prompt both asked for those lines and said "one line per requirement and
nothing else", and listed the executed restatements under "Not shown to
have run").

- At DONE, debt a declaration settles goes to the owning lead in one bounded
  read-only declaration call per owner: no planner call, no task slot, no
  write grant, priced at the reserve. Existing exact receipts settle it; the
  audit judges it.
- The audit prompt carries one authoritative list of required lines; an id
  with no line is an omission, completed once by the same auditor on the
  job's shared judgment allowance, reading only the omitted ids.

Real Session and controller with scripted providers and the project's own
pytest gate."""
import json
import re
from pathlib import Path

import pytest

from quadratus.run_budget import RunLimits
from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios, required_lines
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_proof_binding import (
    CASE,
    NEGATIVE,
    PROOF_R1,
    SHOWN,
    _at,
    _plan,
    _proofs,
    _scope,
)
from tests.lifecycle.test_reachable_evidence import _item, _lead
from tests.lifecycle.test_workflow_repair import MET, SKIP_TESTS, Seen, _script

pytestmark = pytest.mark.requirements_ledger

#: A complex task: its lead (Opus) is not the auditor (Sol), so the lead's
#: restatement can be judged independently.
PLAN1 = _plan(_scope(proofs=_proofs("R1"))).replace("KIND: backend simple", "KIND: backend complex")
DECLARED = re.compile(r"^- (O\d+): app\.py", re.MULTILINE)
SUFFICIENT = "SUFFICIENT test_add drives add through the path the item named and asserts its result"
ADVERSE = "R1: NOT MET - add drops negative operands; at app.py add; example: add(-1, -2) returns 3"


def _declaring(case=CASE, *, extra=""):
    """The owning lead's DONE declaration: one RUNS line for its listed item."""
    def answer(call, replay):
        found = DECLARED.search(call.prompt)
        assert found, "the declaration prompt lists the held item by id"
        return f"RUNS: {found.group(1)} - {case}{extra}"
    return answer


def _auditor(first=None, *, complete=SUFFICIENT):
    """An audit that answers R1 and the scenario row but never the executed
    restatement (cycle04's omission), and a completion that answers exactly
    what it is asked, plus ``complete``'s extra lines."""
    base = judging_scenarios(first or (lambda c, r: f"R1: MET - {CASE}"), PROOF_R1, basis=SHOWN)

    def auditor(call, replay):
        if "It gave no line for:" in call.prompt:
            asked = required_lines(call.prompt)
            return "\n".join(f"{oid}: {complete}" for oid in asked.get("restatement", []) + asked.get("row", []))
        return base(call, replay)
    return auditor


def _answering(call, replay):
    """An audit that answers its whole required-line list."""
    base = judging_scenarios(lambda c, r: f"R1: MET - {CASE}", PROOF_R1, basis=SHOWN)(call, replay)
    said = {line.split(":", 1)[0].strip() for line in base.splitlines() if ":" in line}
    asked = required_lines(call.prompt)
    return base + "".join(f"\n{oid}: {SUFFICIENT}" for oid in asked.get("restatement", []) + asked.get("row", [])
                          if oid not in said)


def _then_done(*plans):
    replies = iter(plans or (PLAN1,))
    return lambda call, replay: next(replies, "DONE")


#: A complex task's reviewer closes its review explicitly.
CLEAN = "NO FINDINGS" + MET + "\nREVIEW: COMPLETE"


def _clean(call, replay):
    return CLEAN


def _run(tmp_path, monkeypatch, name, script, **kw):
    kw.setdefault("max_tasks", 2)

    return H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, record_complete=False, **kw)


# -- 1. the composed route: declaration, existing receipt, audit, completion ---------------------

def test_a_held_item_is_declared_at_done_settled_by_its_receipt_and_completed_by_the_same_auditor(tmp_path,
                                                                                                    monkeypatch):
    seen = Seen()
    script = _script(seen, collaborator=_clean, orchestrator=_then_done(), lead=_lead(), auditor=_auditor())
    script.overrides["proof-binding"] = _declaring()
    replay = _run(tmp_path, monkeypatch, "composed", script)
    record = H.result_json(replay)
    entry = _item(replay)
    oid = entry["oid"]
    # Declaration: one bounded call to the owning lead, read-only, no slot, no planner call.
    (declared,) = replay.of("proof-binding")
    assert declared.task == "t1" and Path(declared.cwd).resolve() != replay.project.resolve(), "a source copy"
    assert "Do not change files" in declared.prompt and oid in declared.prompt
    (decl,) = record["done_declarations"]
    assert decl["owner"] == "t1" and decl["oids"] == [oid] and decl["outcome"] == "declared"
    assert decl["settled"] == [oid] and decl["held"] == [] and decl["reply_artifact"]
    assert len(replay.of("orchestrator")) == 2, "no planner call between DONE and the audit"
    assert [c.task for c in replay.of("lead")] == ["t1"], "no task slot and no editing call"
    (gate,) = record["done_gates"]
    assert gate["audit_asked"] is True and "asked t1" in gate["declaration"]
    (admitted,) = [a for a in record["plan_admissions"] if a["scope"] == "done-declaration"]
    assert admitted["feasible"]
    # The restatement settled on the receipt already on record: no suite ran for it.
    (restated,) = entry["restatements"]
    assert restated["role"] == "proof-binding" and restated["cases"] == [CASE]
    assert len(record["checks"]) == 1, "the declaration attached to t1's own gate run"
    # Audit: the required list names the restatement under its own heading.
    first, completion = replay.of("auditor")
    assert "and nothing else" not in first.prompt
    assert f"- executed restatements (SUFFICIENT or INSUFFICIENT): {oid}" in first.prompt
    assert "## Executed restatements awaiting your judgment" in first.prompt
    shown = first.prompt.split("## Not shown to have run", 1)
    assert len(shown) == 1 or oid not in shown[1].split("\n## ", 1)[0], "never listed as not run"
    # Completion: the same seat, only the omitted id, nothing re-asked.
    assert completion.vendor == first.vendor and completion.model == first.model
    assert required_lines(completion.prompt) == {"restatement": [oid]}
    assert "none of its answers is asked again" in completion.prompt
    audit = record["requirements"]["audits"][-1]
    assert audit["omitted"] == [oid] and set(audit["required"]) >= {"R1", oid}
    assert audit["required"] == [i for ids in required_lines(first.prompt).values() for i in ids], \
        "the record's required ids are exactly the prompt's list"
    assert audit["completion"]["answered"] == [oid] and audit["completion"]["outcome"] == "answered"
    (done,) = [c for c in record["judgment_completions"] if c["task"] == "audit"]
    assert done["claimed"] and done["raw_artifact"] and done["reply_artifact"] and done["role"] == "auditor"
    judged = entry["restated_judgment"]
    assert judged["verdict"] == "sufficient" and judged["by"] != restated["author"]
    from tools.truth_check import _not_run_cleared
    assert _not_run_cleared(entry, record["checks"], record), "the saved record clears it by the same rule"
    assert replay.result.completed, replay.result.error


# -- 2. an adverse audit answer is kept and goes to an editing repair -----------------------------

def test_an_adverse_audit_verdict_survives_the_completion_and_routes_to_an_editing_repair(tmp_path, monkeypatch):
    """The R13 shape: the auditor's NOT MET names a defect. The completion
    asks only the omitted restatement; a MET it volunteers for R1 is not
    read; the defect goes back to the planner, which names a repair task."""
    seen = Seen()
    audits = []

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return PLAN1
        if n == 3:
            assert "An independent audit found these requirements not met" in call.prompt
            assert "add drops negative operands" in call.prompt
            return PLAN1.replace("Implement add in app.py.", "Repair add for negative operands.")
        return "DONE"

    def auditor(call, replay):
        audits.append(call)
        if len(audits) <= 2:
            return _auditor(lambda c, r: ADVERSE, complete=f"{SUFFICIENT}\nR1: MET - {CASE}")(call, replay)
        return _answering(call, replay)
    script = _script(seen, collaborator=_clean, orchestrator=orchestrator, lead=_lead(), auditor=auditor)
    script.overrides["proof-binding"] = _declaring()
    replay = _run(tmp_path, monkeypatch, "adverse", script, max_tasks=3)
    record = H.result_json(replay)
    first = record["requirements"]["audits"][0]
    assert first["verdicts"]["R1"]["met"] is False and "negative operands" in first["verdicts"]["R1"]["why"]
    (oid,) = [e["oid"] for e in record["unexecuted_acceptance"] if e["item"] == "app.py" and e["task"] == "t1"]
    assert first["completion"]["asked"] == [oid] and first["completion"]["dropped_lines"] == 1
    assert "R1" not in first["omitted"], "an answered requirement is never asked again"
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "the defect went to an editing repair"
    assert not any("It gave no line for:" in c.prompt for c in audits[2:]), "one completion for the job"


# -- 3. no allowance, no completion, no MET -------------------------------------------------------

def test_with_the_shared_allowance_spent_an_omitted_restatement_stays_held(tmp_path, monkeypatch):
    """A review completion spends the job's one judgment completion; the
    audit's omission then gets none and nothing is inferred from R1's MET."""
    seen = Seen()
    reviews = []

    def collaborator(call, replay):
        reviews.append(call)
        return "I read app.py." if len(reviews) == 1 else CLEAN

    script = _script(seen, orchestrator=_then_done(), lead=_lead(), collaborator=collaborator, auditor=_auditor())
    script.overrides["proof-binding"] = _declaring()
    replay = _run(tmp_path, monkeypatch, "spent", script)
    record = H.result_json(replay)
    assert [c["task"] for c in record["judgment_completions"]][0] == "t1", "the review spent the allowance"
    audit = record["requirements"]["audits"][0]
    assert audit["omitted"] == [_item(replay)["oid"]]
    assert audit["completion"]["outcome"].startswith("not attempted: the job's allowance is spent")
    assert not any("It gave no line for:" in c.prompt for c in replay.of("auditor"))
    assert _item(replay).get("restated_judgment") is None
    assert audit["verdicts"]["R1"]["met"] is False
    assert not replay.result.completed


# -- 4. a mixed blocker set keeps the edit route; a refused declaration stays held -----------------

def test_an_item_beside_skipped_cases_keeps_the_edit_route_and_asks_no_declaration(tmp_path, monkeypatch):
    """A skip fact holds R1 beside the production-path item: only execution
    settles skipped cases, so the set is mixed and no declaration is bought."""
    seen = Seen()
    script = _script(seen, collaborator=_clean, orchestrator=_then_done(), lead=_lead(tests=SKIP_TESTS),
                     auditor=_answering)
    script.overrides["proof-binding"] = _declaring(NEGATIVE)
    replay = _run(tmp_path, monkeypatch, "mixed", script)
    record = H.result_json(replay)
    assert record["done_declarations"] == [] and replay.of("proof-binding") == []
    gate = record["done_gates"][0]
    assert gate["audit_asked"] is False and "only execution settles them" in gate["declaration"]
    assert gate["continuation"]["route"] == "write-granted"
    assert not replay.result.completed


@pytest.mark.parametrize("name, reply, problem", [
    ("unrelated", lambda oid: f"RUNS: O99 - {CASE}", "not a listed id's line"),
    ("unnamed", lambda oid: f"RUNS: - {CASE}\nRUNS: {oid} - {CASE}", "names no id"),
    ("duplicate", lambda oid: f"RUNS: {oid} - {CASE}\nRUNS: {oid} - {CASE}", "declared twice"),
    ("absent", lambda oid: f"RUNS: {oid} - tests/test_app.py::test_absent", "not defined where it says"),
])
def test_a_declaration_reply_the_reader_refuses_settles_nothing(tmp_path, monkeypatch, name, reply, problem):
    seen = Seen()

    def answer(call, replay):
        return reply(DECLARED.search(call.prompt).group(1))
    script = _script(seen, collaborator=_clean, orchestrator=_then_done(), lead=_lead(), auditor=_answering)
    script.overrides["proof-binding"] = answer
    replay = _run(tmp_path, monkeypatch, name, script)
    record = H.result_json(replay)
    (decl,) = record["done_declarations"]
    assert decl["held"] == [_item(replay)["oid"]] and not _item(replay).get("restatements")
    assert any(problem in p["problem"] for p in record["proof_binding_problems"]), record["proof_binding_problems"]
    if name == "unnamed":
        assert decl["outcome"].startswith("unparseable")
    # Held, sent back while a continuation is admissible; the repeated DONE
    # over the same state asks no one again and goes to the audit.
    assert len(replay.of("proof-binding")) == 1, "one attempt per owner for the whole job"
    first, second = record["done_gates"][:2]
    assert first["audit_asked"] is False and "still held: R1" in first["declaration"]
    assert second["audit_asked"] is True and "already asked" in second["declaration"]
    assert not replay.result.completed


def test_a_source_change_during_the_declaration_voids_it(tmp_path, monkeypatch):
    seen = Seen()

    def answer(call, replay):
        (replay.project / "notes.txt").write_text("changed while the declaration ran\n")
        return f"RUNS: {DECLARED.search(call.prompt).group(1)} - {CASE}"
    script = _script(seen, collaborator=_clean, orchestrator=_then_done(), lead=_lead(), auditor=_answering)
    script.overrides["proof-binding"] = answer
    replay = _run(tmp_path, monkeypatch, "moved", script)
    record = H.result_json(replay)
    (decl,) = record["done_declarations"]
    assert decl["outcome"].startswith("void: source") and not _item(replay).get("restatements")
    assert "the source moved during a declaration" in record["done_gates"][0]["why"]


# -- 5. priced at the reserve, never at the write-granted ceiling ---------------------------------

PER_CALL, RESERVE, CEILING, HELD = 110, 100, 300, 200


def _limits(tokens):
    return RunLimits(max_calls=120, max_reported_tokens=tokens, wall_seconds=600, max_concurrent_workers=2,
                     reserve_tokens_per_call=RESERVE, max_tokens_per_call=CEILING)


def test_a_declaration_is_admitted_at_the_reserve_where_a_write_granted_continuation_is_not(tmp_path, monkeypatch):
    """Every call reports 110; the reserve is 100 and the write-granted
    ceiling 300. With 250 spendable at DONE, the declaration (100) is
    admitted; once it settles nothing, the continuation (planning 110, lead
    300, review 100) is quoted infeasible and the audit is asked instead."""
    def build():
        script = _script(Seen(), collaborator=_clean, orchestrator=_then_done(), lead=_lead(), auditor=_answering)
        script.overrides["proof-binding"] = lambda c, r: "No existing case exercises it."
        return script
    base = _run(tmp_path, monkeypatch, "calibrate", build(), limits=_limits(6_000_000))
    budget = json.loads((Path(base.result.run_dir) / "budget.json").read_text())
    assert budget["reported_tokens"] == PER_CALL * budget["reserved_attempts"], "every call reports 110"
    before = next(i for i, c in enumerate(base.calls) if c.role == "proof-binding")
    replay = _run(tmp_path, monkeypatch, "limited", build(), limits=_limits(PER_CALL * before + HELD + 250))
    assert [c.role for c in replay.calls[:before]] == [c.role for c in base.calls[:before]], "the same calls first"
    record = H.result_json(replay)
    (admitted,) = [a for a in record["plan_admissions"] if a["scope"] == "done-declaration"]
    assert admitted["feasible"] and admitted["needed"]["tokens"] == RESERVE, admitted
    assert len(replay.of("proof-binding")) == 1
    (gate, *_rest) = record["done_gates"]
    assert gate["continuation"]["feasible"] is False and gate["continuation"]["route"] == "write-granted"
    assert gate["audit_asked"] is True


def test_a_later_source_makes_a_new_decision_for_the_same_owner_and_it_is_asked_once(tmp_path, monkeypatch):
    """Root's correction on 9c17005 (PR73 6716888092): t1's lead answered
    nothing at the first DONE; t2 then changed the source. The same item on
    the new source is a new decision, asked once and settled; the first
    state was never asked again."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED
    seen, answers = Seen(), []

    def lead(call, replay):
        if call.task == "t1":
            return _lead()(call, replay)
        H.write(call, {"app.py": FIXED + "\n# t2: documented the sum\n"})
        return f"Documented add.\nPROVES: P1 - {CASE}\nCHANGED: [\"app.py\"]"

    def answer(call, replay):
        answers.append(call)
        oid = DECLARED.search(call.prompt).group(1)
        return "No existing case exercises it." if len(answers) == 1 else f"RUNS: {oid} - {CASE}"
    repair = PLAN1.replace("Implement add in app.py.", "Document the sum in app.py.")
    script = _script(seen, collaborator=_clean, orchestrator=_then_done(PLAN1, "DONE", repair), lead=lead,
                     auditor=_auditor())
    script.overrides["proof-binding"] = answer
    replay = _run(tmp_path, monkeypatch, "later", script, max_tasks=3)
    record = H.result_json(replay)
    first, second = record["done_declarations"]
    assert first["owner"] == second["owner"] == "t1" and first["oids"] == second["oids"]
    assert first["source"] != second["source"] and first["decision"] != second["decision"]
    assert first["outcome"] == "no line for a listed id" and second["outcome"] == "declared"
    assert len(replay.of("proof-binding")) == 2 and [c.task for c in replay.of("lead")] == ["t1", "t2"]
    assert replay.result.completed, replay.result.error
