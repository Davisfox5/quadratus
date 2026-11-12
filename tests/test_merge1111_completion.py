"""Glue written when the development line was merged into the review batch
(2026-11-11), in the requirements ledger, the audit and the task ids: logic
neither line had, because each rule met a feature the other line added.

Offline: scripted replies, no vendor CLI.
"""

import pytest

from quadratus import checkpoint
from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig

pytestmark = pytest.mark.requirements_ledger


def _session(tmp_path, reply):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True, requirements_ledger=True))
    session.memory.ledger.requirements.update({"R1": "typing filters the cards", "R5": "no console errors"})
    session.memory.ledger.requirement_status.update({"R1": "covered by t1", "R5": "covered by t3"})
    session._auditor = lambda: "openai:sol"
    session._invoke_model = lambda key, prompt, **kw: reply
    return session


# -- the audit's completion call and an audit that did not run ------------------------------

def test_a_reply_with_no_requirement_verdict_is_not_completed(tmp_path, monkeypatch):
    """The development line asks the same auditor once more for the ids its
    reply left out. With no verdict for any audited requirement that would
    be the whole audit again, ahead of the review batch's retry at the next
    DONE and on the job's one shared judgment completion."""
    asked = []
    monkeypatch.setattr(Session, "_complete_audit", lambda self, *a, **k: asked.append(a) or (a[1], None))
    session = _session(tmp_path, "I could not open the working directory.")
    verdicts = session._audit_requirements()
    assert asked == [], "no completion is spent on an audit that judged nothing"
    assert all(ok is False for ok, _ in verdicts.values())
    record = session.requirement_audits[-1]
    assert record["ran"] is False and record["completion"] is None
    assert record["omitted"] == ["R1", "R5"], "the record still says what the reply left out"
    assert session._audit_misses == 1 and session.judgment_completions == []


def test_a_reply_that_judges_one_requirement_is_still_completed_for_the_other(tmp_path, monkeypatch):
    """The completion is unchanged where it applies: some requirement was
    judged, so the audit ran and only the omitted id is asked again."""
    asked = []

    def complete(self, auditor, reply, omitted, required, body):
        asked.append(list(omitted))
        return reply + "\nR5: MET - app.py", dict(asked=list(omitted), outcome="answered", answered=list(omitted),
                                                 still_omitted=[], dropped_lines=0, raw_artifact=None,
                                                 reply_artifact=None)
    monkeypatch.setattr(Session, "_complete_audit", complete)
    session = _session(tmp_path, "R1: MET - app.py")
    verdicts = session._audit_requirements()
    assert asked == [["R5"]]
    assert verdicts == {"R1": (True, "app.py"), "R5": (True, "app.py")}
    assert session._audit_unavailable == "" and "ran" not in session.requirement_audits[-1]


# -- the refusal after an audit: one sentence from each line -------------------------------

def test_the_audit_refusal_keeps_the_review_batchs_sentence_and_the_development_lines_routes(tmp_path):
    session = _session(tmp_path, "R1: NOT MET - no filter\nR5: MET - app.py")
    session.requirement_reviews.append(dict(reviewer="openai:sol", result="reviewed"))
    assert session._requirements_satisfied() is False
    refusal = session._done_refusal
    sentence = "\nName a task that fixes them (with COVERS). "
    assert sentence in refusal
    after = refusal.split(sentence, 1)[1]
    assert after.startswith("COVERS takes these requirement ids; RESOLVES names only finding ids, "
                            "F<n> or RFT<task>-<n>, never a requirement.")
    assert S._REPAIR_OUTCOME_RULE in after
    assert "- R1: no filter" in refusal and "\n  route: " in refusal


# -- the one task-id counter across a resume ------------------------------------------------

def _plain(tmp_path, name):
    return Session("goal", ArtifactStore(tmp_path / name), lambda *a, **k: "", config=SessionConfig())


def test_the_id_counter_is_carried_by_the_checkpoint(tmp_path):
    """A resumed episode is a fresh Session. Without the counter it named
    its first task t1 again, the id of a task the job had already run."""
    first = _plain(tmp_path, "a")
    for task_id in ("t1", "t2", "t3"):
        first._issue_task_id(task_id)       # t3: a child that left no history entry
    state = checkpoint.capture_state(first)
    assert state["ids_issued"] == 3 and checkpoint.carried_snapshot(first)["ids_issued"] == 3
    resumed = _plain(tmp_path, "b")
    checkpoint.restore_state(resumed, state)
    assert resumed._next_task_id() == "t4"


def test_a_checkpoint_written_before_the_counter_gives_the_highest_id_its_records_name(tmp_path):
    from quadratus.memory import TaskSummary
    from quadratus.outcome import TaskOutcome
    first = _plain(tmp_path, "a")
    first.history.append(TaskSummary(task_id="t1", author="openai:sol", summary="s", reasoning="r"))
    first.task_outcomes.append(TaskOutcome("t1", "work"))
    first.task_outcomes.append(TaskOutcome("t2", "work"))      # an outcome with no history entry
    state = checkpoint.capture_state(first)
    state.pop("ids_issued")
    resumed = _plain(tmp_path, "b")
    checkpoint.restore_state(resumed, state)
    assert resumed._next_task_id() == "t3"
    empty = _plain(tmp_path, "c")
    state = checkpoint.capture_state(_plain(tmp_path, "d"))
    state.pop("ids_issued")
    checkpoint.restore_state(empty, state)
    assert empty._next_task_id() == "t1"


# -- a cited case's path ---------------------------------------------------------------------

def test_a_cited_case_drops_a_leading_dot_slash_as_a_prefix():
    cited = S._cited_cases("see ./tests/test_a.py::test_x, ./.storybook/a_test.py::test_y "
                           "and tests/t.py::Case::test_z[1].")
    assert cited == ["tests/test_a.py::test_x", ".storybook/a_test.py::test_y", "tests/t.py::Case::test_z[1]"]
