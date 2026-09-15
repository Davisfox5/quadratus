"""Review control markers must not be inferred from ordinary prose."""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.memory import TaskMemory
from quadratus.session import Session, TaskSpec


@pytest.mark.parametrize('note,expected', [
    ('Not marked BLOCKING; consider renaming this variable.', 0),
    ('This is a non-blocking suggestion.', 0),
    ('The word BLOCKING is used in a comment.', 0),
    ('BLOCKING: stale data is accepted', 1),
    ('- BLOCKING: stale data is accepted', 1),
    ('1. **BLOCKING:** stale data is accepted', 1),
])
def test_only_explicit_findings_trigger_recheck(tmp_path, note, expected):
    calls = []

    def invoke(key, prompt):
        calls.append(prompt)
        if 'contributing an independent read' in prompt:
            return note
        if 'Check only your BLOCKING findings' in prompt:
            return 'RESOLVED'
        if 'The task is finished' in prompt:
            return 'SUMMARY: done\nREASONING: checked'
        return 'Implemented'

    session = Session('fix', ArtifactStore(tmp_path), invoke)
    session.run_task(TaskSpec('t1', 'fix', complexity='standard'))
    assert sum('Check only your BLOCKING findings' in p for p in calls) == expected


@pytest.mark.parametrize('reply,resolved', [
    ('RESOLVED', True),
    ('\n resolved \n', True),
    ('The focus now stays inside the dialog.\nRESOLVED', True),
    ('RESOLVED\nThe focus now stays inside the dialog.', True),
    ('UNRESOLVED: focus still escapes', False),
    ('RESOLVED\nUNRESOLVED: focus still escapes', False),
    ('UNRESOLVED: focus still escapes\nRESOLVED', False),
    ('RESOLVED\nBLOCKING: focus still escapes', False),
    ('- BLOCKING: focus still escapes\nRESOLVED', False),
    ('RESOLVED\nThis is not resolved.', False),
    ('RESOLVED: perhaps', False),
    ('RESOLVEDish', False),
    ('Seems resolved to me.', False),
    ('Explanation\nRESOLVED\nMore explanation', False),
    ('RESOLVED\nRESOLVED', False),
    ('', False),
])
def test_recheck_requires_unambiguous_boundary_verdict(tmp_path, reply, resolved):
    store = ArtifactStore(tmp_path)
    session = Session('fix', store, lambda *a: reply)
    unresolved = session._recheck_blocking(
        TaskSpec('t1', 'fix'), [('claude:opus', 'BLOCKING: focus escapes')],
        'Fixed focus', TaskMemory('t1', 'openai:gpt-5.6-sol', store),
    )
    assert (unresolved == []) is resolved
