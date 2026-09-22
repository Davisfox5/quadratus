"""Tool fit is checked on both sides before a worker errand is paid for.

Attempt 3 of the blind acceptance: the lead sent a Haiku worker the whole
implementation with write:false. The worker was handed the instruction and
nothing else -- not what tools it had, not that NEED TOOL existed -- so it did
the best it could with read-only tools and wrote 101KB of code and tests as
prose across eleven turns. 312,518 tokens, sixty percent of the run, for an
answer the harness could not apply. The restrictions held the whole time.

Three checks now stand between that errand and that bill: the lead declares
what the errand needs and the harness refuses a mismatch before any call, the
worker is told what it has and that asking is one line, and the escalation is
capped at two calls.
"""
import json

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.memory import TaskMemory
from quadratus.task_kinds import Need, needs_from_text
from quadratus.workers import (
    ErrandToolMismatch,
    WorkerPool,
    capability_preamble,
    check_errand_fit,
    worker_capabilities,
    worker_menu,
)

# The real attempt-3 errand shape, abridged.
IMPLEMENT = ("Add the core CSV-parsing helper `def _import_preview_rows(project, text):` "
             "to app.py and create tests/test_import_preview.py covering the four scenarios.")
RUN_TESTS = "Add the helper to app.py. Run `/usr/local/bin/python -m pytest -q` and confirm it passes."
EXPLAIN = "Explain what _filter_clips does and which columns the CSV export writes."

# The read-only request rejected during the 2026-09-22 native GameTape run.
REVIEW = (
    "Perform a read-only cumulative review of the CSV import-preview feature against "
    "the full stated contract. Inspect app.py preview helpers/route, static/js/app.js "
    "preview handlers and keyboard/Escape behavior, templates/index.html preview UI, "
    "static/css/style.css preview styles, tests/test_import_preview.py, "
    "tests/ui/import_preview.test.js, and docs/CSV_IMPORT.md. Verify preserved "
    "saved-filter, bulk-edit, focus/keyboard, export, and closeProject teardown behavior. "
    "Report every finding with severity and confidence, include file:line evidence for "
    "every contract clause, explicitly verify the two known documentation defects, and "
    "identify any minimal focused test needed for a confirmed code defect. "
    "Do not edit files or run commands."
)


def test_recorded_readonly_review_needs_no_write_grant():
    assert check_errand_fit(REVIEW, needs=[], write=False) is None


def test_readonly_wording_never_overrides_explicit_required_capabilities():
    assert check_errand_fit(REVIEW, needs=[Need.PATCH], write=False) is not None
    assert check_errand_fit(REVIEW, needs=[Need.EXECUTE], write=False) is not None


def _pool(tmp_path, run):
    return WorkerPool(store=ArtifactStore(tmp_path / '.quadratus'), run=run)


def _task(tmp_path, task_id='t1'):
    return TaskMemory(task_id, 'openai:gpt-5.6-sol', ArtifactStore(tmp_path / '.quadratus'))


# -- what a worker can actually do ---------------------------------------

def test_a_worker_can_never_execute_or_write_directly():
    assert worker_capabilities(allow_writes=False) == frozenset()
    assert worker_capabilities(allow_writes=True) == frozenset({Need.PATCH})
    # The briefing says the same thing in the worker's own words.
    assert 'no shell' in capability_preamble(False)
    assert 'no write tools and no shell' in capability_preamble(True)


def test_the_attempt_three_errand_is_refused_for_the_reason_it_failed():
    message = check_errand_fit(IMPLEMENT, needs=[Need.PATCH], write=False)
    assert message is not None
    assert 'write:true' in message and 'describe' in message


def test_an_errand_that_runs_commands_is_refused_however_it_is_worded():
    # The task brief said "Run `/usr/local/bin/python -m pytest -q`", which a
    # detector anchored on the bare runner name does not see.
    assert Need.EXECUTE in needs_from_text(RUN_TESTS)
    for declared in ([], [Need.EXECUTE], [Need.PATCH]):
        message = check_errand_fit(RUN_TESTS, needs=declared, write=True)
        assert message is not None and 'run commands' in message


def test_direct_write_is_refused_and_named_as_a_patch_instead():
    message = check_errand_fit("Regenerate the logo.png thumbnail",
                               needs=[Need.DIRECT_WRITE], write=True)
    assert message is not None and 'returns a patch' in message


def test_a_grant_the_errand_does_not_need_is_refused_when_needs_were_stated():
    assert check_errand_fit(EXPLAIN, needs=[], write=True) is not None


def test_silence_is_not_a_contradiction():
    """A lead that declares nothing is read from its instruction, not refused.

    Declaring an empty list says "this errand changes nothing"; omitting the
    field says nothing at all. Treating the second as the first would refuse
    every caller written before the field existed, on no evidence.
    """
    assert check_errand_fit('save the config', write=True) is None
    assert check_errand_fit(EXPLAIN, write=False) is None
    # Silence still does not excuse an instruction that plainly runs commands.
    assert check_errand_fit(RUN_TESTS, write=True) is not None


def test_a_matched_errand_passes_on_either_side_of_the_grant():
    assert check_errand_fit(EXPLAIN, needs=[], write=False) is None
    assert check_errand_fit("Add the helper to app.py", needs=[Need.PATCH], write=True) is None


def test_an_unknown_need_label_raises_rather_than_vanishing():
    with pytest.raises(ValueError, match='unknown need'):
        check_errand_fit(EXPLAIN, needs=['exec'], write=False)


# -- the lead's side: refused before anything is spent --------------------

def test_a_mismatched_errand_costs_no_call_and_no_budget(tmp_path):
    calls = []
    pool = _pool(tmp_path, lambda key, prompt, **kw: calls.append(prompt) or 'done')
    task = _task(tmp_path)
    with pytest.raises(ErrandToolMismatch, match='write:true'):
        pool.commission(task=task, parent_key='openai:gpt-5.6-sol', prompt=IMPLEMENT,
                        label='code-1', errand='code', needs=[Need.PATCH], allow_writes=False)
    assert calls == []
    assert pool.spawned('t1') == 0
    assert pool.remaining('t1') == pool.budget.max_per_task


def test_the_menu_states_both_boundaries_to_the_lead():
    menu = worker_menu()
    assert 'can run a command or write a file itself' in menu
    assert 'two calls, not four' in menu


# -- the worker's side: it is told what it has ---------------------------

@pytest.mark.parametrize('write, expected', [
    (False, 'cannot change files'),
    (True, 'PATCH:'),
])
def test_the_worker_is_told_its_tools_and_how_to_ask(tmp_path, write, expected):
    seen = []
    pool = _pool(tmp_path, lambda key, prompt, **kw: seen.append(prompt) or 'done')
    needs = [Need.PATCH] if write else []
    pool.commission(task=_task(tmp_path), parent_key='p', prompt=EXPLAIN if not write else
                    'Add the helper to app.py', label='code-1', errand='code',
                    needs=needs, allow_writes=write)
    prompt = seen[0]
    assert expected in prompt
    assert 'NEED TOOL:' in prompt
    assert 'no shell' in prompt
    assert 'Do not work around a missing tool' in prompt
    assert prompt.endswith(EXPLAIN if not write else 'Add the helper to app.py')


def test_a_worker_that_asks_returns_the_request_to_the_lead(tmp_path):
    pool = _pool(tmp_path, lambda key, prompt, **kw: 'NEED TOOL: write access to app.py')
    result = pool.commission(task=_task(tmp_path), parent_key='p', prompt=EXPLAIN,
                             label='code-1', errand='code', needs=[], allow_writes=False)
    assert result.needs_tool == 'write access to app.py'
    assert result.error is None
    assert 'worker needs a tool' in result.summary


# -- the escalation is two calls ------------------------------------------

def test_asking_twice_on_one_errand_ends_the_escalation(tmp_path):
    calls = []

    def run(key, prompt, **kw):
        calls.append(kw.get('allow_writes'))
        return 'NEED TOOL: more'

    pool = _pool(tmp_path, run)
    task = _task(tmp_path)
    first = pool.commission(task=task, parent_key='p', prompt=EXPLAIN, label='code-1',
                            errand='code', needs=[], allow_writes=False)
    assert first.needs_tool == 'more'
    second = pool.commission(task=task, parent_key='p', prompt=EXPLAIN, label='code-2',
                             errand='code', needs=[Need.PATCH], allow_writes=True)
    assert second.needs_tool is None
    assert 'second time' in second.error and 'do it yourself' in second.error
    assert len(calls) == 2, 'the escalation is one ask and one reissue'
    assert second.ref is not None, 'the second answer is still filed'


def test_a_different_errand_may_still_ask(tmp_path):
    pool = _pool(tmp_path, lambda key, prompt, **kw: 'NEED TOOL: something')
    task = _task(tmp_path)
    pool.commission(task=task, parent_key='p', prompt=EXPLAIN, label='a-1',
                    errand='code', needs=[], allow_writes=False)
    other = pool.commission(task=task, parent_key='p', prompt='Summarise docs/BULK_EDIT.md',
                            label='read-1', errand='read', needs=[], allow_writes=False)
    assert other.needs_tool == 'something' and other.error is None


# -- end to end: the lead is refused before a worker is paid for -----------

def test_the_lead_gets_the_refusal_back_without_a_worker_call(tmp_path, monkeypatch):
    """The whole point, driven through a real project run.

    The lead sends the attempt-3 errand: the entire implementation, with
    write:false. No worker call is made, the refusal comes back in the lead's
    next prompt naming the fix, and the lead proceeds.
    """
    from quadratus.cli_providers import CLIProvider
    from quadratus.config import Settings
    from quadratus.project_run import run_project

    (tmp_path / 'app.py').write_text('x = 1\n')
    monkeypatch.setattr(CLIProvider, 'available', lambda _: True)
    prompts = []

    def call(self, prompt, system, history):
        prompts.append(prompt)
        self.last_usage = {'input_tokens': 10, 'output_tokens': 2}
        if 'Name the single next task' in prompt:
            return 'KIND: backend simple\nSCOPE: ' + json.dumps({
                'permitted_paths': ['app.py'], 'intended_result': 'Add a helper',
                'acceptance': ['the helper exists'], 'max_lines': 20,
            }) + '\nAdd a helper to app.py.'
        if 'You are leading' in prompt:
            if not any('was sent without a write grant' in p for p in prompts):
                return 'WORKER ' + json.dumps({
                    'errand': 'code', 'instruction': IMPLEMENT,
                    'demanding': False, 'write': False, 'needs': ['patch'],
                })
            (tmp_path / 'app.py').write_text('x = 1\ndef helper():\n    return 2\n')
            return 'Implemented'
        return 'ok'

    monkeypatch.setattr(CLIProvider, '_call', call)
    run_project('Add a helper', tmp_path, Settings(backend='cli'),
                allow_writes=True, max_tasks=1)

    refusals = [p for p in prompts if 'was sent without a write grant' in p]
    assert refusals, 'the lead was never told why the errand was refused'
    assert 'write:true' in refusals[0]
    # No worker ran: every call is a seat call, none carries the preamble.
    assert not any('## What you can do' in p for p in prompts)
