"""Probe evidence must survive container cleanup without copying unrelated sessions."""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'native_probe', Path(__file__).parents[1] / 'tools/acceptance/native_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_retains_only_exact_parent_and_explicitly_linked_same_workspace_child(tmp_path):
    sessions = tmp_path / 'sessions'
    dated = sessions / '2026/09/15'
    dated.mkdir(parents=True)
    work = tmp_path / 'work'
    work.mkdir()
    for name, parent, cwd in [('parent', None, work), ('child', 'parent', work),
                              ('unrelated', 'someone-else', work),
                              ('wrong-cwd', 'parent', tmp_path / 'elsewhere')]:
        meta = {'id': name, 'cwd': str(cwd),
                'source': {'subagent': {'thread_spawn': {'parent_thread_id': parent}}}}
        (dated / f'rollout-{name}.jsonl').write_text(
            json.dumps({'type': 'session_meta', 'payload': meta}) + '\nprivate body\n')
    (dated / 'rollout-link.jsonl').symlink_to(dated / 'rollout-parent.jsonl')
    destination = tmp_path / 'evidence'
    retained = probe.retain_rollouts(sessions, 'parent', work, destination)
    assert set(retained) == {'rollout-parent.jsonl', 'rollout-child.jsonl'}
    for name in retained:
        assert (destination / name).read_bytes() == (dated / name).read_bytes()
        assert (destination / name).stat().st_mode & 0o077 == 0
    assert destination.stat().st_mode & 0o077 == 0
