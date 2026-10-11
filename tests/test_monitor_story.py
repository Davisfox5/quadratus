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
        assert 'not proof' not in view.split('Run records and full report', 1)[0]
        assert 'No team reply has been saved yet' in view
        assert 'Run has not finished' in view
        assert {p: p.stat().st_mtime_ns for p in run.rglob('*')} == before
    finally:
        release.set()
        thread.join(5)
    view = render_story(read_status(tmp_path))
    assert 'The reset button now keeps your search' in view
    assert 'Claude reported: The reset button now keeps your search' in view
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
    assert '3 steps closed' in view


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
    assert 'Team replies describe what agents reported' in view


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
    assert '<div class="qm-big">Testing the changes</div>' in view
    assert 'not proof the process is still running' not in view
    journal.record('check-ended', task='t1')
    view = render_story(read_status(tmp_path))
    assert 'Testing the changes</div>' not in view
    assert 'Waiting for the next update' in view


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


def test_live_cards_describe_the_task_tests_and_budget_without_filler(tmp_path):
    run, journal = _live(tmp_path)
    journal('task 2: COVERS: R1\nRESOLVES: F1\nFix the deterministic assertion mismatch in `tests/gui_completion/browser_sort_reset.test.js`.')
    journal.record('call-started', call_id='a', model='grok:default', role='lead', task='t2')
    (run / 'budget.json').write_text(json.dumps(dict(reported_tokens=12500, reserved_attempts=2,
        elapsed_seconds=65, limits=dict(max_reported_tokens=100000, max_calls=10))))
    view = render_story(read_status(tmp_path))
    assert 'Grok: building the change — Fix the wrong expected result in the browser test.' in view
    assert '2 of 10 requests · 1 min 05 sec' in view
    assert '12% of 100,000 token limit' in view
    cards = view.split('<div class="qm-cards">')[1].split('<section aria-label="Activity">')[0]
    for filler in ('not proof', 'Earlier passes', 'Updates after', 'Closed tasks'):
        assert filler not in cards
    assert 'COVERS' not in view.split('Run records and full report')[0]


def test_live_harness_counts_are_visible_but_model_claims_do_not_override_them(tmp_path):
    run, journal = _live(tmp_path)
    journal.record('check-started', task='t1')
    journal.record('check-ended', task='t1')
    store = ArtifactStore(run / 'artifacts')
    store.put('check: passed: exit 0\n121 passed in 2.3s\nextra-1: failed: nonzero exit\n# pass 54\n# fail 2\n# skipped 0',
              kind='check-output', author='harness')
    ref = store.put('All browser tests passed.', kind='monitor-reply', author='grok:default')
    journal.record('reply', artifact=ref.id, model='grok:default', role='lead', task='t2')
    view = render_story(read_status(tmp_path))
    assert 'Last checked: 175 passed · 2 failed' in view
    assert 'Tests finished: 175 passed · 2 failed' in view
    assert 'Grok reported: All browser tests passed.' in view
    assert 'qm-banner qm-confirmed' not in view


def test_terminal_cards_use_actual_failed_receipts_and_explain_the_stop(tmp_path):
    run = _terminal(tmp_path, completed=False, tasks=1, error='RunBudgetExceeded',
        in_flight=dict(task='t2', description='Correct the expected message in the browser test.'),
        checks=[dict(passed=False, receipts=[dict(report=dict(counts=dict(passed=175, failed=2, skipped=0),
          failures=[dict(nodeid='test.js::restore-order browser review at 390x844')]))])])
    journal = RunActivity(run)
    journal.record('call-started', call_id='a', model='grok:default', role='lead', task='t2')
    (run / 'budget.json').write_text(json.dumps(dict(stop_reason='reported_token_threshold',
        reported_tokens=2019296, reserved_attempts=11, elapsed_seconds=876,
        limits=dict(max_reported_tokens=2000000, max_calls=20))))
    view = render_story(read_status(tmp_path))
    assert 'reached its token limit before completion was confirmed' in view
    assert 'Last work: Correct the expected message in the browser test.' in view
    assert '175 passed · 2 failed · 0 skipped' in view
    assert 'Restore library order (phone)' in view
    assert '11 of 20 requests · 14 min 36 sec' in view
    assert '<div class="qm-big">In progress</div>' not in view


def test_check_output_fifo_and_non_harness_claims_are_not_test_results(tmp_path):
    import os
    run, _ = _live(tmp_path)
    store = ArtifactStore(run / 'artifacts')
    ref = store.put('# pass 99\n# fail 0', kind='check-output', author='harness')
    body = run / 'artifacts' / (ref.id + '.txt')
    body.unlink()
    os.mkfifo(body)
    store.put('# pass 999\n# fail 0', kind='check-output', author='claude:opus')
    view = render_story(read_status(tmp_path))
    assert 'Not tested yet' in view
    assert '999 passed' not in view


def test_protocol_and_tool_details_stay_out_of_reply_previews():
    from quadratus.monitor_story import _excerpt
    assert _excerpt('KIND: debug\nSCOPE: {"permitted_paths": ["test.js"]}\n\nFix the deterministic assertion mismatch in `tests/browser.test.js`.') == 'Fix the wrong expected result in the browser test.'
    assert _excerpt('## Checks\n\n**NOT RUN:** pytest — this review session has no shell tool at all (no Bash).') == 'Could not run the tests because this reviewer could not run commands.'


@pytest.mark.parametrize('output, expected', [
    ('check: passed: exit 0\n121 passed in 2.3s', '121 passed · 0 failed'),
    ('check: failed: nonzero exit\n1 failed, 2 passed in 2.3s\n# pass 4\n# fail 0', '6 passed · 1 failed'),
    ('check: failed: nonzero exit\n1 error, 2 passed, 3 skipped in 2.3s', '2 passed · 1 failed · 3 skipped'),
    ('check: failed: nonzero exit\n1 failed, 2 passed\nℹ pass 4\nℹ fail 0', '6 passed · 1 failed'),
])
def test_live_python_results_are_independent_of_node_results(tmp_path, output, expected):
    run, _ = _live(tmp_path)
    ArtifactStore(run / 'artifacts').put(output, kind='check-output', author='harness')
    view = render_story(read_status(tmp_path))
    assert f'Last checked: {expected}' in view
    assert 'qm-banner qm-confirmed' not in view


def test_recorded_wall_deadline_has_a_plain_time_limit_explanation(tmp_path):
    run = _terminal(tmp_path, completed=False, error='RunBudgetExceeded')
    (run / 'budget.json').write_text(json.dumps(dict(stop_reason='wall_deadline')))
    view = render_story(read_status(tmp_path))
    assert 'The run reached its time limit.' in view
    assert 'goal not confirmed' in view


def test_runner_failure_does_not_claim_the_application_needs_a_fix(tmp_path):
    _terminal(tmp_path, completed=False, error='CheckUnattributable',
              checks=[dict(passed=False, attribution=dict(product=False, reasons=['runner unavailable']))])
    view = render_story(read_status(tmp_path))
    assert '<div class="qm-big">Did not pass</div>' in view
    assert 'Needs a fix' not in view
