"""Codex review of 039bebb (#42), P2: a bare directory is a broad scope and
must never pass the exact-file admission. Reproducer kept verbatim."""
from pathlib import Path

from tests.lifecycle import harness as H
from tests.lifecycle.test_direct_tier import FAVICON, _decl, _run, _task


def test_directory_prefix_is_not_an_exact_file_grant(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {'static/nested/unplanned.svg': '<svg/>\n'})
        H.evidence(Path(call.cwd), call.task, age=H.FRESH)
        return 'Wrote a nested file.\nCHANGED: ["static/nested/unplanned.svg"]'

    replay = _run(tmp_path, monkeypatch,
                  _decl(scope=dict(FAVICON, permitted_paths=['static'])),
                  lead=lead, record_complete=False)
    task = _task(replay)
    assert (replay.project / 'static/nested/unplanned.svg').is_file()
    assert task['contract']['required']['tier'] == 'normal', (
        'The exact-file admission accepted an existing directory prefix; '
        'Scope.permits then accepted an undeclared descendant and collaborator review was omitted',
        replay.workflow['calls_by_task'], task['contract']['required'])


def test_a_directory_spelling_is_refused_at_admission(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch,
                  _decl(scope=dict(FAVICON, permitted_paths=FAVICON['permitted_paths'] + ['static/'])),
                  record_complete=False)
    refused = _task(replay)['contract']['required']['tier_refused']
    assert 'spelled as a directory' in refused, refused


def test_an_existing_directory_is_refused_at_admission(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch,
                  _decl(scope=dict(FAVICON, permitted_paths=['static'])), record_complete=False)
    refused = _task(replay)['contract']['required']['tier_refused']
    assert 'existing directory, not a file' in refused, refused


def test_a_declared_file_turned_directory_is_out_of_scope_on_the_direct_tier(tmp_path, monkeypatch):
    def lead(call, replay):
        (Path(call.cwd) / 'static/favicon.svg').unlink()
        H.write(call, {'static/favicon.svg/icon.svg': '<svg/>\n'})
        return 'Made the path a directory.\nCHANGED: ["static/favicon.svg/icon.svg"]'

    replay = _run(tmp_path, monkeypatch, _decl(), lead=lead, record_complete=False)
    task = _task(replay)
    assert task['contract']['required']['tier'] == 'direct'
    assert task['contract']['scope']['exact'] is True
    assert task['closed_as'] == 'failed', task
    assert any(f['kind'] == 'failed' and 'scope' in f['detail'] for f in task['facts']), task['facts']
    assert any('static/favicon.svg/icon.svg' in f['detail'] for f in task['facts'] if f['kind'] == 'integrity')


def test_the_normal_tier_keeps_directory_prefix_grants(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {'static/nested/unplanned.svg': '<svg/>\n'})
        H.evidence(Path(call.cwd), call.task, age=H.FRESH)
        return 'Wrote a nested file.\nCHANGED: ["static/nested/unplanned.svg"]'

    replay = _run(tmp_path, monkeypatch,
                  _decl(scope=dict(FAVICON, permitted_paths=['static']), tier=''), lead=lead)
    task = _task(replay)
    assert task['contract']['required']['tier'] == 'normal'
    assert 'exact' not in task['contract']['scope']
    assert task['closed_as'] != 'failed', task['closed_as']
