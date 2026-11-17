"""Role packet extension units (Davis, 2026-11-09).

The policy's 12,000-byte default stays. A task may carry a larger packet
only under an allowance the orchestrator granted on its lead's justified
request, bounded by the run's own ceiling, bound to the task and the exact
scope the request measured, and kept on the record.
"""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadratus import checkpoint
from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.policy import PacketTooLarge, load_policy
from quadratus.scope import read_scope
from quadratus.session import Session, SessionConfig, TaskSpec

SAVED = (Path(__file__).parent / "lifecycle" / "cycle20_t8_scope.json").read_text()
WHY = "the nine scenarios each pin one failure mode the requirement names, and a brief form drops the inputs"
NEEDS = "every proof scenario with its operation, state and expectation as declared, one per failure mode"


def _project(tmp_path):
    root = tmp_path / "p"
    for name in ("README.md", "tests/test_cli_workflows.py", "store.py"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x\n")
    return root


def _scope(data=SAVED):
    return read_scope("KIND: test simple\nSCOPE: " + data + "\nbody")[0]


def _session(tmp_path, invoke=None, **config):
    root = _project(tmp_path)
    return Session("g", ArtifactStore(tmp_path / "a"), invoke or (lambda *a, **k: ""),
                   config=SessionConfig(project=root, allow_writes=True, requirements_ledger=True,
                                        repository_policy=load_policy(root), **config))


def _spec(scope=None, task_id="t1"):
    return TaskSpec(task_id=task_id, description="Do the work.", kind="backend", complexity="simple",
                    scope=scope or _scope())


def _request(size, necessary=NEEDS, why=WHY):
    return f"PACKET EXTENSION: {size}\nNECESSARY: {necessary}\nWHY NOT SHORTER: {why}"


def _needed(session, spec):
    policy = session.config.repository_policy
    return max(policy.packet_needed(policy.scope(spec.scope), r) for r in session._packet_roles(spec))


def _granted(session, spec, size=None, **extra):
    needed = _needed(session, spec)
    request = dict(id=f"PX-{spec.task_id}-1", task=spec.task_id, origin="intake",
                   fingerprint=session._packet_fingerprint(spec), needed=needed, default=12_000,
                   requested=size or needed, state="granted", granted=size or needed, decisions=[], revisions=[],
                   grant_reason="the scenarios are the requirement", decided_by="orch", **extra)
    session.packet_requests.append(request)
    return request


# -- the default and the builder ------------------------------------------------------------------

def test_the_default_is_unchanged_and_a_given_allowance_builds_the_same_contract(tmp_path):
    policy = load_policy(_project(tmp_path))
    assert policy.packet_limit == 12_000
    scope = policy.scope(_scope())
    with pytest.raises(PacketTooLarge) as info:
        policy.role_packet(scope, "lead")
    assert info.value.needed == policy.packet_needed(scope, "lead")
    packet = policy.role_packet(scope, "lead", limit=info.value.needed)
    assert scope.render() in packet and len(packet.encode()) <= info.value.needed + 400
    small = replace(scope, proofs=scope.proofs[:2])
    assert policy.role_packet(small, "lead") == policy.role_packet(small, "lead", limit=None)


def test_the_policy_clamp_bounds_the_default_only(tmp_path):
    policy = load_policy(_project(tmp_path))
    policy.document.setdefault("context", {})["reference_bytes_limit"] = 50_000
    assert policy.packet_limit == 24_000, "a repository policy still cannot set its default past 24,000"
    scope = policy.scope(_scope())
    packet = policy.role_packet(scope, "lead", "n" * 40_000, limit=30_000)
    exempt = len(scope.render().encode()) - len(scope.render(include_forbidden=False).encode())
    assert 24_000 < len(packet.encode()) <= 30_000 + exempt, "an approved allowance is not cut back to the clamp"


# -- the ceiling ----------------------------------------------------------------------------------

def _budgeted(session, per_call):
    session._budget = lambda: SimpleNamespace(limits=SimpleNamespace(max_tokens_per_call=per_call))
    return session


def test_the_ceiling_comes_from_the_per_call_threshold_and_turn_cap(tmp_path):
    canonical = _budgeted(_session(tmp_path / "a"), 500_000)
    ceiling, basis = canonical._packet_ceiling()
    assert ceiling == 100_000 and "500,000-token per-call threshold" in basis and "8 rounds" in basis
    capped = _budgeted(_session(tmp_path / "b", lead_max_turns=20), 500_000)
    assert capped._packet_ceiling()[0] == 40_000
    bare = _session(tmp_path / "c")
    assert bare._packet_ceiling()[0] == 120_000 and "60,000-token context" in bare._packet_ceiling()[1]
    tight = _budgeted(_session(tmp_path / "d", lead_max_turns=1_000), 500_000)
    assert tight._packet_ceiling()[0] == 12_000, "never below the default"


# -- the lead's request and the orchestrator's lines ---------------------------------------------

def test_a_request_is_read_by_its_markers_only():
    assert S._parse_packet_request(_request("15,500")) == dict(kind="request", bytes=15_500, necessary=NEEDS, why=WHY)
    narrow = S._parse_packet_request("PACKET NARROW: split P1-P4 and P5-P9 into two tasks, each with its rows")
    assert narrow["kind"] == "narrow"
    for reply, problem in [
            ("The packet is needed, please grant it.", "no PACKET EXTENSION line"),
            (_request(15_000, why="needed"), "WHY NOT SHORTER line is empty"),
            (_request(15_000, why="it is long"), "says too little"),
            (_request(15_000) + "\nPACKET NARROW: split it into two tasks with their rows", "both"),
            (_request(15_000) + "\nPACKET EXTENSION: 16000", "more than one"),
            ("PACKET EXTENSION: 15000\nWHY NOT SHORTER: " + WHY, "no NECESSARY line"),
            (_request(0), "not a positive number")]:
        parsed = S._parse_packet_request(reply)
        assert parsed["kind"] == "malformed" and problem in parsed["problem"], (reply, parsed)


def test_decision_lines_are_taken_out_of_the_task_text():
    spec = _spec()
    spec = replace(spec, description="Implement it.\nPACKET GRANT: PX-t1-1 15000 | because: the scenarios are needed"
                                     "\nPACKET REFUSE: PX-t1-2 | because: narrow it\nmore text")
    decisions, out = S._read_packet_decisions(spec)
    assert [(d["verb"], d["id"]) for d in decisions] == [("GRANT", "PX-t1-1"), ("REFUSE", "PX-t1-2")]
    assert out.description == "Implement it.\nmore text"
    assert S._read_packet_decisions(_spec()) == ([], _spec())


def _decide(session, spec, line, **request):
    needed = _needed(session, spec)
    entry = dict(id="PX-t1-1", task="t1", origin="intake", fingerprint=session._packet_fingerprint(spec),
                 needed=needed, requested=needed + 1_000, state="requested", decisions=[], revisions=[])
    entry.update(request)
    session.packet_requests.append(entry)
    decisions, _ = S._read_packet_decisions(replace(spec, description=line))
    return entry, session._packet_decide(spec, session._packet_fingerprint(spec), needed, decisions), needed


def test_a_grant_within_the_measured_need_and_the_request_is_applied(tmp_path):
    session, spec = _session(tmp_path), _spec()
    needed = _needed(session, spec)
    entry, problems, _ = _decide(session, spec, f"PACKET GRANT: PX-t1-1 {needed + 500} | because: the "
                                                "scenarios are the requirement's own cases")
    assert problems == [] and entry["state"] == "granted" and entry["granted"] == needed + 500
    assert entry["decided_by"] and session.store.get(entry["decision_artifact"]).startswith("PACKET GRANT")
    assert session._packet_allowance(spec) == needed + 500
    assert session._packet_allowance(_spec(task_id="t2")) is None, "no other task reads it"


@pytest.mark.parametrize("line, problem", [
    ("PACKET GRANT: PX-t1-9 99999 | because: the scenarios are the requirement", "no request the harness issued"),
    ("PACKET GRANT: PX-t1-1 {low} | because: the scenarios are the requirement", "below the measured"),
    ("PACKET GRANT: PX-t1-1 {high} | because: the scenarios are the requirement", "more than the lead asked"),
    ("PACKET GRANT: PX-t1-1 {needed} | because: fine", "says too little"),
    ("PACKET GRANT: PX-t1-1 {needed}", "is not 'PACKET GRANT"),
    ("PACKET GRANT: PX-t1-1 {needed} | because: the scenarios are the requirement\n"
     "PACKET REFUSE: PX-t1-1 | because: narrow it instead of growing", "more than one decision line"),
])
def test_an_invalid_or_forged_grant_changes_nothing(tmp_path, line, problem):
    session, spec = _session(tmp_path), _spec()
    needed = _needed(session, spec)
    entry, problems, _ = _decide(session, spec, line.format(low=needed - 1, high=needed + 1_001, needed=needed))
    assert entry["state"] == "requested" and session._packet_allowance(spec) is None
    assert any(problem in p for p in problems), problems


def test_a_grant_over_the_ceiling_or_for_a_changed_scope_or_task_is_refused(tmp_path):
    session, spec = _session(tmp_path), _spec()
    session._packet_ceiling = lambda: (12_500, "test")
    entry, problems, needed = _decide(session, spec, "PACKET GRANT: PX-t1-1 {} | because: the scenarios are the "
                                                     "requirement".format(_needed(session, spec)))
    assert entry["state"] == "requested" and "over this run's ceiling" in problems[0]
    session = _session(tmp_path / "s")
    changed = replace(spec.scope, intended_result=spec.scope.intended_result + " and more")
    entry, problems, _ = _decide(session, replace(spec, scope=changed), "PACKET GRANT: PX-t1-1 20000 | because: the "
                                 "scenarios are the requirement", fingerprint=session._packet_fingerprint(spec))
    assert entry["state"] == "requested" and "different scope" in problems[0]
    assert entry["decisions"][0]["outcome"].startswith("not applied")
    session = _session(tmp_path / "t")
    entry, problems, _ = _decide(session, spec, "PACKET GRANT: PX-t1-1 20000 | because: the scenarios are the "
                                                "requirement", task="t2")
    assert entry["state"] == "requested" and "another task" in problems[0]


def test_a_refusal_is_recorded_with_its_reason(tmp_path):
    session, spec = _session(tmp_path), _spec()
    entry, problems, _ = _decide(session, spec, "PACKET REFUSE: PX-t1-1 | because: split the error cases out")
    assert problems == [] and entry["state"] == "refused" and entry["refusal_reason"] == "split the error cases out"
    assert "this exact scope is not asked again" in session._packet_request_text(entry)


# -- intake: the request, its call and its send-back ---------------------------------------------

def test_intake_asks_the_lead_once_and_the_planner_decides(tmp_path):
    calls = []

    def invoke(key, prompt, **k):
        calls.append(prompt)
        return _request(20_000)
    session = _session(tmp_path, invoke=invoke)
    session._pick_lead = lambda spec: "claude:opus"
    text = session._packet_problem(_spec())
    assert len(calls) == 1 and "ROLE PACKET EXTENSION REQUEST (t1)" in calls[0]
    assert "## Role packet" not in calls[0], "the request call carries no role packet"
    assert "PACKET GRANT: PX-t1-1 <bytes> | because:" in text and "NECESSARY, in the lead's words" in text
    (request,) = session.packet_requests
    assert request["state"] == "requested" and request["requested"] == 20_000 and request["lead"] == "claude:opus"
    assert session._dispatch_lead[:2] == ("t1", "claude:opus"), "dispatch uses the lead that justified"
    again = session._packet_problem(_spec())
    assert len(calls) == 1 and "PX-t1-1" in again, "the same scope is not asked twice"
    decisions, spec = S._read_packet_decisions(replace(_spec(), description="Do the work.\nPACKET GRANT: PX-t1-1 "
                                                       "18000 | because: the scenarios are the requirement"))
    assert session._packet_problem(spec, decisions) == "" and session._packet_allowance(spec) == 18_000
    assert len(calls) == 1


def test_intake_over_the_ceiling_or_without_budget_makes_no_call(tmp_path):
    calls = []
    session = _session(tmp_path, invoke=lambda *a, **k: calls.append(a) or "")
    session._packet_ceiling = lambda: (12_500, "the test's ceiling")
    text = session._packet_problem(_spec())
    assert calls == [] and "over the largest allowance any extension can grant in this run, 12,500" in text
    session = _session(tmp_path / "b", invoke=lambda *a, **k: calls.append(a) or "")
    session._packet_budget_problem = lambda extra: "the job has 3 tokens left"
    text = session._packet_problem(_spec())
    assert calls == [] and "No extension can be requested for it (the job has 3 tokens left)" in text
    assert session.packet_requests[0]["state"] == "unaffordable"


def test_a_listed_task_has_nobody_to_grant_and_makes_no_call(tmp_path):
    calls = []
    session = _session(tmp_path, invoke=lambda *a, **k: calls.append(a) or "")
    session._explicit_tasks = ["one"]
    assert "no orchestrator to grant" in session._packet_problem(_spec()) and calls == []


def test_the_budget_estimate_counts_calls_and_the_resent_bytes(tmp_path):
    session = _session(tmp_path)

    def snap(**over):
        data = dict(limits=dict(max_calls=60, max_reported_tokens=1_000_000, reserve_tokens_per_call=200_000,
                                max_tokens_per_call=500_000),
                    reserved_attempts=10, reported_tokens=0, finish_protection=dict(pending=["audit"]))
        data.update(over)
        return data
    session._budget = lambda: SimpleNamespace(snapshot=lambda: snap())
    assert session._packet_budget_problem(4_000) == ""
    session._budget = lambda: SimpleNamespace(snapshot=lambda: snap(reported_tokens=590_000))
    assert "do not fit" in session._packet_budget_problem(4_000)
    session._budget = lambda: SimpleNamespace(snapshot=lambda: snap(reserved_attempts=58))
    assert "ordinary call(s) left" in session._packet_budget_problem(4_000)


# -- the grant in force: lookup, corrections, resume ---------------------------------------------

def test_a_grant_is_bound_to_its_task_and_measured_scope(tmp_path):
    session, spec = _session(tmp_path), _spec()
    _granted(session, spec, 16_000)
    assert session._packet_allowance(spec) == 16_000
    assert session._role_packet(spec, "reviewer"), "every role builds under it"
    assert session._packet_allowance(_spec(task_id="t2")) is None
    data = json.loads(SAVED)
    data["proofs"] = data["proofs"][:-1]
    assert session._packet_allowance(_spec(_scope(json.dumps(data)))) is None, "a changed scope is a new request"


def test_a_reviewed_correction_is_measured_against_the_grant_and_recorded(tmp_path):
    session = _session(tmp_path)
    spec = replace(_spec(), scope=session.config.repository_policy.scope(_scope()))
    grant = _granted(session, spec, _needed(session, spec) + 300)
    pid = spec.scope.proofs[0]["id"]
    changes = {"expect": spec.scope.proofs[0]["expect"] + " exactly"}
    assert session._correction_packet_problem(spec, pid, changes) == ""
    session._active_spec = spec
    corrected = session._apply_scenario_correction(spec, pid, changes, findings=["F1"], by="orch")
    (revision,) = grant["revisions"]
    assert revision["by"] == "scenario correction" and revision["findings"] == ["F1"]
    assert session._packet_allowance(corrected) == grant["granted"]
    too_much = {"expect": spec.scope.proofs[0]["expect"] + " x" * 400}
    assert "over its limit" in session._correction_packet_problem(corrected, pid, too_much)


def test_the_requests_survive_a_checkpoint_round_trip(tmp_path):
    session, spec = _session(tmp_path / "a"), _spec()
    _granted(session, spec, 17_000)
    session.packet_decisions_rejected.append(dict(task="t1", id="PX-t1-9", problem="no request"))
    state = json.loads(json.dumps(checkpoint.capture_state(session), default=str))
    resumed = _session(tmp_path / "b")
    checkpoint.restore_state(resumed, state)
    assert resumed._packet_allowance(spec) == 17_000
    assert resumed.packet_decisions_rejected[0]["id"] == "PX-t1-9"
    assert "packet_requests" in checkpoint._ADDED_LATER, "older checkpoints resume with none"


def test_the_dispatch_record_names_the_grant(tmp_path):
    session, spec = _session(tmp_path), _spec()
    grant = _granted(session, spec, 16_500, lead="claude:opus", necessary=NEEDS, why_not_shorter=WHY)
    session._outcome = SimpleNamespace(task_id="t1", packet_allowance=None)
    session._prepare_dispatch(spec)
    record = session._outcome.packet_allowance
    assert record["granted"] == 16_500 and record["necessary"] == NEEDS and record["why_not_shorter"] == WHY
    assert record["reason"] == grant["grant_reason"] and grant["dispatched"] == ["t1"]


def test_the_prompts_state_the_channel_and_the_ceiling(tmp_path):
    session = _session(tmp_path)
    text = session._scope_request()
    assert "PACKET GRANT: <id> <bytes> | because: <reason>" in text and "only your own" in text
    assert f"ceiling of {session._packet_ceiling()[0]:,} bytes" in text
    lead = session._lead_prompt(_spec(replace(_scope(), proofs=_scope().proofs[:1])), lead="claude:opus")
    assert "PACKET EXTENSION: <bytes>" in lead and "WHY NOT SHORTER" in lead and "one request per task" in lead


def test_a_new_justified_request_spends_no_correction_and_repeats_do(tmp_path):
    """The correction allowance is the run's, never reset: a run whose fourth
    task needs an extension must not stall on the protocol's own step. A
    task's first two requests are free; a third, a repeat, a malformed
    answer or an undecided request each spend one."""
    session = _session(tmp_path, invoke=lambda *a, **k: _request(30_000))
    session._pick_lead = lambda spec: "claude:opus"
    data = json.loads(SAVED)
    scopes = []
    for n in range(3):
        data["intended_result"] = data["intended_result"] + f" ({n})"
        scopes.append(_scope(json.dumps(data)))
    session._packet_problem(_spec(scopes[0]))
    assert session._packet_progress is True
    session._packet_problem(_spec(scopes[0]))
    assert session._packet_progress is False, "named again undecided: a correction"
    session._packet_problem(_spec(scopes[1]))
    assert session._packet_progress is True
    session._packet_problem(_spec(scopes[2]))
    assert session._packet_progress is False, "a third request for one task is a correction"
    other = _session(tmp_path / "o", invoke=lambda *a, **k: "it is needed")
    other._pick_lead = lambda spec: "claude:opus"
    other._packet_problem(_spec())
    assert other._packet_progress is False, "a malformed answer is a correction"
