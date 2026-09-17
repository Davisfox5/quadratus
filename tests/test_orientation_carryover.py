"""Attempt 8: the lead re-read what the orchestrator had already read.

The lead was handed the file to change *and* the exact signature to write, and
still spent 18 of its 26 tool calls looking around -- 9 file reads, 7 searches,
2 directory listings, against 1 edit. It had to, because nothing told it what
was *inside* the project: the codebase map it received said "Primary language:
JavaScript (19 files)" and "Top-level directories: docs, static, templates,
tests", which orients nobody.

Minutes earlier the orchestrator had read app.py, the tests and the brief to
write that task. That reading was discarded. The map is only ever written by
the deterministic scan and by task close-outs, and no attempt in this lane has
reached a close-out, so in eight runs the map has never gained a learned fact.

Re-reading is not merely duplicated, it is duplicated at a far worse price: an
agent re-sends its whole conversation on each step, so cost grows with the
square of the step count. The same work measured 437,173 tokens at 15 steps and
would be ~104,000 at 7.
"""
import pytest

from quadratus.repo_scan import ScanReport, scan_repo
from quadratus.session import (
    _MAX_ORIENT_NOTE_CHARS,
    _MAX_ORIENT_NOTES,
    _ORIENT_REQUEST,
    _read_task_orientation,
)

# -- the scan now says where the code is -----------------------------------

def _tree(tmp_path):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'static').mkdir()
    (tmp_path / 'app.py').write_text('x = 1\n' * 900)
    (tmp_path / 'static' / 'app.js').write_text('var a;\n' * 400)
    (tmp_path / 'small.py').write_text('y = 2\n')
    (tmp_path / 'tests' / 'test_big.py').write_text('assert True\n' * 5000)
    return tmp_path


def test_the_map_names_the_principal_files_with_their_sizes(tmp_path):
    report = scan_repo(_tree(tmp_path))
    assert report.principal_files[:2] == [('app.py', 900), ('static/app.js', 400)]
    note = next(n for t, n in report.map_notes() if n.startswith('Largest source files'))
    assert 'app.py (900 lines)' in note


def test_the_test_suite_is_never_named_as_principal_source(tmp_path):
    """A suite is often the biggest thing in a repository, and pointing a lead
    at it would point it at exactly the wrong place."""
    report = scan_repo(_tree(tmp_path))
    assert all('test' not in name for name, _ in report.principal_files)
    assert report.test_dirs == ['tests/']
    assert any(n == 'Test files live under: tests/' for _, n in report.map_notes())


def test_the_block_stays_orientation_rather_than_an_inventory(tmp_path):
    """It is re-sent on every call for the rest of the run, so it must be small."""
    for i in range(40):
        (tmp_path / f'mod{i}.py').write_text('z = 3\n' * (i + 1))
    report = scan_repo(_tree(tmp_path))
    assert len(report.principal_files) <= 6
    rendered = sum(len(t) + len(n) for t, n in report.map_notes())
    assert rendered < 1200, 'the map block must stay cheap enough to re-send'


def test_an_empty_tree_adds_nothing(tmp_path):
    report = scan_repo(tmp_path)
    assert report.principal_files == [] and report.test_dirs == []


def test_an_unreadable_file_is_skipped_rather_than_failing_the_scan(tmp_path):
    report = ScanReport(root=tmp_path)
    assert report.map_notes() == []


# -- and the orchestrator's own reading is carried forward ------------------

def test_the_orchestrator_is_asked_for_facts_that_can_be_acted_on():
    assert 'MAP NOTES' in _ORIENT_REQUEST
    # The instruction has to say what a useful note looks like; "app.py is the
    # main file" is what a vague request gets, and it saves nobody a read.
    assert 'already imported' in _ORIENT_REQUEST
    assert 'Omit the section entirely' in _ORIENT_REQUEST


def test_notes_are_captured_and_removed_from_the_task_text():
    notes, description = _read_task_orientation(
        'Implement the header reader in app.py.\n'
        'Budget 30 implementation lines.\n'
        '\n'
        'MAP NOTES: layout: app.py holds all 32 Flask routes; helpers near line 1261\n'
        '- imports: csv and io are already imported at the top of app.py\n'
    )
    assert notes == [
        ('layout', 'app.py holds all 32 Flask routes; helpers near line 1261'),
        ('imports', 'csv and io are already imported at the top of app.py'),
    ]
    # Not part of the task: leaving them in would put them through the scope
    # lint and into the text the lead is told to satisfy verbatim.
    assert 'MAP NOTES' not in description
    assert description.startswith('Implement the header reader')
    assert 'Budget 30 implementation lines.' in description


def test_a_reply_with_no_notes_is_left_exactly_as_it_was():
    text = 'Just implement the thing in app.py.'
    assert _read_task_orientation(text) == ([], text)


def test_a_description_continuing_after_the_section_is_not_swallowed():
    notes, description = _read_task_orientation(
        'MAP NOTES: layout: routes live in app.py\n'
        '\n'
        'Then wire the endpoint and update the docs.\n'
    )
    assert notes == [('layout', 'routes live in app.py')]
    assert description == 'Then wire the endpoint and update the docs.'


def test_a_malformed_note_is_dropped_rather_than_guessed_at():
    """The map is long-lived and re-sent on every call; a wrong note there is
    worse than no note."""
    notes, _ = _read_task_orientation('MAP NOTES: this line names no topic at all\n')
    assert notes == []


@pytest.mark.parametrize('count', [_MAX_ORIENT_NOTES + 3, 20])
def test_the_number_of_notes_is_bounded(count):
    """This exists to remove a per-call cost, not to relocate one into the map."""
    body = 'MAP NOTES:\n' + ''.join(f'- t{i}: fact number {i}\n' for i in range(count))
    notes, _ = _read_task_orientation(body)
    assert len(notes) == _MAX_ORIENT_NOTES


def test_a_long_note_is_truncated_rather_than_rejected():
    notes, _ = _read_task_orientation('MAP NOTES: layout: ' + 'x' * 500 + '\n')
    assert len(notes[0][1]) == _MAX_ORIENT_NOTE_CHARS


def test_a_note_records_the_model_that_established_it_not_the_seat_record():
    """Attempt 9 wrote provenance as a whole Seat.

    The map showed "{'key': 'openai:gpt-6-astra', 'reason':
    'fallback-unavailable', ...}" where a reader expects a model name. A note's
    author answers "who established this fact", and that is the model; why it
    held the chair belongs to the ledger.
    """
    from dataclasses import dataclass

    from quadratus.codebase_map import CodebaseMap

    @dataclass
    class _Seat:
        key: str
        reason: str = 'fallback-unavailable'

    @dataclass
    class _Meta:
        description: str

    class _Session:
        config = type('C', (), {'codebase_map': None})()

    import tempfile

    from quadratus.session import Session
    with tempfile.TemporaryDirectory() as tmp:
        session = _Session()
        session.config.codebase_map = CodebaseMap(f'{tmp}/map.jsonl')
        meta = Session._absorb_orientation(
            session, _Meta('Do it.\nMAP NOTES: imports: csv is imported at app.py:8\n'),
            _Seat('openai:gpt-6-astra'))
        note = session.config.codebase_map.notes[-1]
        assert note.author == 'openai:gpt-6-astra'
        assert 'fallback-unavailable' not in note.author
        assert meta.description == 'Do it.'


def test_a_plain_string_seat_still_works():
    import tempfile
    from dataclasses import dataclass

    from quadratus.codebase_map import CodebaseMap
    from quadratus.session import Session

    @dataclass
    class _Meta:
        description: str

    class _Session:
        config = type('C', (), {'codebase_map': None})()

    with tempfile.TemporaryDirectory() as tmp:
        session = _Session()
        session.config.codebase_map = CodebaseMap(f'{tmp}/map.jsonl')
        Session._absorb_orientation(session, _Meta('MAP NOTES: t: f\n'), 'grok:default')
        assert session.config.codebase_map.notes[-1].author == 'grok:default'
