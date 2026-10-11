"""Operator visibility during real session calls, with no provider or model use."""

import json
import threading

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.delegation import invocation
from quadratus.gui import monitor_view
from quadratus.monitor import read_status
from quadratus.monitor_story import render_story
from quadratus.run_activity import RunActivity
from quadratus.session import Session, SessionConfig


def test_a_blocked_call_is_visible_before_it_returns_and_reply_appears_after(tmp_path):
    run = tmp_path / '.quadratus/runs/20261010T180000Z-deadbeef'
    run.mkdir(parents=True)
    journal = RunActivity(run)
    started, release = threading.Event(), threading.Event()

    def invoke(model, prompt):
        started.set()
        assert release.wait(5)
        return 'The reset button now keeps your search and restores library order.'

    session = Session('Add reset', ArtifactStore(run / 'artifacts'), invoke,
                      config=SessionConfig(activity=journal.record))

    def call():
        with invocation('t1', 'collaborator'):
            session._invoke_model_call('claude:opus', 'Review the change')

    thread = threading.Thread(target=call)
    thread.start()
    try:
        assert started.wait(5)
        before = {p: p.stat().st_mtime_ns for p in run.rglob('*')}
        view, _ = monitor_view(str(tmp_path))
        assert 'Claude: reviewing the change' in view
        assert 'No team reply has been saved yet' in view
        assert 'Run has not finished' in view
        assert {p: p.stat().st_mtime_ns for p in run.rglob('*')} == before
    finally:
        release.set()
        thread.join(5)
    view = render_story(read_status(tmp_path))
    assert 'The reset button now keeps your search' in view
    assert 'reply saved' in view
    assert '**Last recorded:** Claude: reviewing' not in view


def test_call_errors_end_activity_and_broken_journal_does_not_change_the_error(tmp_path):
    events = []

    def record(kind, **fields):
        events.append(kind)
        raise OSError('viewer broken')

    def fail(*args):
        raise RuntimeError('original failure')

    session = Session('goal', ArtifactStore(tmp_path / 'artifacts'), fail,
                      config=SessionConfig(activity=record))
    with pytest.raises(RuntimeError, match='original failure'):
        session._invoke_model_call('claude:opus', 'prompt')
    assert events == ['call-started', 'call-ended']


def test_old_terminal_run_shows_public_replies_not_prompts_and_preserves_error(tmp_path):
    run = tmp_path / '.quadratus/runs/20261010T180000Z-deadbeef'
    run.mkdir(parents=True)
    store = ArtifactStore(run / 'artifacts')
    store.put('private prompt must stay out', kind='prompt', author='harness')
    store.put('<script>alert(1)</script> We kept the search.', kind='review:claude:opus', author='claude:opus')
    error = 'CheckUnattributable: extra-1 timed out after 600s'
    (run / 'result.json').write_text(json.dumps(dict(completed=False, error=error, tasks=2, goal='Add reset')))
    view = render_story(read_status(tmp_path))
    assert 'Run ended — goal not confirmed' in view
    assert 'A test did not finish after 10 minutes' in view
    assert error in view
    assert 'We kept the search' in view
    assert 'private prompt must stay out' not in view
    assert '<script>' not in view
    assert '&lt;script&gt;' in view


def test_journal_saves_progress_without_a_gui_and_tolerates_a_broken_callback(tmp_path):
    def broken(message):
        raise RuntimeError('viewer gone')
    journal = RunActivity(tmp_path, broken)
    journal('Checking the work')
    event = json.loads(journal.path.read_text())
    assert event['message'] == 'Checking the work'


def test_terminal_result_overrides_an_unfinished_call_and_unjudged_goal_stays_unjudged(tmp_path):
    run = tmp_path / '.quadratus/runs/20261010T180000Z-deadbeef'
    run.mkdir(parents=True)
    journal = RunActivity(run)
    journal.record('call-started', call_id='one', model='claude:opus', role='lead')
    (run / 'result.json').write_text(json.dumps(dict(completed=True, explicit_tasks=dict(goal_judged=False))))
    view = render_story(read_status(tmp_path))
    assert 'Listed tasks finished — overall goal not checked' in view
    assert '**Last recorded:**' not in view


def test_nonregular_reply_and_malformed_metadata_are_not_read(tmp_path):
    run = tmp_path / '.quadratus/runs/20261010T180000Z-deadbeef'
    run.mkdir(parents=True)
    store = ArtifactStore(run / 'artifacts')
    ref = store.put('reply', kind='draft', author='claude:opus')
    body = run / 'artifacts' / (ref.id + '.txt')
    body.unlink()
    import os
    os.mkfifo(body)
    assert 'No team reply has been saved yet' in render_story(read_status(tmp_path))
    (run / 'artifacts' / (ref.id + '.json')).write_text(json.dumps(dict(kind=[], author='claude:opus')))
    assert 'No team reply has been saved yet' in render_story(read_status(tmp_path))
