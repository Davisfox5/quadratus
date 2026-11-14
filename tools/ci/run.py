"""Execute exactly one planned shard and retain live progress and phase timings."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import pytest
from plan import snapshot

# The recorder writes between a test's call and its teardown, while whatever
# the test replaced is still in place. Tests here replace file primitives
# (Path.open, os.replace) to prove a reader's bound or a failed write, so the
# recorder keeps the real ones from import time and never goes through Path.
_open = open
_replace = os.replace


class Recorder:
    def __init__(self, out):
        self.out = out
        self.start = time.monotonic()
        self.phases = []
        self.collected = 0
        self.finished = 0

    def event(self, kind, **fields):
        row = dict(event=kind, elapsed=time.monotonic() - self.start, **fields)
        with _open(self.out / 'events.jsonl', 'a') as f:
            f.write(json.dumps(row) + '\n')
        tmp = self.out / 'current.tmp'
        with _open(tmp, 'w') as f:
            f.write(json.dumps(dict(row, finished=self.finished, collected=self.collected)))
        _replace(tmp, self.out / 'current.json')

    def pytest_collection_finish(self, session):
        self.collected = len(session.items)
        self.event('collected', nodeids=[i.nodeid for i in session.items])

    def pytest_runtest_logstart(self, nodeid, location):
        self.event('test_start', nodeid=nodeid)

    def pytest_runtest_logreport(self, report):
        self.phases.append(dict(nodeid=report.nodeid, phase=report.when, outcome=report.outcome, seconds=report.duration))
        if report.when == 'teardown':
            self.finished += 1
        self.event('phase', **self.phases[-1])

    def finish(self, code, **context):
        durations = {}
        for row in self.phases:
            path = row['nodeid'].split('::')[0]
            durations[path] = durations.get(path, 0) + row['seconds']
        self.event('returned', exit_code=code)
        (self.out / 'durations.json').write_text(json.dumps(durations, indent=2) + '\n')
        (self.out / 'receipt.json').write_text(json.dumps(dict(exit_code=code, seconds=time.monotonic()-self.start,
                                            collected=self.collected, finished=self.finished, phases=self.phases, **context), indent=2) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plan', type=Path, required=True)
    ap.add_argument('--shard', type=int, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if os.environ.get('PYTEST_ADDOPTS', '').strip():
        raise SystemExit('PYTEST_ADDOPTS must be empty: the plan controls test selection')
    args.plan = args.plan.resolve()
    args.output = args.output.resolve()
    root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
    os.chdir(root)
    sys.path.insert(0, str(root))
    plan_bytes = args.plan.read_bytes()
    plan = json.loads(plan_bytes)
    if snapshot(root) != plan['source_sha256']:
        raise SystemExit('Planned source inputs changed; create a fresh plan')
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() != plan['head']:
        raise SystemExit('Plan HEAD differs from current source')
    shard = next(s for s in plan['shards'] if s['index'] == args.shard)
    if not shard['files']:
        raise SystemExit('Empty shard must not invoke pytest full discovery')
    for path in shard['files']:
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != plan['files_sha256'][path]:
            raise SystemExit(f'Planned test changed: {path}')
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'intent.json').write_text(json.dumps(dict(plan_head=plan['head'], shard=shard, pid=os.getpid())))
    recorder = Recorder(args.output)
    code = int(pytest.main(['-q', '-ra', '-o', 'addopts=', '--durations=15', '-p', 'no:cacheprovider',
                           '--junitxml=' + str(args.output / 'junit.xml'), *shard['files']], plugins=[recorder]))
    source_unchanged = snapshot(root) == plan['source_sha256']
    if not source_unchanged:
        code = code or 1
    recorder.finish(code, plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
                    plan_head=plan['head'], coverage=plan['coverage'], shard=shard,
                    python=platform.python_version(), source_unchanged=source_unchanged)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
