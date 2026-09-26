"""The project workflow shared by the terminal and the GUI."""

from __future__ import annotations

import fcntl
import json
import re
import shlex
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import ArtifactStore
from .codebase_map import CodebaseMap
from .delegation import DelegationLedger, reconcile
from .integration import GateSuite, IntegrationGate
from .policy import load_policy
from .project import Project
from .providers import ProviderError
from .repo_scan import scan_repo, seed_map
from .run_budget import RunBudget
from .session import SessionConfig
from .usage import UsageMeter
from .workers import WorkerBudget


@dataclass
class ProjectResult:
    completed: bool
    report: str
    run_dir: Path
    diff: str
    error: str = ''


@contextmanager
def _project_lock(project):
    folder = project.root / '.quadratus'
    folder.mkdir(exist_ok=True)
    with (folder / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProviderError('Another Quadratus run is using this project.') from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_project(goal, project, settings, *, allow_writes=False, check='',
                state_dir=None, max_tasks=20, mode='adversarial',
                progress=None, ask_operator=None, plan_gate=None,
                default_scope=None, run_limits=None, forbid=(), declared_paths=(),
                security_verdict_json=False, gates=None):
    """Keep both successful and interrupted runs next to their source tree."""
    from .runtime import Fleet, new_session

    if not goal.strip():
        raise ValueError('Describe the project change to make.')
    if max_tasks < 1:
        raise ValueError('max_tasks must be at least 1')
    if not isinstance(project, Project):
        project = Project(project)
    state = Path(state_dir).expanduser() if state_dir else Path('.quadratus')
    if not state.is_absolute():
        state = project.root / state
    state = state.resolve()
    if state == project.root or project.root.is_relative_to(state):
        raise ValueError('Run state must not contain the project source folder.')
    project.exclude.add(state)

    policy = load_policy(project.root, forbid=forbid)
    if declared_paths:
        from .scope import TaskScope
        if default_scope is not None:
            raise ValueError('Use either default_scope or declared_paths, not both')
        default_scope = TaskScope(permitted_paths=tuple(declared_paths))
    default_scope = policy.scope(default_scope)
    with _project_lock(project):
        return _run(goal, project, settings, state=state, allow_writes=allow_writes,
                    check=check, max_tasks=max_tasks, mode=mode, progress=progress,
                    ask_operator=ask_operator, plan_gate=plan_gate,
                    default_scope=default_scope,
                    run_limits=run_limits, policy=policy, gates=gates,
                    security_verdict_json=security_verdict_json,
                    fleet_type=Fleet, session_factory=new_session)


def _project_check_command(argv, root):
    """Expose only a project-owned check, never an external examiner path.

    A conventional interpreter/runner may live outside the project; script,
    config and other path arguments must remain inside it. Unsupported forms
    still run through the integration gate without a model-side allowance.
    """
    if not argv:
        return None
    for index, argument in enumerate(argv):
        value = argument.split('=', 1)[-1] if argument.startswith('-') else argument
        if index == 0 and Path(value).is_absolute():
            if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?|pytest|node|npm|npx|uv', Path(value).name):
                continue
        # A joined flag such as -I/private/path is conservative runner-only.
        if value.startswith('-') and '/' in value:
            return None
        if not (root / value).resolve().is_relative_to(root):
            return None
    return shlex.join(argv)


def _run(goal, project, settings, *, state, allow_writes, check, max_tasks,
         mode, progress, ask_operator, plan_gate, fleet_type, session_factory,
         default_scope=None, run_limits=None, policy=None, gates=None, security_verdict_json=False):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run_dir = state / 'runs' / f'{stamp}-{uuid.uuid4().hex[:8]}'
    run_dir.mkdir(parents=True)
    before = project.contents()
    scan = scan_repo(project.root)
    code_map = CodebaseMap(state / 'codebase-map.jsonl')
    seed_map(scan, code_map)
    command = shlex.split(check) if check else scan.check_command
    gate = (GateSuite(gates, cwd=project.root, exclude=project.exclude) if gates is not None
            else IntegrationGate(command, cwd=project.root) if command else None)
    # Only checks configured at the selected project root can be granted to
    # an editing lead. Nested/skipped gates still run through the gate suite.
    check_commands = (tuple(_project_check_command(c.argv, project.root) for c in gate.commands
                            if c.argv and not c.skip_reason
                            and (project.root / c.cwd).resolve() == project.root)
                      if isinstance(gate, GateSuite)
                      else (_project_check_command(command, project.root),) if command else ())
    check_commands = tuple(c for c in check_commands if c is not None)
    store = ArtifactStore(run_dir / 'artifacts')
    meter = UsageMeter(run_dir / 'usage.jsonl')
    delegation = DelegationLedger(path=run_dir / 'invocations.jsonl')
    budget = RunBudget(run_limits, path=run_dir / 'budget.json') if run_limits else None
    config = SessionConfig(
        project=project.root, project_excludes=tuple(project.exclude),
        allow_writes=allow_writes, mode=mode, integration_gate=gate,
        codebase_map=code_map, ask_operator=ask_operator, plan_gate=plan_gate,
        progress=progress, delegation_ledger=delegation,
        default_scope=default_scope, repository_policy=policy,
        security_verdict_json=security_verdict_json,
    )
    preview = policy.resolve(default_scope.permitted_paths if default_scope else (),
                             writing=allow_writes) if policy else None
    (run_dir / 'policy-plan.json').write_text(json.dumps(preview, indent=2), encoding='utf-8')
    session, error = None, ''
    if run_limits:
        config.worker_budget = WorkerBudget(
            # This permissive per-task ceiling cannot widen the shared run cap:
            # every worker transport attempt also reserves from RunBudget.
            max_per_task=run_limits.max_calls,
            max_concurrent=run_limits.max_concurrent_workers,
        )
    fleet = fleet_type(settings, project=project, allow_writes=allow_writes,
                       check_commands=check_commands,
                       usage_meter=meter, delegation_ledger=delegation,
                       **({'run_budget': budget} if budget else {}))
    try:
        fleet.progress = progress  # one live line per call as it ends
    except Exception:  # noqa: BLE001 -- a fake fleet may refuse attributes
        pass
    in_flight = {}
    try:
        if preview and preview['blocked']:
            raise ValueError('; '.join(preview['blocked']))
        if progress:
            progress(f'Project: {project.root}; edits {"enabled" if allow_writes else "disabled"}')
        session = session_factory(
            goal, store, fleet=fleet, config=config,
            invariants=['Project source is available in the working directory. '
                        'Use actual files as evidence. Do not commit or publish changes.'],
        )
        session.run(max_tasks=max_tasks)
    except (Exception, KeyboardInterrupt) as exc:  # persist partial work and its cause
        error = f'{type(exc).__name__}: {exc}'
        # A stopped editing call already inspected the tree and kept whatever
        # was there. Carrying that forward is what makes the handoff resumable
        # rather than merely honest about having failed.
        partial = getattr(exc, 'partial', None)
        in_flight = dict(getattr(session, 'in_flight', {}) or partial or {})
        if in_flight:
            (run_dir / 'in-flight.json').write_text(json.dumps(in_flight, indent=2), encoding='utf-8')
    finally:
        fleet.close()
    if not error and session is not None and getattr(session, 'stop_reason', ''):
        # A stop the session chose (the turn-limit breaker) rather than one an
        # exception forced. Reported as the error so the result says why, and
        # the capped tasks' preserved edits are listed as in-flight work.
        error = session.stop_reason
        records = list((getattr(session, 'turn_limited_records', {}) or {}).values())
        changed = sorted({name for r in records for name in r.get('changed') or []})
        in_flight = dict(
            note='Stopped by the turn-limit breaker; every capped task\'s edits are preserved.',
            changed=changed, changed_lines=sum(r.get('changed_lines') or 0 for r in records),
            turn_limited=records)
        (run_dir / 'in-flight.json').write_text(json.dumps(in_flight, indent=2), encoding='utf-8')
    # What each call did inside its own session, from the vendors' transcripts.
    # Collected after the run so a slow copy never delays a model call.
    traces = []
    try:
        from .trace import build_traces
        traces = build_traces(run_dir, run_dir / 'invocations.jsonl', project.root)
    except Exception:  # noqa: BLE001 -- tracing never fails a run
        traces = []
    diff = project.diff(before)
    completed = bool(session and session.completed and not error)
    checks = session.checks if session else []
    ledger = session.memory.render(current='') if session else ''
    status = 'Goal reported complete' if completed else 'Run incomplete'
    lines = [f'# {status}', '', f'Project: {project.root}', '',
             f'Edits: {"enabled" if allow_writes else "disabled"}', '',
             f'Run files: {run_dir}', '']
    if preview:
        lines += [f"Default family: {preview['primary_family']}", '',
                  f"Policy plan: {preview['hash']}", '']
    if error:
        lines += [f'Error: {error}', '']
    if in_flight:
        changed = in_flight.get('changed') or []
        lines += ['## In-flight work when the run stopped', '',
                  in_flight.get('note', ''), '']
        if changed:
            lines += [f'Already written and preserved ({len(changed)} file(s), '
                      f'{in_flight.get("changed_lines", 0)} line(s)):', '',
                      *[f'- {name}' for name in changed], '']
    if not completed and not error:
        lines += ['The plan was declined, the task limit was reached, or findings/checks remain open.', '']
    lines += ['Source changes are saved in the project folder.' if diff else
              'No source changes were produced. Any passing checks describe the existing tree.', '']
    if checks:
        for result in checks:
            lines += [f'Check {"PASSED" if result["passed"] else "FAILED"}: {result["command"]}',
                      '', f'Checked folder: {result["cwd"]}', '', result['output'], '']
    else:
        lines += ['No integration check ran; this result has not been test-verified.', '']
    lines += [meter.render_report(), '']
    # Who actually ran, kept apart from who was selected, and from what the
    # harness could only witness. Reported next to the API-price counterfactual
    # rather than merged into it: they answer different questions.
    if traces:
        from .trace import render_timeline
        lines += [render_timeline(traces), '']
    lines += [delegation.render_report(), '', '## Task ledger', '', ledger]
    report = '\n'.join(lines)
    (run_dir / 'changes.diff').write_text(diff, encoding='utf-8')
    (run_dir / 'ledger.md').write_text(ledger, encoding='utf-8')
    (run_dir / 'report.md').write_text(report, encoding='utf-8')
    (run_dir / 'result.json').write_text(json.dumps({
        'project': str(project.root), 'goal': goal, 'completed': completed,
        'allow_writes': allow_writes, 'error': error, 'checks': checks,
        'source_changed': bool(diff), 'source_fingerprint': project.fingerprint(),
        'tasks': len(session.history) if session else 0,
        'turn_limited_tasks': list(getattr(session, 'turn_limited', []) or []) if session else [],
        'personal_preferences': _preferences_record(settings),
        'requirements': _requirements_record(session),
        'design_checks': list(getattr(session, 'design_checks', []) or []) if session else [],
        'parallel_batches': list(getattr(session, 'parallel_batches', []) or []) if session else [],
        'trace': {'calls': len(traces),
                  'transcripts_found': sum(1 for t in traces if t.get('tool_calls') is not None),
                  'calls_outside_project': sum(1 for t in traces if t.get('outside_project')),
                  'unserved_requests': sum(len(t.get('protocol_attempts') or []) for t in traces),
                  'injected_rules': sorted({r['source'] for t in traces for r in t.get('injected_rules') or []})},
        'in_flight': in_flight,
        'policy_preview': preview,
        'policy_plans': getattr(session, 'policy_plans', []),
        'budget': budget.snapshot() if budget else None,
        'delegation': reconcile(delegation.events, delegation.native_children.values()),
        'scope_reports': [
            {'within_scope': r.within_scope, 'out_of_scope': r.out_of_scope,
             'changed': r.changed, 'changed_lines': r.changed_lines, 'max_lines': r.max_lines,
             'code_lines': r.code_lines, 'test_lines': r.test_lines, 'oversized': r.oversized}
            for r in (session.scope_reports if session else [])
        ],
    }, indent=2), encoding='utf-8')
    (run_dir / 'delegation.md').write_text(delegation.render_report(), encoding='utf-8')
    return ProjectResult(completed, report, run_dir, diff, error)


def _requirements_record(session):
    if session is None:
        return None
    ledger = session.memory.ledger
    return {"listed": dict(ledger.requirements), "status": dict(ledger.requirement_status),
            "ambiguous": dict(ledger.ambiguous),
            "decided_by_orchestrator": dict(ledger.decisions),
            "operator_rulings": list(ledger.rulings),
            "reviews": list(getattr(session, "requirement_reviews", []) or []),
            "audits": list(getattr(session, "requirement_audits", []) or [])}


def _preferences_record(settings=None):
    """What this run asked for about the operator's personal CLI configuration.

    An intent, not an observation: the flags below were passed, and what each
    CLI then loaded is not independently verified here (Codex review of #25).
    """
    from .cli_providers import CLAUDE_SPEC, CODEX_SPEC, GROK_SPEC, neutral_preferences
    requested = (getattr(settings, "neutral_preferences", False) if settings is not None
                 else neutral_preferences())
    specs = (CLAUDE_SPEC, CODEX_SPEC, GROK_SPEC)
    if not requested:
        return {"requested": "personal configuration not suppressed",
                "note": "each CLI loaded whatever user configuration and account rules it has"}
    return {"requested": "neutral", "observed": "not verified per CLI",
            "flags_passed": {s.vendor: list(s.neutral_args) for s in specs},
            "not_removable": [s.neutral_gap for s in specs if s.neutral_gap]}


def standing_rulings(path, fallback=None):
    """An ask_operator that answers from rulings the operator gave in advance.

    The file is JSON: a list of {"about": "<regex>", "answer": "<text>"}. A
    question matching an entry gets its answer, recorded as a standing ruling
    like any other; anything else goes to ``fallback`` (an interactive
    prompt), or stops the run with OperatorInputNeeded. A blind run can then
    carry answers the operator already gave without anyone at the keyboard.
    """
    import json as _json
    import re as _re
    entries = _json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and isinstance(e.get("about"), str) and isinstance(e.get("answer"), str)
            for e in entries):
        raise ValueError(f"{path}: expected a list of {{'about': regex, 'answer': text}}")

    def ask(question: str) -> str:
        for entry in entries:
            if _re.search(entry["about"], question, _re.IGNORECASE):
                return entry["answer"]
        if fallback is not None:
            return fallback(question)
        from .session import OperatorInputNeeded
        raise OperatorInputNeeded(question)
    return ask
