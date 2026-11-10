"""One local impact-selected run; no full suite or second Python by default."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from plan import build, git


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True, help='Last validated commit or PR base')
    ap.add_argument('--output', type=Path, required=True, help='New receipt directory')
    ap.add_argument('--full', action='store_true', help='Explicit full regression')
    args = ap.parse_args()
    root = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel'))
    # Includes committed, staged, unstaged and new Python edits.
    changed = set(git(root, 'diff', '--name-only', args.base).splitlines())
    changed.update(p for p in git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0') if p.endswith('.py'))
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    plan = build(root, sorted(changed), full=args.full, shards=1)
    path = args.output / 'plan.json'
    path.write_text(json.dumps(plan, indent=2) + '\n')
    print(f"Selected {len(plan['selected'])} test files; omitted {len(plan['omitted'])}. " + '; '.join(plan['reasons']), flush=True)
    if plan['fallback'] and not args.full:
        # A full fallback is the eight-shard CI's to run, never a serial local
        # suite by default (PR73 6716159070): the plan is kept, nothing runs,
        # and the result is not a success. --full stays the explicit opt-in.
        (args.output / 'ci-required.json').write_text(json.dumps(dict(
            status='ci-required', reasons=plan['reasons'], selected=len(plan['selected']), head=plan['head'],
            note='full fallback: run the eight-shard CI; pass --full to run it here serially'), indent=2) + '\n')
        print('Full fallback: plan saved, no local pytest launched. Run the eight-shard CI for broad regression, '
              'or pass --full to run it here serially.', flush=True)
        return 3
    if not plan['shards']:
        print('No executable changes selected. No pytest invocation; lint remains separate.')
        return 0
    return subprocess.call([sys.executable, str(root / 'tools/ci/run.py'), '--plan', str(path),
                            '--shard', '0', '--output', str(args.output / 'results')], cwd=root)


if __name__ == '__main__':
    raise SystemExit(main())
