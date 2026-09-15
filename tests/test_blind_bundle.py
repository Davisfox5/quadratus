import hashlib
import subprocess

import pytest

from quadratus.blind_bundle import freeze_input


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])
    git('init', '-q')
    git('config', 'user.email', 'fixture@example.invalid')
    git('config', 'user.name', 'Fixture')
    (repo / 'app.py').write_text('print("original")\n')
    (repo / 'CLAUDE.md').write_text('private routing expectations')
    (repo / 'link.py').symlink_to('/outside/scoring')
    git('add', 'app.py', 'CLAUDE.md', 'link.py')
    git('commit', '-qm', 'fixture')
    (repo / 'app.py').write_text('uncommitted solution must not be exported')
    return repo


def test_export_uses_committed_source_and_excludes_all_unselected_context(source, tmp_path):
    result = freeze_input(source, 'HEAD', ['app.py'], tmp_path / 'bundle', brief='Small app change')
    work = tmp_path / 'bundle/work'
    assert set(p.name for p in work.iterdir()) == {'app.py', 'TASK.md'}
    assert (work / 'app.py').read_text() == 'print("original")\n'
    for name, digest in result['files'].items():
        assert hashlib.sha256((work / name).read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError, match='new'):
        freeze_input(source, 'HEAD', ['app.py'], tmp_path / 'bundle', brief='another')


@pytest.mark.parametrize('selected', ['.', '../other', '/tmp/secret', '.env', 'CLAUDE.md',
                                     '.codex/config.toml', 'docs/handoffs/evidence',
                                     '.cursor/rules/work.mdc', '.cursorrules', 'GEMINI.md',
                                     '.github/copilot-instructions.md', '.aider.chat.history.md'])
def test_rejects_context_or_escaping_selections(source, tmp_path, selected):
    with pytest.raises(ValueError):
        freeze_input(source, 'HEAD', [selected], tmp_path / 'bundle', brief='task')


@pytest.mark.parametrize('selected', ['link.py', 'absent.py'])
def test_rejects_symlinks_and_missing_inputs(source, tmp_path, selected):
    with pytest.raises(ValueError):
        freeze_input(source, 'HEAD', [selected], tmp_path / 'bundle', brief='task')
    assert not (tmp_path / 'bundle').exists()
