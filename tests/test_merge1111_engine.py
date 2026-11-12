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
