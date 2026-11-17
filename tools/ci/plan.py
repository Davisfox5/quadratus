"""Conservative file impact selection and deterministic, file-atomic CI shards.

No tests or project modules are imported to plan a run. Unknown changes fall
back to the full suite; selection is optimization evidence, not acceptance.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

SMOKE = ('tests/test_run_budget.py', 'tests/test_prompt_rule_parity.py')
#: The reasons of a selection made by impact routing; any other reason is a
#: full fallback (an explicit --full, shared configuration, an unknown path,
#: an unresolvable import or an unreachable change).
SELECTED = ('transitive imports, literal module/path references, changed tests and contract smoke',
            'documentation/metadata only; lint still required')
GLOBAL = {'pyproject.toml', 'pytest.ini', 'setup.cfg', 'setup.py', 'requirements.txt',
          'requirements-dev.txt', 'tests/conftest.py'}


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


def module(path):
    name = str(Path(path).with_suffix('')).replace('/', '.')
    return name.removesuffix('.__init__')


def reference_index(modules):
    spellings = {m: m for m in modules}
    spellings.update({m.replace('.', '/') + '.py': m for m in modules})
    matcher = re.compile('|'.join(re.escape(s) for s in sorted(spellings, key=len, reverse=True)))
    basenames = {}
    for m, path in modules.items():
        basenames.setdefault(Path(path).name, set()).add(m)
    return spellings, matcher, basenames


def dependencies(path, source, modules, index=None):
    """Imports (including inside functions), relative imports and literal refs."""
    tree = ast.parse(source)
    spellings, matcher, basenames = index or reference_index(modules)
    found = set()
    own = module(path)
    package = own if Path(path).name == '__init__.py' else own.rpartition('.')[0]
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ''
            if node.level:
                parent = package.split('.')[:len(package.split('.')) - node.level + 1]
                prefix = '.'.join(parent + ([prefix] if prefix else []))
            names = [prefix] + [f'{prefix}.{n.name}' for n in node.names]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Also covers importlib, monkeypatch targets and subprocess scripts.
            names = [spellings[match.group()] for match in matcher.finditer(node.value)]
            found.update(basenames.get(node.value, set()))
        for name in names:
            parts = name.split('.')
            found.update('.'.join(parts[:i]) for i in range(1, len(parts) + 1) if '.'.join(parts[:i]) in modules)
    return found


def select(root, changed, *, full=False):
    paths = sorted({p for p in git(root, 'ls-files', '-z').split('\0') if p}
                   | {p for p in git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0') if p.endswith('.py')})
    tests = sorted(p for p in paths if p.startswith('tests/') and Path(p).name.startswith('test_') and p.endswith('.py') and (root / p).is_file())
    sources = {p: (root / p).read_text() for p in paths if p.endswith('.py') and (root / p).is_file()}
    modules = {module(p): p for p in sources}
    reasons = []
    if full:
        reasons.append('explicit full compatibility run')
    if any(p in GLOBAL or Path(p).name == 'conftest.py' or p.startswith(('.github/', 'tools/ci/')) for p in changed):
        reasons.append('shared test, dependency or CI configuration changed')
    def docs(p):
        return p.endswith(('.md', '.rst', '.txt')) and not p.startswith(('quadratus/', 'tests/')) and p not in GLOBAL
    referenced = {p for p in changed if any(p in s or Path(p).name in s for s in sources.values())}
    executable = [p for p in changed if (not docs(p) or p in referenced) and (p != 'docs/dev-record.json' or p in referenced)]
    unknown = [p for p in executable if p not in sources]
    if unknown:
        reasons.append('unknown, removed or non-Python contract paths: ' + ', '.join(unknown))
    if reasons:
        return tests, tests, reasons
    if not executable:
        return [], tests, ['documentation/metadata only; lint still required']
    try:
        index = reference_index(modules)
        graph = {p: dependencies(p, source, modules, index) for p, source in sources.items()}
    except (SyntaxError, ValueError) as exc:
        return tests, tests, [f'cannot resolve imports: {exc}; full fallback']
    def closure(start):
        reached = set(start)
        while True:
            affected_modules = {module(p) for p in reached}
            more = {p for p, imports in graph.items() if imports & affected_modules} - reached
            if not more:
                return reached
            reached |= more

    selected = set()
    # Each changed path needs a route to a test; another tested edit cannot mask it.
    for path in executable:
        consumers = set(tests) & closure([path])
        if not consumers:
            return tests, tests, [f'no reachable tests for {path}; full fallback']
        selected |= consumers
    if not selected:
        return tests, tests, ['no affected tests resolved; full fallback']
    selected.update(p for p in SMOKE if p in tests)
    return sorted(selected), tests, ['transitive imports, literal module/path references, changed tests and contract smoke']


def split(root, selected, count, durations=None):
    """Longest-first bins; never split a file's module fixtures or ordering."""
    if not 1 <= count <= 8:
        raise ValueError('shards must be between 1 and 8')
    bins = [[] for _ in range(min(count, len(selected)))]
    loads = [0.0 for _ in bins]
    weights = {}
    for path in selected:
        # A measured duration improves packing; stale data can only affect speed.
        measured = (durations or {}).get(path)
        if isinstance(measured, (int, float)) and measured > 0:
            weights[path] = float(measured)
        else:
            try:
                tree = ast.parse((root / path).read_text())
                cases = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith('test_') for n in ast.walk(tree))
            except (SyntaxError, ValueError):
                cases = 1  # Collection/lint reports the real syntax error.
            weights[path] = max(1, cases) * (4 if '/lifecycle/' in path else 1)
    for path in sorted(selected, key=lambda p: (-weights[p], p)):
        i = min(range(len(bins)), key=lambda j: (loads[j], j))
        bins[i].append(path)
        loads[i] += weights[path]
    return [dict(index=i, files=sorted(files), weight=loads[i]) for i, files in enumerate(bins)]


def snapshot(root):
    """Bind results to tracked inputs plus new Python sources during local edits."""
    paths = set(git(root, 'ls-files', '-z').split('\0'))
    paths.update(p for p in git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
                 if p.endswith('.py'))
    return {p: hashlib.sha256((root / p).read_bytes()).hexdigest()
            if (root / p).is_file() else None for p in sorted(paths) if p}


def changes_since(previous, current):
    """Only an aggregate-success receipt may advance the incremental baseline."""
    if not previous.get('validated_run') or not previous.get('head'):
        raise ValueError('previous plan lacks a successful aggregate receipt')
    old = previous.get('source_sha256')
    if not isinstance(old, dict) or not old:
        raise ValueError('previous plan lacks source fingerprints')
    return sorted(p for p in old.keys() | current.keys() if old.get(p) != current.get(p))


def build(root, changed, *, full=False, shards=8, durations=None):
    selected, all_tests, reasons = select(root, changed, full=full)
    groups = split(root, selected, shards, durations)
    assigned = [p for g in groups for p in g['files']]
    assert sorted(assigned) == sorted(selected) and len(assigned) == len(set(assigned))
    return dict(version=1, head=git(root, 'rev-parse', 'HEAD'), changed=sorted(changed), reasons=reasons,
                source_sha256=snapshot(root), selected=selected, omitted=sorted(set(all_tests) - set(selected)), shards=groups,
                files_sha256={p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in selected},
                coverage='focused' if len(selected) < len(all_tests) else 'full',
                fallback=not all(r in SELECTED for r in reasons),
                limitation='Static impact selection cannot prove arbitrary dynamic dependencies. Full compatibility runs retain that coverage.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base')
    ap.add_argument('--full', action='store_true')
    ap.add_argument('--previous', type=Path, help='Same-PR plan saved after aggregate success')
    ap.add_argument('--shards', type=int, default=8)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--durations', type=Path)
    args = ap.parse_args()
    root = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel'))
    changed = git(root, 'diff', '--name-only', args.base, 'HEAD').splitlines() if args.base else []
    full = args.full or not args.base
    baseline = {'kind': 'git-base', 'ref': args.base}
    if args.previous and not args.full:
        try:
            previous = json.loads(args.previous.read_text())
            changed = changes_since(previous, snapshot(root))
            full = False
            baseline = {'kind': 'successful-same-pr-source', 'head': previous['head'],
                        'validated_run': previous['validated_run']}
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            full = True
            baseline = {'kind': 'unusable-previous-receipt-full-fallback'}
    plan = build(root, changed, full=full, shards=args.shards,
                 durations=json.loads(args.durations.read_text()) if args.durations else None)
    plan['baseline'] = baseline
    args.output.write_text(json.dumps(plan, indent=2) + '\n')
    print(json.dumps(dict(selected=len(plan['selected']), omitted=len(plan['omitted']), reasons=plan['reasons'])))


if __name__ == '__main__':
    main()
