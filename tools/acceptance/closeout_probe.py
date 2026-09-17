"""One unscored closeout call from supplied evidence, inside run_isolated only."""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from quadratus import cli_providers as cli
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger, invocation
from quadratus.run_budget import RunBudget, RunLimits
from quadratus.runtime import Fleet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('seat', choices=['grok:default', 'claude:fable'])
    args = parser.parse_args()
    if not Path('/.dockerenv').exists() or os.environ.get('HOME') != '/tmp/solver-home':
        parser.error('Requires run_isolated with its external watchdog')
    os.umask(0o077)
    os.environ['QUADRATUS_NATIVE_DELEGATION'] = 'off'
    root = Path('/work')
    prompt = (root / 'prompt.txt').read_text()
    budget = RunBudget(RunLimits(max_calls=1, max_reported_tokens=50_000,
                                wall_seconds=70, max_concurrent_workers=1), path=root / 'budget.json')
    ledger = DelegationLedger(path=root / 'invocations.jsonl')
    fleet = Fleet(Settings(backend='cli', backend_overrides={}, openai_api_key=None,
                           anthropic_api_key=None, xai_api_key=None),
                  project=root, run_budget=budget, delegation_ledger=ledger)
    original = cli._launch
    launches = []

    def capture(argv, **kwargs):
        cwd = Path(kwargs['cwd'])
        launches.append({'argv': argv, 'cwd_empty': not list(cwd.iterdir()),
                         'timeout': kwargs.get('timeout')})
        response = original(argv, **kwargs)
        (root / 'stdout.private').write_text(response.stdout or '')
        (root / 'stderr.private').write_text(response.stderr or '')
        return response

    cli._launch = capture
    started = time.monotonic()
    result = {'seat': args.seat, 'scored': False,
              'prompt_bytes': len(prompt.encode()),
              'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}
    try:
        with invocation('saved-task-closeout-replay', 'closeout'):
            reply = fleet.invoke(args.seat, prompt)
        (root / 'reply.txt').write_text(reply)
        result['outcome'] = 'returned'
    except Exception as exc:
        result['outcome'] = type(exc).__name__
        (root / 'error.private').write_text(str(exc))
    finally:
        cli._launch = original
        result.update(elapsed_seconds=time.monotonic()-started, budget=budget.snapshot(),
                      launches=launches)
        (root / 'summary.private.json').write_text(json.dumps(result, indent=2)+'\n')
    return 0 if result['outcome'] == 'returned' else 1


if __name__ == '__main__':
    raise SystemExit(main())
