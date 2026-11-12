"""The development line's engine code held to the review batch's rules
(conformance audit of the merged tree, 2026-11-12).

Each test names the audit finding it closes. All of them run offline:
scripted replies, a fake CLI, a temporary folder, never a model.
"""

import pytest

import tests.test_review1110_compete as arena
from quadratus.providers import PermissionBlocked
from quadratus.session import _never_replaced
from tests.test_review1110_compete import _three_seat_trust  # noqa: F401 -- autouse fixture

# -- transport:1: a contest never replaces a routed lead that hit a permission block -------


def _blocked():
    return PermissionBlocked("Grok stopped: Fetch on a file target needed approval",
                             permission_block=dict(tool="Fetch", scheme="file"))


def test_a_routed_lead_stopped_on_a_permission_block_is_not_replaced_by_the_challenger(tmp_path):
    """With one lead the run stops on the block. A contest must not be a
    quiet way round it: the challenger's finished draft is not merged."""
    assert _never_replaced(_blocked())
    script = arena.Script([arena._task(), "DONE"],
                          {arena.OPUS: arena._raises(_blocked()), arena.SOL: arena._sub(), arena.GROK: arena._sub()})
    session, project = arena._session(tmp_path, script, compete="complex")
    with pytest.raises(PermissionBlocked):
        session.run(max_tasks=3)
    record = arena._held(session)
    assert record["winner_model"] is None and record["decided_by"] == arena.contest.ROUTED_STOP
    assert "def sub" not in (project / "calc.py").read_text() and not session.completed


def test_a_challenger_stopped_on_a_permission_block_only_loses_the_contest(tmp_path):
    script = arena.Script([arena._task(), "DONE"],
                          {arena.OPUS: arena._sub(), arena.SOL: arena._raises(_blocked()), arena.GROK: arena._sub()})
    session, project = arena._session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = arena._held(session)
    assert record["winner_model"] == arena.OPUS and record["decided_by"] == "completed"
    assert "def sub" in (project / "calc.py").read_text() and session.completed


# -- transport:5: a replay refused by the finish hold ends in the call's own failure -------

_503 = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "api_error_status": 503,
        "result": "API Error: 503 Service Unavailable", "session_id": "s",
        "usage": {"input_tokens": 0, "output_tokens": 0}}


def test_an_editing_replay_refused_to_keep_the_finish_ends_in_the_first_calls_own_failure(tmp_path, monkeypatch):
    """The editing call was sent once and failed. FinishReserved means
    "refused before it started", and the session reads it as no call made."""
    import tests.test_review1110_transport as T
    from quadratus.config import Settings
    from quadratus.delegation import DelegationLedger
    from quadratus.providers import ProviderError
    from quadratus.run_budget import FinishReserved, RunBudget, RunLimits
    from quadratus.runtime import Fleet

    monkeypatch.setattr("quadratus.providers.random.uniform", lambda a, b: 0.0)
    root = T._project(tmp_path)
    script, runs = T._fake_cli(tmp_path, "claude", f"print(json.dumps({_503!r}))\nsys.exit(1)\n")
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    budget = RunBudget(RunLimits(max_calls=3, max_reported_tokens=1_000_000, wall_seconds=600,
                                 finish_wall_seconds=0))
    budget.protect_finish(["goal", "audit"])
    ledger = DelegationLedger()
    fleet = Fleet(Settings(backend="cli", retry_base_delay=0.0, max_retries=4, cli_timeout=30.0),
                  project=root, allow_writes=True, delegation_ledger=ledger, run_budget=budget)
    with pytest.raises(ProviderError) as caught:
        fleet.invoke("claude:opus", "edit the module", allow_writes=True)
    assert not isinstance(caught.value, FinishReserved) and T._runs(runs) == 1
    assert caught.value.transport_status == 503 and caught.value.retryable is True
    assert "keep the calls held for the finish" in str(caught.value) and caught.value.resend_refused
    # The refusal itself is still on the ledger, as a call that was not made.
    assert [(e.outcome, e.invoked) for e in ledger.events][-1] == ("FinishReserved", False)


# -- trust:3: a kept final audit is not reused when the unmeasured changes differ ----------


def test_a_kept_final_audit_is_not_reused_once_the_unmeasured_changes_differ(tmp_path):
    """The audit prompt lists what editing calls changed where the measured
    diff does not read. A kept audit answered a prompt with another list."""
    from tests.test_unexecuted_acceptance import UNIT, _session

    s, prompts = _session(tmp_path, f"R1: MET - {UNIT}\nR5: NOT MET - no console check\n")
    s._audit_requirements(final=True)
    before = s._audit_binding(None)
    s._audit_requirements(final=True)
    assert len(prompts) == 1, "nothing changed: the kept audit stands"
    s.unmeasured_changes["t1"] = ["build/helper.txt"]
    changed = s._audit_binding(None)
    assert changed != before
    s._audit_requirements(final=True)
    assert len(prompts) == 2 and "build/helper.txt" in prompts[-1]
    # Order of tasks and names is not a difference; another name is.
    s.unmeasured_changes = {"t2": ["b", "a"], "t1": ["build/helper.txt"]}
    one = s._audit_binding(None)
    s.unmeasured_changes = {"t1": ["build/helper.txt"], "t2": ["a", "b"]}
    assert s._audit_binding(None) == one != changed
