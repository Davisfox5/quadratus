"""Execute one prepared blind project run inside the isolated supervisor.

Mount only the frozen application and engine. Examiner cases and collaboration
history belong outside both mounts. Native envelopes remain private evidence.
"""

import json
import os
import shutil
import threading
from dataclasses import asdict
from pathlib import Path

from quadratus import cli_providers as cli
from quadratus.config import Settings
from quadratus.project_run import run_project
from quadratus.run_budget import RunLimits


def main():
    if not Path('/.dockerenv').exists() or os.environ.get('HOME') != '/tmp/solver-home':
        raise SystemExit('Run only inside run_isolated with its external watchdog')
    os.umask(0o077)
    os.environ['QUADRATUS_NATIVE_DELEGATION'] = 'off'
    # This process has already proved it is inside run_isolated's container,
    # which is the security boundary: read-only root, no capabilities, no new
    # privileges, writes confined to /work and a tmpfs. A vendor's own inner
    # sandbox is redundant here, and Codex's cannot start at all -- see
    # cli_providers.contained for the measurement and the trade.
    os.environ['QUADRATUS_CONTAINED'] = '1'
    project = Path('/work')
    state = project / '.quadratus'
    evidence = state / 'native-private'
    evidence.mkdir(parents=True, mode=0o700)
    settings = Settings(backend='cli', backend_overrides={}, openai_api_key=None,
                        anthropic_api_key=None, xai_api_key=None)
    limits = RunLimits(max_calls=24, max_reported_tokens=500_000, wall_seconds=900,
                       max_concurrent_workers=2)
    (state / 'effective-config.json').write_text(json.dumps({
        'settings': asdict(settings), 'run_limits': asdict(limits),
        'native_delegation': 'off', 'mode': 'adversarial', 'allow_writes': True,
        'check': 'python -m pytest -q', 'max_tasks': 20,
        'operator_input': None, 'plan_gate': None,
    }, indent=2) + '\n')
    original = cli._launch
    lock = threading.Lock()
    next_call = 0

    def capture(*args, **kwargs):
        nonlocal next_call
        with lock:
            next_call += 1
            prefix = evidence / f'call-{next_call:03d}'
        try:
            response = original(*args, **kwargs)
            prefix.with_suffix('.stdout').write_text(response.stdout or '')
            prefix.with_suffix('.stderr').write_text(response.stderr or '')
            return response
        except BaseException as exc:
            prefix.with_suffix('.error').write_text(f'{type(exc).__name__}: {exc}')
            raise

    def progress(message):
        with (state / 'progress.log').open('a') as stream:
            stream.write(str(message) + '\n')

    cli._launch = capture
    try:
        rulings = Path(__file__).with_name('rulings.json')
        ask = None
        if rulings.exists():
            from quadratus.project_run import standing_rulings
            ask = standing_rulings(rulings)
        result = run_project((project / 'TASK.md').read_text(), project, settings,
                             allow_writes=True, check='python -m pytest -q',
                             progress=progress, run_limits=limits, ask_operator=ask)
        (state / 'launcher-result.json').write_text(json.dumps({
            'completed': result.completed, 'error': result.error,
            'run_dir': str(result.run_dir),
        }, indent=2) + '\n')
        return 0 if result.completed else 2
    finally:
        cli._launch = original
        # This HOME began empty except for subscription auth. Copy session
        # evidence only, never credential/config directories themselves.
        for vendor, relative in [('codex', '.codex/sessions'),
                                 ('claude', '.claude/projects'),
                                 ('grok', '.grok/sessions')]:
            source = Path.home() / relative
            if source.is_dir():
                target = evidence / vendor
                target.mkdir(mode=0o700)
                for file in source.rglob('*'):
                    if file.is_file() and not file.is_symlink():
                        out = target / file.relative_to(source)
                        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        shutil.copyfile(file, out)
                        out.chmod(0o600)


if __name__ == '__main__':
    raise SystemExit(main())
