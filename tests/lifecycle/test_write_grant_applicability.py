"""A task's write permission is bounded by its dispatch grant (map P3.4;
O-NEXT-10 A at 548da3d, Codex ruling 5865444019).

Every editing call, the lead and revision text, worker write requests and
the capped-close handling read live ``allow_writes``; the contract's
``authority.write_grant`` was recorded and never read. Effective permission
is now the dispatch grant AND the live grant, with the Fleet's run-level
grant still beneath both. A later live grant never widens a task; a live
removal is honoured. The difference is recorded once. The drift is a
synthetic write, a controller invariant: no production path changes
``allow_writes`` after dispatch, and the Fleet already refuses a read-only
run's escalation (Sol, 5865330461), so this is not a demonstrated
production write escalation.
"""

import dataclasses

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.outcome import TaskOutcome
from quadratus.scope import TaskScope
from quadratus.session import RunStalled, Session, SessionConfig, TaskSpec

LEAD = "claude:opus"
EDIT = "edit method in your role"
NO_EDIT = "This run has no edit grant"


def _session(tmp_path, calls, *, allow_writes):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "app.py").write_text("def add(a, b):\n    return 0\n")

    def invoke(key, prompt, allow_writes=False, **kw):
        calls.append(allow_writes)
        return "Done.\nCHANGED: []"
    return Session("goal", ArtifactStore(tmp_path / "a"), invoke,
                   config=SessionConfig(project=root, allow_writes=allow_writes))


def _spec():
    return TaskSpec("t1", "Implement add in app.py.", kind="refactor",
                    scope=TaskScope(permitted_paths=["app.py"], max_lines=40))


def _dispatch(session, spec):
    outcome = TaskOutcome("t1", "implementation")
    session._outcome = outcome
    session._contract = session._build_contract(spec, outcome)
    session._active_spec = spec
    session._task_before = session._capture_source()
    return dict(session._contract.authority)["write_grant"]


def _drift(session, value):
    session.config = dataclasses.replace(session.config, allow_writes=value)


# -- a later live grant never widens a task --------------------------------------------

def test_a_read_only_task_does_not_gain_writes_when_the_grant_drifts_on(tmp_path):
    calls = []
    session = _session(tmp_path, calls, allow_writes=False)
    spec = _spec()
    assert _dispatch(session, spec) == "none"
    _drift(session, True)
    session._edit(LEAD, "Revise.", role="revision")
    prompt = session._lead_prompt(spec)
    assert calls == [False], "the call is read-only, as dispatched"
    assert NO_EDIT in prompt and EDIT not in prompt
    assert session._outcome.mismatches == ["write_grant: contract 'none', legacy 'operator'"]


# -- a live removal is honoured ---------------------------------------------------------

def test_an_editing_task_loses_writes_when_the_grant_is_removed(tmp_path):
    calls = []
    session = _session(tmp_path, calls, allow_writes=True)
    spec = _spec()
    assert _dispatch(session, spec) == "operator"
    _drift(session, False)
    session._edit(LEAD, "Revise.", role="revision")
    prompt = session._lead_prompt(spec)
    assert calls == [False], "the revocation holds"
    assert NO_EDIT in prompt and EDIT not in prompt
    assert session._revision_delivery() != "Update the project using the edit method in your role instructions."
    assert session._outcome.mismatches == ["write_grant: contract 'operator', legacy 'none'"]


def test_a_worker_write_request_after_removal_is_refused(tmp_path):
    session = _session(tmp_path, [], allow_writes=True)
    _dispatch(session, _spec())
    _drift(session, False)
    import json

    from quadratus.memory import TaskMemory
    request = "WORKER " + json.dumps(dict(errand="code", instruction="Fix add.", write=True, needs=["patch"]))
    with pytest.raises(RunStalled, match="without an operator write grant"):
        session._serve_worker(request, LEAD, _spec(), TaskMemory("t1", LEAD), {})


# -- controls ---------------------------------------------------------------------------

@pytest.mark.parametrize("grant, allowed, text", [(True, True, EDIT), (False, False, NO_EDIT)])
def test_no_drift_keeps_the_dispatch_grant(tmp_path, grant, allowed, text):
    calls = []
    session = _session(tmp_path, calls, allow_writes=grant)
    spec = _spec()
    _dispatch(session, spec)
    session._edit(LEAD, "Revise.", role="revision")
    assert calls == [allowed] and text in session._lead_prompt(spec)
    assert session._outcome.mismatches == []


def test_with_no_task_contract_the_live_grant_decides(tmp_path):
    session = _session(tmp_path, [], allow_writes=True)
    assert session._writes() is True
    _drift(session, False)
    assert session._writes() is False


# -- edits made under the grant stay held to scope after a revocation ---------------------

@pytest.mark.parametrize("revoke", [False, True], ids=["granted", "revoked-during-call"])
def test_a_capped_close_holds_edits_made_under_the_grant_to_scope(tmp_path, revoke):
    """O-NEXT-15 F1 (5865760320): the scope stop keyed on the grant at close,
    so a revocation during a turn-limited call switched it off for the files
    that call had already written. It now keys on the dispatch grant."""
    from quadratus.memory import TaskMemory
    from quadratus.providers import TurnLimitReached
    from quadratus.session import PartialWorkStopped
    session = _session(tmp_path, [], allow_writes=True)
    spec = _spec()
    _dispatch(session, spec)
    (session.project / "evil.py").write_text("x = 1\n")
    if revoke:
        _drift(session, False)
    exc = TurnLimitReached("turn limit", partial_text="Still working.", turns=14)
    with pytest.raises(PartialWorkStopped, match="exceed the declared scope"):
        session._close_turn_limited(LEAD, spec, TaskMemory("t1", LEAD), exc, session._task_before)
    assert (session.project / "evil.py").exists(), "the work is preserved"


def test_a_read_only_task_is_not_held_to_a_scope_it_never_wrote_under(tmp_path):
    from quadratus.memory import TaskMemory
    from quadratus.providers import TurnLimitReached
    session = _session(tmp_path, [], allow_writes=False)
    spec = _spec()
    _dispatch(session, spec)
    _drift(session, True)
    exc = TurnLimitReached("turn limit", partial_text="Still reading.", turns=14)
    summary = session._close_turn_limited(LEAD, spec, TaskMemory("t1", LEAD), exc, session._task_before)
    assert summary is not None and session._write_ceiling() is False
