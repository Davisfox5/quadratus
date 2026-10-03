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
from .integration import GateCommand, GateSuite, IntegrationGate
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
                security_verdict_json=False, gates=None, extra_checks=(), capture_profile=None,
                readiness=None, survey=None, direct_tier=False, tasks=None, decider=None,
                decider_labels="unstated"):
    """Keep both successful and interrupted runs next to their source tree.

    ``tasks`` is an operator-written task list (explicit-task entry): each
    text is read as an orchestrator reply would be and run in order under the
    same lifecycle, with no planner or acknowledgment call and no task the
    list did not name. The result's ``explicit_tasks`` section says which
    ran and that the goal was not judged; ``completed`` then means every
    listed task closed clean, not that the goal is proven.

    ``extra_checks`` are further operator checks, each an argv list (or a
    string split without a shell), run as required gates beside ``check``
    in the selected project. A pattern is never expanded: name the files.

    ``capture_profile`` is a path to the operator's preview profile
    (quadratus.preview), validated against the selected project here, before
    any model call; with it the harness captures UI tasks' renders itself.

    ``decider`` names an external routing decider (``"jev"``) consulted only
    where the orchestrator stated no usable label; ``None`` or ``"rule"`` is
    the deterministic default. Its calls are billed API calls, metered.
    ``decider_labels`` is ``"unstated"`` (the decider fills in only what the
    orchestrator left out) or ``"all"`` (the orchestrator is asked not to
    label, so the decider routes every planned task).

    ``readiness`` is the operator's capability readiness probes
    (quadratus.readiness): a JSON path or list, validated here and run once
    before the first model call. A failing probe stops the run as an
    operator handoff; a passing one proves readiness, never acceptance.
    """
    from .runtime import Fleet, new_session

    if not goal.strip():
        raise ValueError('Describe the project change to make.')
    if decider_labels not in ('unstated', 'all'):
        raise ValueError("decider_labels must be 'unstated' or 'all'")
    if max_tasks < 1:
        raise ValueError('max_tasks must be at least 1')
    if tasks is not None:
        tasks = list(tasks)
        if not tasks or any(not str(t).strip() for t in tasks):
            raise ValueError('tasks must name at least one task, each a non-empty text')
        if len(tasks) > max_tasks:
            raise ValueError(f'max_tasks ({max_tasks}) must cover the {len(tasks)} listed task(s)')
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
    extras = _extra_gate_commands(extra_checks)
    profile = None
    if capture_profile:
        from .preview import load_profile
        profile = load_profile(capture_profile, project.root)
    probes = ()
    if readiness:
        from .readiness import probes_from
        probes = probes_from(readiness, project.root)
    with _project_lock(project):
        return _run(goal, project, settings, state=state, allow_writes=allow_writes,
                    check=check, max_tasks=max_tasks, mode=mode, progress=progress,
                    ask_operator=ask_operator, plan_gate=plan_gate,
                    default_scope=default_scope,
                    run_limits=run_limits, policy=policy, gates=gates,
                    security_verdict_json=security_verdict_json,
                    fleet_type=Fleet, session_factory=new_session, extras=extras,
                    capture_profile=profile, readiness=probes, survey=survey, direct_tier=direct_tier,
                    tasks=list(tasks) if tasks is not None else None, decider=decider,
                    decider_labels=decider_labels)


#: Flags that change only how much a runner prints, never what it runs.
_QUIET_FLAGS = {'-q', '--quiet', '-v', '--verbose', '--silent'}


def _check_identity(argv):
    """A check's identity for de-duplication: exact argv, normalised only
    where the equivalence is proven. A Python interpreter's name, ``pytest``
    versus ``python -m pytest``, and output-only flags are normalised;
    anything that selects tests (a path, ``-k``) is kept, so a subset never
    stands in for a full suite (Codex review of 3ef9962).
    """
    if not argv:
        return ()
    head = Path(argv[0]).name
    rest = list(argv[1:])
    if re.fullmatch(r'python(\d+(\.\d+)*)?', head):
        head = 'python'
    elif head == 'pytest':
        head, rest = 'python', ['-m', 'pytest', *rest]
    # The declared harness report only adds output (integration.REPORT_TOKEN),
    # so a check that declares it is the same check as the scanned one.
    return (head, *[a for a in rest if a not in _QUIET_FLAGS and not _report_only(a)])


def _report_only(arg: str) -> bool:
    from .integration import REPORT_TOKEN
    return REPORT_TOKEN in arg


def _is_test_suite(argv) -> bool:
    """Whether a command is a test runner, whose success needs a count."""
    identity = _check_identity(argv)
    return (identity[:3] == ('python', '-m', 'pytest') or '--test' in identity
            or identity[:2] in (('npm', 'test'), ('npm', 't')))


def _extra_gate_commands(extra_checks):
    """Operator extra checks as required GateCommands, or ValueError.

    Codex, Run 15: the selected project documents its Node UI suite in prose
    and has no package.json, so nothing declared it; the operator names it
    instead. Same grant as ``--check``: run in the selected project, no
    shell. A glob is refused rather than passed through literally, since
    without a shell it would reach the runner unexpanded.
    """
    commands = []
    for index, raw in enumerate(extra_checks or (), 1):
        minimum = None
        if isinstance(raw, dict):
            minimum = raw.get('minimum_tests')
            raw = raw.get('argv') or ()
        argv = shlex.split(raw) if isinstance(raw, str) else list(raw)
        if not argv or any(not isinstance(a, str) or not a for a in argv):
            raise ValueError(f'Extra check {index} must be a nonempty command')
        if any(ch in a for a in argv for ch in '*?['):
            raise ValueError(f'Extra check {index} contains a pattern; list the files instead')
        # A test runner must show it ran something; a syntax or build check
        # has no count and keeps none (Codex review of 3ef9962).
        if minimum is None and _is_test_suite(argv):
            minimum = 1
        commands.append(GateCommand(id=f'extra-{index}', argv=tuple(argv), minimum_tests=minimum))
    return commands


def _with_declared_checks(command, scan):
    """The gate with the project's declared checks, when ``command`` does
    not already cover them; see :func:`_gate_plan`."""
    return _gate_plan(command, (), scan)


def _gate_plan(command, extras, scan):
    """Every required check for the run as GateCommands, or None when the
    single-command gate stands.

    ``command`` (the operator's ``--check`` or the scanned one) comes first,
    then operator extra checks, then each check the project declares that
    neither already names (Codex, Run 15: a package.json test script beside
    an operator pytest never ran). All are required: a missing runner is
    blocked and a failure fails the gate, so none can pass as complete, and
    an explicit ``--check`` cannot crowd the others out.
    """
    if not command and not extras:
        return None       # nothing named and nothing declared: no gate
    named = {_check_identity(command)} if command else set()
    named |= {_check_identity(e.argv) for e in extras}
    declared = [c for c in scan.declared_checks if _check_identity(c) not in named]
    if not extras and not declared:
        return None
    gates = ([GateCommand(id='check', argv=tuple(command),
                          minimum_tests=1 if _is_test_suite(command) else None)] if command else [])
    gates += list(extras)
    gates += [GateCommand(id='declared-' + Path(c[0]).name + (f'-{i}' if i else ''), argv=tuple(c),
                          minimum_tests=1)
              for i, c in enumerate(declared)]
    return gates


def _merge_extras(gates, extras):
    """Caller-supplied gates plus operator extras, never one silently
    dropping the other (Codex review of 3ef9962). A clashing id is refused
    before any call."""
    if not extras:
        return gates
    taken = {g.id for g in gates}
    clash = sorted(taken & {e.id for e in extras})
    if clash:
        raise ValueError(f'Extra checks clash with configured gate ids: {", ".join(clash)}')
    return [*gates, *extras]


def _run(goal, project, settings, *, state, allow_writes, check, max_tasks,
         mode, progress, ask_operator, plan_gate, fleet_type, session_factory,
         default_scope=None, run_limits=None, policy=None, gates=None, security_verdict_json=False,
         extras=(), capture_profile=None, readiness=(), survey=None, direct_tier=False, tasks=None,
         decider=None, decider_labels="unstated"):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run_dir = state / 'runs' / f'{stamp}-{uuid.uuid4().hex[:8]}'
    run_dir.mkdir(parents=True)
    before = project.contents()
    scan = scan_repo(project.root)
    code_map = CodebaseMap(state / 'codebase-map.jsonl')
    seed_map(scan, code_map)
    command = shlex.split(check) if check else scan.check_command
    gates = _merge_extras(list(gates), extras) if gates is not None else _gate_plan(command, extras, scan)
    plan = [dict(id=g.id, argv=list(g.argv), cwd=g.cwd, required=g.required, minimum_tests=g.minimum_tests)
            for g in gates or ()] or (
        [dict(id='check', argv=list(command), cwd='.', required=True,
              minimum_tests=1 if _is_test_suite(command) else None)] if command else [])
    # Shown to the operator before any task, and kept with the run. Never put
    # into a model prompt: a gate command can name an examiner path.
    (run_dir / 'gate-plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    if progress:
        progress('Checks: ' + ('; '.join(f"{g['id']}: {' '.join(g['argv'])}" for g in plan) or 'none'))
    gate = (GateSuite(gates, cwd=project.root, exclude=project.exclude) if gates is not None
            else IntegrationGate(command, cwd=project.root,
                                 minimum_tests=1 if _is_test_suite(command) else None) if command else None)
    store = ArtifactStore(run_dir / 'artifacts')
    meter = UsageMeter(run_dir / 'usage.jsonl')
    delegation = DelegationLedger(path=run_dir / 'invocations.jsonl')
    budget = RunBudget(run_limits, path=run_dir / 'budget.json') if run_limits else None
    if isinstance(decider, str) or decider is None:
        from .decisions import decider_from_name
        decider = decider_from_name(decider, meter=meter, budget=budget)
    elif budget is not None and getattr(decider, "budget", None) is None and hasattr(decider, "budget"):
        # A decider object handed in directly (tests, embedding callers) is
        # bound to this run's budget too: no billed call escapes the limits.
        decider.budget = budget
    config = SessionConfig(
        project=project.root, project_excludes=tuple(project.exclude),
        allow_writes=allow_writes, mode=mode, integration_gate=gate,
        lead_max_turns=getattr(settings, 'lead_max_turns', None),
        codebase_map=code_map, ask_operator=ask_operator, plan_gate=plan_gate,
        progress=progress, delegation_ledger=delegation,
        default_scope=default_scope, repository_policy=policy,
        security_verdict_json=security_verdict_json,
        capture_profile=capture_profile,
        readiness_probes=tuple(readiness or ()),
        survey=survey,
        direct_tier=direct_tier,
        decider=decider,
        decider_labels=decider_labels,
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
                       usage_meter=meter, delegation_ledger=delegation,
                       **({'run_budget': budget} if budget else {}))
    try:
        fleet.progress = progress  # one live line per call as it ends
        # The checks are the commands a granted editing call may run
        # unapproved and the denials that count as a capability failure.
        fleet.check_commands = tuple(shlex.join(g['argv']) for g in plan if g.get('argv'))
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
        session.run(max_tasks=max_tasks, **({'tasks': tasks} if tasks is not None else {}))
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
        # A stop the session chose rather than one an exception forced (the
        # turn-limit breaker, unverified design evidence). Reported as the
        # error so the result says why; a breaker stop also lists the capped
        # tasks' preserved edits as in-flight work.
        error = session.stop_reason
        records = list((getattr(session, 'turn_limited_records', {}) or {}).values())
        failed = list((getattr(session, 'failed_records', {}) or {}).values())
        if error.startswith(('TurnLimitBreaker', 'TaskFailureBreaker')) and (records or failed):
            changed = sorted({name for r in records + failed for name in r.get('changed') or []})
            in_flight = dict(
                note='Stopped by the unfinished-task breaker; every capped or failed task\'s edits are preserved.',
                changed=changed, changed_lines=sum(r.get('changed_lines') or 0 for r in records + failed),
                turn_limited=records, failed=failed)
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
    explicit = getattr(session, 'explicit', None) if session else None
    status = (('Listed tasks completed; the goal was not judged' if explicit is not None
               else 'Goal reported complete') if completed else 'Run incomplete')
    lines = [f'# {status}', '', f'Project: {project.root}', '',
             f'Edits: {"enabled" if allow_writes else "disabled"}', '',
             f'Run files: {run_dir}', '']
    if preview:
        lines += [f"Default family: {preview['primary_family']}", '',
                  f"Policy plan: {preview['hash']}", '']
    if error:
        lines += [f'Error: {error}', '']
    if explicit is not None:
        ran = [r['task'] for r in explicit.get('ran', [])]
        lines += [f"Explicit task list: {len(ran)} of {explicit.get('listed', 0)} listed task(s) ran"
                  + (f" ({', '.join(ran)})" if ran else '') + '. No orchestrator planned, acknowledged '
                  'or judged the goal; completion here means every listed task closed clean.', '']
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
        'failed_tasks': list(getattr(session, 'failed', []) or []) if session else [],
        'personal_preferences': _preferences_record(settings),
        'requirements': _requirements_record(session),
        'design_checks': list(getattr(session, 'design_checks', []) or []) if session else [],
        'findings': list(getattr(session, 'findings', []) or []) if session else [],
        'dependency_identity': (getattr(getattr(session, 'dependency_watch', None), 'record', None)
                                if session else None),
        'workflow': _workflow_record(session, completed, error),
        'survey': _survey_record(session, completed),
        'explicit_tasks': _explicit_record(session, completed),
        'parallel_batches': list(getattr(session, 'parallel_batches', []) or []) if session else [],
        'trace': {'calls': len(traces),
                  'transcripts_found': sum(1 for t in traces if t.get('tool_calls') is not None),
                  'calls_outside_project': sum(1 for t in traces if t.get('outside_project')),
                  'unserved_requests': sum(len(t.get('protocol_attempts') or []) for t in traces),
                  'injected_rules': sorted({r['source'] for t in traces for r in t.get('injected_rules') or []})},
        'decisions': list(getattr(session, 'decisions', []) or []) if session else [],
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
    (run_dir / 'findings.json').write_text(
        json.dumps(list(getattr(session, 'findings', []) or []) if session else [], indent=2), encoding='utf-8')
    return ProjectResult(completed, report, run_dir, diff, error)


def _calls_by_task(session):
    """Invoked model calls per task and role, from the delegation ledger:
    count, reported tokens and seconds. Measured, so a tier's saving is read
    from what ran, never from the label (direct tier, 2026-09-30)."""
    ledger = getattr(session.config, 'delegation_ledger', None)
    out = {}
    for event in getattr(ledger, 'events', None) or []:
        if not getattr(event, 'invoked', False):
            continue
        role = (event.role or '').split(':')[0]
        row = out.setdefault(event.task, {}).setdefault(role, dict(calls=0, input_tokens=0, output_tokens=0,
                                                                     seconds=0.0, unknown_usage=0))
        row['calls'] += 1
        if event.input_tokens is None and event.output_tokens is None:
            row['unknown_usage'] += 1
        row['input_tokens'] += event.input_tokens or 0
        row['output_tokens'] += event.output_tokens or 0
        row['seconds'] = round(row['seconds'] + (event.seconds or 0.0), 1)
    return out


def _explicit_record(session, completed):
    """The explicit-task section: which listed tasks ran, which text was
    refused, and that no one judged the goal. None on an orchestrated run."""
    record = getattr(session, 'explicit', None) if session is not None else None
    if record is None:
        return None
    ran = [r['task'] for r in record.get('ran', [])]
    closed = {s.task_id: getattr(s, 'outcome', 'closed') for s in getattr(session, 'history', [])}
    return dict(
        listed=record.get('listed', 0), ran=list(record.get('ran', [])),
        not_run=record.get('listed', 0) - len(ran),
        invalid=record.get('invalid'),
        tasks_closed_clean=[t for t in ran if closed.get(t) == 'closed'],
        tasks_unfinished=[t for t in ran if closed.get(t) not in (None, 'closed')],
        goal_judged=False,
        requirements_claimed=list(record.get('requirements_claimed', [])),
        requirements_unclaimed=list(record.get('requirements_unclaimed', [])),
        completed=completed,
        note='completed means every listed task closed clean with no open finding or failing '
             'check and every requirement a listed task claimed audited met; requirements no '
             'task claimed are unjudged; no orchestrator planned, acknowledged or judged the goal.',
    )


def _survey_record(session, completed):
    """The survey section: what a run that continues through failures
    collected, reported apart from acceptance (SessionConfig.survey). None
    on an ordinary run."""
    if session is None or getattr(session.config, "survey", None) is None:
        return None
    outcomes = list(getattr(session, "task_outcomes", []) or [])
    failures = []
    for outcome in outcomes:
        for fact in outcome.facts:
            if fact.kind in ("failed", "cap") or (fact.kind == "product" and fact.stage == "checks"):
                cause = (fact.detail.split(":", 1)[0] if fact.kind == "failed" else fact.kind)
                failures.append(dict(task=outcome.task_id, kind=fact.kind, cause=cause,
                                     detail=fact.detail[:200], recovered=bool(fact.recovered)))
    findings = list(getattr(session, "findings", []) or [])
    return dict(
        recovery_tasks=session.config.survey.recovery_tasks,
        recovery_used=session.survey.get("recovery_used", 0),
        failures=failures,
        unique_causes=sorted({f["cause"] for f in failures}),
        recovered=[f["task"] for f in failures if f["recovered"]],
        open=[f["task"] for f in failures if not f["recovered"]],
        findings=[dict(id=f["id"], kind=f.get("kind"), status=f["status"], task=f["task"]) for f in findings],
        repeats=list(session.survey.get("repeats", [])),
        hypotheses=list(session.survey.get("hypotheses", [])),
        completed=completed,
    )


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


def _workflow_record(session, completed, error):
    """The typed task and run outcomes and their parity with the legacy
    decisions (quadratus.outcome, phase 1). Observational: never fails a run."""
    if session is None or not hasattr(session, 'run_outcome'):
        return None
    try:
        from dataclasses import asdict

        from .outcome import parity
        try:
            open_ids = list(session._open_findings_for(None))
        except Exception:  # noqa: BLE001 -- recorded as unknown, never raised
            open_ids = ['unknown']
        history = [f"{s.task_id}:{getattr(s, 'outcome', 'closed')}" for s in session.history]
        return {'tasks': [t.to_dict() for t in session.task_outcomes],
                'calls_by_task': _calls_by_task(session),
                'run': asdict(session.run_outcome),
                'parity': parity(session.run_outcome, session.task_outcomes, open_findings=open_ids,
                                 legacy_completed=completed, legacy_error=error, history=history)}
    except Exception as exc:  # noqa: BLE001 -- observation never fails a run
        return {'error': f'{type(exc).__name__}: {exc}'}


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

