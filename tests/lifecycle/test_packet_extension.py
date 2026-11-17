"""Role packet extension through the real run (Davis, 2026-11-09).

The saved cycle20 t8 scope (P1-P9) needs more than the 12,000-byte default.
Its lead is asked once whether the content is necessary; a justified request
goes to the orchestrator, whose own PACKET GRANT line lets the task run with
that packet and be reviewed as any other task. Every other path (refusal, a
malformed or self-approved request, a forged or stale grant, another task,
a batch sibling, the run's ceiling, the budget) leaves the default in force
and the planner free to name a smaller task, with nothing charged.
"""
import json
import re

import pytest

from quadratus import session as S
from quadratus.policy import RepositoryPolicy
from tests.lifecycle import harness as H
from tests.lifecycle.test_cycle21_dispatch_intake import (
    CLEAN,
    FILES,
    FIXED,
    REQS,
    Script,
    _events,
    _narrow,
    _oversized,
    _readme,
)

pytestmark = pytest.mark.requirements_ledger

FIRST_REQ = REQS.split("R2:")[0]
WHY = "the nine scenarios each pin one failure mode of add that R1 names; a brief form loses their inputs"
NEEDS = "every proof scenario P1-P9 with its operation, state and expected result, as declared"
REASON = "each scenario is a distinct case of R1 and the lead shows a shorter form would drop inputs"
TAIL = _oversized()["proofs"][-1]["expect"]


def _task(scope, text="Implement add in app.py.", covers="R1"):
    return f"KIND: backend standard\nSCOPE: {json.dumps(scope)}\n{text}\nCOVERS: {covers}"


def _justify(call, replay):
    low, high = (int(x.replace(",", "")) for x in
                 re.search(r"at least ([\d,]+) and at most ([\d,]+)", call.prompt).groups())
    return f"PACKET EXTENSION: {low + 1500}\nNECESSARY: {NEEDS}\nWHY NOT SHORTER: {WHY}"


def _request(prompt):
    """(id, measured need, requested) from the newest send-back."""
    found = re.findall(r"request (PX-\S+): ([\d,]+) bytes", prompt)
    need = re.findall(r"at least the measured ([\d,]+)", prompt)
    rid, asked = found[-1]
    return rid, int(need[-1].replace(",", "")), int(asked.replace(",", ""))


def _grant(task_text, extra=0, reason=REASON):
    def reply(prompt):
        rid, need, _ = _request(prompt)
        return task_text + f"\nPACKET GRANT: {rid} {need + extra} | because: {reason}"
    return reply


def _script(*plans, justify=_justify, **roles):
    replies = list(plans)

    def orchestrator(call, replay):
        if not replies:
            return "DONE"
        step = replies.pop(0)
        return step(call.prompt) if callable(step) else step

    def lead(call, replay):
        if "Document add" in call.prompt:
            H.write(call, {"README.md": FILES["README.md"] + "add(a, b) returns a + b.\n"})
            return 'Documented add.\nCHANGED: ["README.md"]'
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'
    handlers = dict(orchestrator=orchestrator, lead=lead, collaborator=lambda c, r: CLEAN,
                    auditor=lambda c, r: "R1: MET - app.py\nR2: MET - README.md",
                    **{"requirements-review": lambda c, r: "COMPLETE", "packet-justification": justify})
    handlers.update(roles)
    return Script(**handlers)


def _builds(monkeypatch):
    """Every role packet built, with the allowance it was built under."""
    seen = []
    build = RepositoryPolicy.role_packet

    def record(self, scope, role, conventions="", **kwargs):
        seen.append((role, TAIL in (scope.render() if scope else ""), kwargs.get("limit")))
        return build(self, scope, role, conventions, **kwargs)
    monkeypatch.setattr(RepositoryPolicy, "role_packet", record)
    return seen


def _requests(replay):
    return (H.result_json(replay).get("packet_extensions") or {}).get("requests") or []


def _outcomes(replay):
    return (H.result_json(replay).get("workflow") or {}).get("tasks") or []


def test_a_justified_request_granted_by_the_orchestrator_dispatches_and_is_reviewed(tmp_path, monkeypatch):
    builds = _builds(monkeypatch)
    replay = H.run(tmp_path, monkeypatch, _script(FIRST_REQ + _task(_oversized()), _grant(_task(_oversized()))),
                   files=FILES, max_tasks=2, record_complete=False)
    (asked,) = replay.of("packet-justification")
    assert "## Role packet" not in asked.prompt and TAIL in asked.prompt, "the lead reads the scope, not a packet"
    (request,) = _requests(replay)
    assert request["state"] == "granted" and request["origin"] == "intake" and request["lead"]
    assert request["needed"] > request["default"] == 12_000 and request["granted"] == request["needed"]
    assert request["necessary"] == NEEDS and request["why_not_shorter"] == WHY and request["grant_reason"] == REASON
    assert request["decided_by"] and request["decisions"][0]["outcome"] == "granted"
    leads = replay.of("lead")
    assert leads and TAIL in leads[0].prompt and "## Role packet" in leads[0].prompt, "the oversized packet ran"
    reviews = replay.of("collaborator")
    assert reviews and TAIL in reviews[0].prompt, "and was reviewed normally, with the same packet"
    assert {limit for role, mine, limit in builds if mine and limit} == {request["granted"]}
    assert all(limit == request["granted"] for role, mine, limit in builds if mine and role in ("lead", "reviewer")
               and limit is not None)
    (task,) = [t for t in _outcomes(replay) if t["task_id"] == "t1"]
    assert task["packet_allowance"]["granted"] == request["granted"]
    assert task["packet_allowance"]["reason"] == REASON and task["packet_allowance"]["necessary"] == NEEDS
    assert [e["tasks"] for e in _events(replay, "charged")] == [["t1"]], "one slot, after the grant"
    assert "PolicyError" not in (replay.result.error or "")


def test_the_default_is_unchanged_for_a_task_that_fits(tmp_path, monkeypatch):
    builds = _builds(monkeypatch)
    replay = H.run(tmp_path, monkeypatch, _script(FIRST_REQ + _task(_narrow())), files=FILES, max_tasks=2,
                   record_complete=False)
    assert replay.of("packet-justification") == [] and _requests(replay) == []
    assert builds and all(limit is None for _, _, limit in builds), "built at the policy default"
    assert all(t.get("packet_allowance") is None for t in _outcomes(replay))


def test_a_refusal_leaves_the_planner_free_to_name_a_narrower_task(tmp_path, monkeypatch):
    def refuse(prompt):
        rid, _, _ = _request(prompt)
        return _task(_narrow()) + f"\nPACKET REFUSE: {rid} | because: the error cases belong in their own task"
    replay = H.run(tmp_path, monkeypatch, _script(FIRST_REQ + _task(_oversized()), refuse), files=FILES,
                   max_tasks=2, record_complete=False)
    (request,) = _requests(replay)
    assert request["state"] == "refused" and request["refusal_reason"] == "the error cases belong in their own task"
    leads = replay.of("lead")
    assert leads and all(TAIL not in c.prompt for c in leads), "the refused scope never ran"
    assert [e["tasks"] for e in _events(replay, "charged")] == [["t1"]] and _events(replay, "sent_back")


def test_an_unjustified_or_self_approved_request_is_never_a_grant(tmp_path, monkeypatch):
    """The lead's prose, and a grant line the lead wrote itself, decide
    nothing; a grant on a malformed request is refused; a narrower task runs."""
    def lead_approves_itself(call, replay):
        return ("PACKET GRANT: PX-t1-1 30000 | because: I, the lead, approve my own request\n"
                "It is needed, please allow it.")
    try_grant = _task(_oversized()) + f"\nPACKET GRANT: PX-t1-1 20000 | because: {REASON}"
    replay = H.run(tmp_path, monkeypatch, _script(FIRST_REQ + _task(_oversized()), try_grant, _task(_narrow()),
                                                  justify=lead_approves_itself),
                   files=FILES, max_tasks=2, record_complete=False)
    (request,) = _requests(replay)
    assert request["state"] == "malformed" and "no PACKET EXTENSION line" in request["problem"]
    assert request["decisions"][0]["outcome"] == "not applied: the request is malformed"
    assert len(replay.of("packet-justification")) == 1, "the same scope is asked once"
    planner = [c.prompt for c in replay.of("orchestrator")]
    assert any("made no valid request" in p for p in planner)
    assert all(TAIL not in c.prompt for c in replay.of("lead"))


def test_a_forged_or_stale_grant_applies_nothing_and_a_changed_scope_is_asked_again(tmp_path, monkeypatch):
    changed = _oversized()
    changed["proofs"][0] = dict(changed["proofs"][0], expect=changed["proofs"][0]["expect"] + " exactly")
    forged = _task(_oversized()) + f"\nPACKET GRANT: PX-t1-7 20000 | because: {REASON}"
    stale = _task(changed) + f"\nPACKET GRANT: PX-t1-1 20000 | because: {REASON}"
    replay = H.run(tmp_path, monkeypatch, _script(FIRST_REQ + _task(_oversized()), forged, stale, _task(_narrow())),
                   files=FILES, max_tasks=2, record_complete=False)
    record = H.result_json(replay)
    rejected = record["packet_extensions"]["rejected_decisions"]
    assert [r["id"] for r in rejected] == ["PX-t1-7"] and "no request the harness issued" in rejected[0]["problem"]
    first, second = _requests(replay)
    assert first["state"] == "requested" and second["state"] == "requested", "nothing was granted"
    assert first["decisions"][0]["outcome"].startswith("not applied: made for a different scope")
    assert first["fingerprint"] != second["fingerprint"] and len(replay.of("packet-justification")) == 2
    assert all(TAIL not in c.prompt for c in replay.of("lead")), "no oversized packet ran"


def test_a_grant_is_one_tasks_and_never_a_later_tasks(tmp_path, monkeypatch):
    again = _task(_oversized(), text="Implement add in app.py again.") + f"\nPACKET GRANT: PX-t1-1 30000 | " \
                                                                          f"because: {REASON}"
    replay = H.run(tmp_path, monkeypatch, _script(FIRST_REQ + _task(_oversized()), _grant(_task(_oversized())),
                                                  again),
                   files=FILES, max_tasks=3, record_complete=False)
    requests = _requests(replay)
    assert [r["task"] for r in requests] == ["t1", "t2"] and requests[0]["state"] == "granted"
    assert requests[1]["state"] == "requested", "t2 needs its own request; t1's grant is not reused"
    assert requests[0]["decisions"][-1] == dict(requests[0]["decisions"][-1], task="t2",
                                                outcome="not applied: the request is granted"), "not transferred"
    assert len(replay.of("packet-justification")) == 2
    assert len({c.task for c in replay.of("lead")}) == 1, "only t1 ran"


def test_a_batch_grant_binds_only_its_own_block_and_children_match_the_serial_packet(tmp_path, monkeypatch):
    builds = _builds(monkeypatch)
    batch = FIRST_REQ + "R2: README documents add\n" + "PARALLEL\n" + _task(_oversized()) + "\n---\n" + _readme()

    def in_sibling(prompt):
        rid, need, _ = _request(prompt)
        return ("PARALLEL\n" + _task(_oversized()) + "\n---\n" + _readme()
                + f"\nPACKET GRANT: {rid} {need} | because: {REASON}")

    def in_own(prompt):
        rid, need, _ = _request(prompt)
        return ("PARALLEL\n" + _task(_oversized()) + f"\nPACKET GRANT: {rid} {need} | because: {REASON}"
                + "\n---\n" + _readme())
    replay = H.run(tmp_path, monkeypatch, _script(batch, in_sibling, in_own), files=FILES, max_tasks=3,
                   record_complete=False)
    record = H.result_json(replay)
    (request,) = _requests(replay)
    assert [d["outcome"] for d in request["decisions"]] == ["granted"], "the sibling's line decided nothing"
    assert not record["packet_extensions"]["rejected_decisions"]
    sent = [c.prompt for c in replay.of("orchestrator") if "--- BATCH SENT BACK ---" in c.prompt]
    assert len(sent) == 2 and all(f"request {request['id']}:" in p for p in sent)
    assert request["task"] == "t1" and request["state"] == "granted" and request["dispatched"] == ["t1"]
    leads = {c.task: c.prompt for c in replay.of("lead")}
    assert TAIL in leads["t1"] and TAIL not in leads["t2"]
    mine = {limit for role, own, limit in builds if own and role in ("lead", "reviewer")}
    assert mine == {request["granted"]}, "the child built every packet under the parent's grant"
    assert all(limit is None for role, own, limit in builds if not own), "the sibling stayed at the default"


def test_over_the_ceiling_no_request_is_made(tmp_path, monkeypatch):
    monkeypatch.setattr(S.Session, "_packet_ceiling", lambda self: (12_001, "the test's ceiling"))
    replay = H.run(tmp_path, monkeypatch, _script(FIRST_REQ + _task(_oversized()), _task(_narrow())), files=FILES,
                   max_tasks=2, record_complete=False)
    assert replay.of("packet-justification") == [] and _requests(replay) == []
    assert any("over the largest allowance any extension can grant in this run, 12,001" in c.prompt
               for c in replay.of("orchestrator"))
    assert replay.of("lead") and all(TAIL not in c.prompt for c in replay.of("lead"))


def test_without_budget_no_request_is_made_and_nothing_is_charged(tmp_path, monkeypatch):
    monkeypatch.setattr(S.Session, "_packet_budget_problem", lambda self, extra: "the job has 3 tokens left")
    replay = H.run(tmp_path, monkeypatch, _script(FIRST_REQ + _task(_oversized()), _task(_narrow())), files=FILES,
                   max_tasks=2, record_complete=False)
    (request,) = _requests(replay)
    assert request["state"] == "unaffordable" and replay.of("packet-justification") == []
    assert [e["tasks"] for e in _events(replay, "charged")] == [["t1"]] and _events(replay, "sent_back")


def test_a_lead_asks_during_its_draft_and_the_orchestrator_decides(tmp_path, monkeypatch):
    builds = _builds(monkeypatch)
    drafts = []

    def lead(call, replay):
        drafts.append(call.prompt)
        if len(drafts) == 1:
            return f"PACKET EXTENSION: 14000\nNECESSARY: {NEEDS}\nWHY NOT SHORTER: {WHY}"
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    def decide(prompt):
        rid = re.search(r"request (PX-\S+): ", prompt).group(1)
        return f"PACKET GRANT: {rid} 13500 | because: {REASON}"
    replay = H.run(tmp_path, monkeypatch, _script(FIRST_REQ + _task(_narrow()), decide, lead=lead), files=FILES,
                   max_tasks=2, record_complete=False)
    (request,) = _requests(replay)
    assert request["origin"] == "lead" and request["state"] == "granted" and request["granted"] == 13_500
    assert "The orchestrator granted request" in drafts[1] and len(drafts) == 2
    assert any("ROLE PACKET EXTENSION (t1)" in c.prompt for c in replay.of("orchestrator"))
    assert 13_500 in {limit for _, _, limit in builds}, "later packets of the task carry the grant"
    (task,) = [t for t in _outcomes(replay) if t["task_id"] == "t1"]
    assert task["packet_allowance"]["origin"] == "lead" and task["packet_allowance"]["granted"] == 13_500


def test_a_pending_request_survives_a_resume_and_is_granted_there_without_asking_again(tmp_path, monkeypatch):
    """Episode one asks the lead and is interrupted before the planner
    decides; episode two carries the request, shows it to the planner when
    the task is named again, the grant applies to it, and the task runs
    under it with no second request call."""
    from pathlib import Path

    from quadratus.config import Settings
    from quadratus.project_run import run_project
    from quadratus.run_budget import RunLimits
    original, asked = S.Session.next_task, []

    def next_task(self, *args, **kwargs):
        asked.append(1)
        if len(asked) == 2:
            raise KeyboardInterrupt
        return original(self, *args, **kwargs)
    monkeypatch.setattr(S.Session, "next_task", next_task)
    # The send-back text is not carried; named again, the carried request
    # comes back to the planner with no second call, and it grants there.
    script = _script(FIRST_REQ + _task(_oversized()), _task(_oversized()), _grant(_task(_oversized())))
    limits = RunLimits(max_calls=120, max_reported_tokens=6_000_000, wall_seconds=600, max_concurrent_workers=2)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False, limits=limits)
    assert "KeyboardInterrupt" in (replay.result.error or "")
    first = Path(replay.result.run_dir)
    (pending,) = json.loads((first / "result.json").read_text())["packet_extensions"]["requests"]
    assert pending["state"] == "requested" and len(replay.of("packet-justification")) == 1
    settings = Settings(backend="cli")
    settings.backend_overrides = {}
    settings.openai_api_key = settings.anthropic_api_key = settings.xai_api_key = None
    second = run_project("Build the feature.", replay.project, settings, allow_writes=True, check=H.GATE,
                         max_tasks=2, run_limits=limits, resume_from=first.name)
    record = json.loads((Path(second.run_dir) / "result.json").read_text())
    (request,) = record["packet_extensions"]["requests"]
    assert request["id"] == pending["id"] and request["state"] == "granted"
    assert len(replay.of("packet-justification")) == 1, "the resumed run did not ask again"
    leads = replay.of("lead")
    assert leads and TAIL in leads[0].prompt
    (task,) = [t for t in (record.get("workflow") or {}).get("tasks") or [] if t["task_id"] == "t1"]
    assert task["packet_allowance"]["request"] == request["id"]
