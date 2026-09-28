"""The turn-limit breaker names the tasks its counter counted (map P3.4,
``turn_limited``).

The breaker counts capped *serial* tasks; a parallel batch neither counts
nor resets it, but its capped children were appended to the legacy
``turn_limited`` list, whose tail the stop used to name. The names now come
from the outcomes captured when the counter counted them, and when a batch
ran between them the text no longer claims "in a row". The counter, its
timing, the limit and the stop kind are unchanged. Whole Session on the
parallel harness; turn limits are injected at the lead call.
"""

from quadratus.providers import TurnLimitReached
from tests.test_parallel_tasks import BATCH, Orchestrated, _block
from tests.test_parallel_tasks import _session as parallel_session


def _run(tmp_path, plan, capped):
    script = Orchestrated(plan, lead_delay=0)
    base_for = script.invoke_for

    def invoke_for(root):
        base = base_for(root)

        def invoke(model, prompt, system=None, allow_writes=False):
            if "You are leading" in prompt and any(f" in {name}" in prompt for name in capped):
                raise TurnLimitReached("capped", turns=14)
            return base(model, prompt, system, allow_writes)
        return invoke
    script.invoke_for = invoke_for
    session, project = parallel_session(tmp_path, script, requirements_ledger=False)
    session.invoke = invoke_for(project)
    session.run(max_tasks=len(plan) + 1)
    return session, script


def test_a_batch_between_counted_caps_names_what_was_counted(tmp_path):
    session, _ = _run(tmp_path, [_block("c.py"), BATCH, _block("d.py"), "DONE"], {"c.py", "b.py", "d.py"})
    assert session.stop_reason == (
        "TurnLimitBreaker: the lead turn limit was reached on 2 serial tasks with no serial task completing "
        "in between (t1, t4; a parallel batch ran between them); stopped instead of re-planning again. "
        "Work preserved.")
    assert session.turn_limited == ["t1", "t3", "t4"], "the legacy mirror is unchanged"
    stop = session.run_outcome.facts[-1]
    assert (stop.kind, stop.legacy) == ("cap", "TurnLimitBreaker")
    assert not [m for o in session.task_outcomes for m in o.mismatches]


def test_serial_caps_keep_the_in_a_row_wording(tmp_path):
    session, _ = _run(tmp_path, [_block("c.py"), _block("d.py"), "DONE"], {"c.py", "d.py"})
    assert session.stop_reason == ("TurnLimitBreaker: the lead turn limit was reached 2 times in a row "
                                   "(t1, t2); stopped instead of re-planning again. Work preserved.")


def test_the_breaker_fires_at_the_same_point(tmp_path):
    """Timing control: the stop comes on the second counted cap, and no
    task is dispatched after it."""
    session, script = _run(tmp_path, [_block("c.py"), BATCH, _block("d.py"), _block("e.py"), "DONE"],
                           {"c.py", "b.py", "d.py"})
    assert [o.task_id for o in session.task_outcomes] == ["t1", "t2", "t3", "t4"]
    assert script.plan == [_block("e.py"), "DONE"], "nothing was asked for after the breaker"
