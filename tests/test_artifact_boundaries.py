"""Untrusted FETCH ids and filesystem links cannot cross the artifact root."""

import hashlib
import json
import os

import pytest

from quadratus.artifacts import ArtifactRef, ArtifactStore
from quadratus.session import Session, SessionConfig


@pytest.mark.parametrize('identifier', ['../outside', '/tmp/outside', '..%2foutside',
                                       'a' * 13, 'A' * 12, '', None, 'a\x00b'])
def test_invalid_ids_are_missing_for_all_entry_points(tmp_path, identifier):
    store = ArtifactStore(tmp_path / 'artifacts')
    (tmp_path / 'outside.txt').write_text('sentinel')
    with pytest.raises(KeyError):
        store.get(identifier)
    with pytest.raises(KeyError):
        store.get(ArtifactRef(identifier, 'draft', 'model', 1, 8, ''))
    assert store.ref(identifier) is None
    assert store.refs(identifier) == []
    assert (tmp_path / 'outside.txt').read_text() == 'sentinel'


@pytest.mark.parametrize('suffix', ['.txt', '.json'])
@pytest.mark.parametrize('link_kind', ['existing', 'dangling', 'cycle'])
def test_valid_ids_cannot_follow_links_on_reads_or_writes(tmp_path, suffix, link_kind):
    store = ArtifactStore(tmp_path / 'artifacts')
    content = 'content chosen by a model'
    identifier = hashlib.sha256(content.encode()).hexdigest()[:12]
    outside = tmp_path / ('outside' + suffix)
    if link_kind == 'existing':
        outside.write_text('sentinel')
    link = store.root / (identifier + suffix)
    link.symlink_to(link if link_kind == 'cycle' else outside)
    if suffix == '.txt':
        with pytest.raises(KeyError):
            store.get(identifier)
    else:
        assert store.ref(identifier) is None
    with pytest.raises(OSError):
        store.put(content, kind='draft', author='model')
    assert link.is_symlink()
    if link_kind == 'existing':
        assert outside.read_text() == 'sentinel'
    else:
        assert not outside.exists()


def test_nonregular_artifact_does_not_block_on_fifo(tmp_path):
    store = ArtifactStore(tmp_path / 'artifacts')
    os.mkfifo(store.root / ('a' * 12 + '.txt'))
    with pytest.raises(KeyError):
        store.get('a' * 12)


def test_unicode_empty_dedup_and_all_provenance_survive_reopen(tmp_path):
    store = ArtifactStore(tmp_path)
    for content in ['', 'Καλημέρα\n原文\n']:
        first = store.put(content, kind='draft', author='lead')
        second = store.put(content, kind='review', author='reviewer')
        assert first.id == second.id
        reopened = ArtifactStore(tmp_path)
        assert reopened.get(second) == content
        assert reopened.ref(first.id) == first
        assert {r.author for r in reopened.refs(first.id)} == {'lead', 'reviewer'}
    assert len(store.ids()) == 2


def test_session_fetch_does_not_inline_outside_file(tmp_path):
    (tmp_path / 'outside.txt').write_text('PRIVATE_SENTINEL')
    prompts = []

    def invoke(model, prompt):
        prompts.append(prompt)
        return 'KIND: backend\nFETCH: ../outside' if len(prompts) == 1 else 'DONE'

    session = Session('review', ArtifactStore(tmp_path / 'artifacts'), invoke=invoke,
                      available=lambda key: True, config=SessionConfig())
    assert session.next_task() is None
    assert len(prompts) == 2
    assert all('PRIVATE_SENTINEL' not in prompt for prompt in prompts)


def test_legacy_metadata_remains_readable(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.put('old', kind='draft', author='original')
    for path in tmp_path.glob(f'{ref.id}.*.json'):
        path.unlink()
    assert json.loads((tmp_path / f'{ref.id}.json').read_text())['author'] == 'original'
    assert store.refs(ref.id) == [ref]
    store.put('old', kind='review', author='new-reviewer')
    assert {r.author for r in ArtifactStore(tmp_path).refs(ref.id)} == {'original', 'new-reviewer'}
