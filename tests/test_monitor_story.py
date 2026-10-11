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
        assert '<div class="qm-big">In progress</div>' in view
        assert 'Replies appear when each call finishes' in view
        assert 'No team reply has been saved yet' in view
        assert 'Run has not finished' in view
        assert {p: p.stat().st_mtime_ns for p in run.rglob('*')} == before
    finally:
        release.set()
        thread.join(5)
    view = render_story(read_status(tmp_path))
    assert 'The reset button now keeps your search' in view
    assert 'reply saved' in view
    assert '<div class="qm-big">In progress</div>' not in view, 'a returned call is no longer shown as in flight'


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
    assert '<div class="qm-big">In progress</div>' not in view
    assert 'qm-banner qm-confirmed' not in view


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


# Card layout (2026-10-11): what the presentation must never lose.

def _terminal(tmp_path, **result):
    run = tmp_path / '.quadratus/runs/20261011T040426Z-1b6cfff5'
    run.mkdir(parents=True)
    (run / 'result.json').write_text(json.dumps(result))
    return run


def test_every_saved_string_is_escaped_in_banner_cards_and_replies(tmp_path):
    run = _terminal(tmp_path, completed=False, goal='<img src=x onerror=alert(1)>. more',
                    error='<b>boom</b> & stop', checks=[dict(passed=False)])
    store = ArtifactStore(run / 'artifacts')
    store.put('<script>steal()</script>\n\nmore <a href="javascript:x">link</a>', kind='draft',
              author='claude:opus')
    view = render_story(read_status(tmp_path))
    for raw in ('<img', '<script>', '<a href', '<b>boom'):
        assert raw not in view
    assert '&lt;img src=x onerror=alert(1)&gt;' in view
    assert '&lt;b&gt;boom&lt;/b&gt; &amp; stop' in view, 'the exact error stays readable in Technical details'


def test_the_banner_never_reads_as_success_unless_the_engine_recorded_it(tmp_path):
    _terminal(tmp_path, completed=False, tasks=3, checks=[dict(passed=True)])
    view = render_story(read_status(tmp_path))
    assert 'Run ended — goal not confirmed' in view and 'qm-banner qm-stopped' in view
    assert 'qm-banner qm-confirmed' not in view and 'goal complete' not in view
    assert 'Closed tasks do not prove the whole goal is met' in view


def test_every_saved_reply_opens_in_full_behind_a_short_excerpt(tmp_path):
    run = _terminal(tmp_path, completed=False)
    store = ArtifactStore(run / 'artifacts')
    long = 'First line of the review. ' + 'detail ' * 300 + 'THE LAST WORDS'
    store.put(long, kind='review:claude:opus', author='claude:opus')
    view = render_story(read_status(tmp_path))
    excerpt = view.split('class="qm-excerpt">', 1)[1].split('</p>', 1)[0]
    assert len(excerpt) <= 200 and excerpt.startswith('First line of the review.')
    assert '<details><summary>Read the full reply</summary><pre>' in view
    assert 'THE LAST WORDS' in view, 'the full saved text is one click away'
    assert view.count('They are the team’s own claims') == 1, 'the caveat is stated once, not per card'


def _live(tmp_path):
    run = tmp_path / '.quadratus/runs/20261011T040426Z-1b6cfff5'
    run.mkdir(parents=True)
    return run, RunActivity(run)


def test_a_test_command_recorded_as_started_shows_as_checking(tmp_path):
    run, journal = _live(tmp_path)
    journal.record('call-started', call_id='a', model='openai:gpt-5.6-sol', role='lead', task='t1')
    journal.record('call-ended', call_id='a', model='openai:gpt-5.6-sol', role='lead', task='t1')
    journal.record('check-started', task='t1')
    view = render_story(read_status(tmp_path))
    assert '<div class="qm-big">Checking the project tests</div>' in view
    assert 'not proof the process is still running' in view
    journal.record('check-ended', task='t1')
    view = render_story(read_status(tmp_path))
    assert 'Checking the project tests</div>' not in view
    assert 'Nothing recorded as in progress' in view


def test_an_activity_task_id_is_escaped_exactly_once(tmp_path):
    run, journal = _live(tmp_path)
    journal.record('call-started', call_id='a', model='claude:opus', role='collaborator', task='t1 & <b>x</b>')
    view = render_story(read_status(tmp_path))
    assert 't1 &amp; &lt;b&gt;x&lt;/b&gt;' in view
    assert '&amp;amp;' not in view and '&amp;lt;' not in view and '<b>x' not in view


def test_a_reply_cut_at_the_read_bound_is_labelled_saved_not_full(tmp_path):
    run, _ = _live(tmp_path)
    ArtifactStore(run / 'artifacts').put('start ' + 'y' * 7000, kind='draft', author='claude:opus')
    view = render_story(read_status(tmp_path))
    assert '<summary>Read saved reply</summary>' in view and 'Read the full reply' not in view
    assert 'Shown up to 6,000 characters' in view


def test_the_timeline_overrides_host_list_styles():
    from quadratus.monitor_story import _STYLE
    assert 'ol.qm-timeline{margin:0 !important;padding:0 !important;list-style:none !important}' in _STYLE
