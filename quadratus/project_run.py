"""The project workflow shared by the terminal and the GUI."""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
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

log = logging.getLogger(__name__)


@dataclass
class ProjectResult:
    completed: bool
    report: str
    run_dir: Path
    diff: str
    error: str = ''


def _publish_in_flight(store, run_dir, in_flight: dict) -> None:
    """Preserved partial work: a record of the committed generation when the
    store still publishes, and the historical export either way."""
    try:
        store.publish({'in_flight': in_flight})
    except Exception:  # noqa: BLE001 -- a latched store keeps its last generation
        log.warning('could not publish the in-flight record', exc_info=True)
    (run_dir / 'in-flight.json').write_text(json.dumps(in_flight, indent=2), encoding='utf-8')


def _store_readable(store) -> bool:
    try:
        store.current()
        return not store.refused
    except Exception:  # noqa: BLE001 -- a corrupt manifest is reported, never read around
        return False


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
                decider_labels="unstated", repair_tests=(), resume_from=None):
    """Keep both successful and interrupted runs next to their source tree.

    ``resume_from`` is the run folder of an interrupted run of the same job
    (quadratus.checkpoint): the new run is its next episode, carrying its
    record, its lineage and what it spent, or it is refused before any call.

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

    repair_tests = _repair_tests(project.root, repair_tests, forbid)
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
                    decider_labels=decider_labels, repair_tests=repair_tests,
                    resume_from=resume_from)


def _resume_parent(state, resume_from):
    """The run folder to resume: inside this state folder's runs, never a
    path elsewhere (a checkpoint is the job's own record, not an input)."""
    from .checkpoint import CheckpointRefused
    runs = (state / 'runs').resolve()
    parent = Path(resume_from).expanduser()
    parent = (parent if parent.is_absolute() else runs / parent).resolve()
    if parent.parent != runs or not parent.is_dir():
        raise CheckpointRefused(f'{resume_from} is not a run of this project state ({runs})')
    return parent


def _repair_tests(root, paths, forbid=()):
    """The operator's authorized test repairs as exact project-relative
    paths, or ValueError before any model call. Each must be an existing
    test file (not a helper, fixture or configuration) that no operator
    exclusion protects; a glob, a folder or a path outside the project is
    refused, so the authorization cannot widen past the files it names."""
    from .integration import _test_family
    from .scope import _matches
    names = []
    for raw in paths or ():
        name = str(raw).strip().removeprefix('./')
        if not name:
            continue
        if any(c in name for c in '*?[') or name.endswith('/') or name.startswith('/') or '..' in name.split('/'):
            raise ValueError(f'Test repair must name one exact file inside the project: {raw!r}')
        target = Path(root) / name
        if target.is_symlink() or not target.is_file():
            raise ValueError(f'Test repair names no existing file: {name}')
        if _test_family(name) is None:
            raise ValueError(f'Test repair names a file that is not a test: {name}')
        if any(_matches(name, pattern) for pattern in forbid or ()):
            raise ValueError(f'Test repair names a path the operator forbade: {name}')
        names.append(name)
    return tuple(dict.fromkeys(names))


def _packet_floor(policy, scope):
    """Refuse before any model call when the mandatory part of a role packet
    (the operator's scope and forbidden paths, the family checklist, the
    gates) is already over the packet cap. A task's own scope only adds to
    it, so no task of this run could be dispatched (run
    20261010T135407Z-f3d33e9d: 264 forbidden paths, refused only after the
    planning and review calls). Reference notes are truncatable and are not
    part of the floor."""
    for role in ('lead', 'reviewer'):
        policy.role_packet(scope, role)


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
    # ``-p no:cacheprovider`` disables pytest's result cache (``.pytest_cache``,
    # ``--lf``/``--ff``) and selects nothing: series rule-7590b13 f3 ran its
    # suite twice per gate, once as the operator's ``check`` and once as the
    # scanned ``declared-python``, and the second had no report to attribute.
    rest = _without_cacheprovider(rest)
    return (head, *[a for a in rest if a not in _QUIET_FLAGS and not _report_only(a)])


def _without_cacheprovider(rest):
    out, skip = [], False
    for i, arg in enumerate(rest):
        if skip:
            skip = False
            continue
        if arg == '-p' and i + 1 < len(rest) and rest[i + 1] == 'no:cacheprovider':
            skip = True
            continue
        if arg == '-pno:cacheprovider':
            continue
        out.append(arg)
    return out


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
         decider=None, decider_labels="unstated", repair_tests=(), resume_from=None):
    parent = _resume_parent(state, resume_from) if resume_from is not None else None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run_dir = state / 'runs' / f'{stamp}-{uuid.uuid4().hex[:8]}'
    run_dir.mkdir(parents=True)
    from .run_activity import RunActivity
    activity = RunActivity(run_dir, progress)
    progress = activity
    activity.record('run-started', goal=str(goal)[:4000])
    stale_fixtures = _retire_stale_fixtures(project.root, run_dir, progress)
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
    # The resume decision needs the settings, limits and gate exactly as this
    # episode will run with them; it is made here, before any call.
    from .run_budget import effective_lead_turns as _lead_turns
    _turns, _source = _lead_turns(getattr(settings, 'lead_max_turns', None), run_limits)
    _settings = (dataclasses.replace(settings, lead_max_turns=_turns)
                 if _turns is not None and getattr(settings, 'lead_max_turns', None) is None else settings)
    _selected = _selected_limits(max_tasks, run_limits, survey, _turns, _source)
    _preview = (policy.resolve(default_scope.permitted_paths if default_scope else (), writing=allow_writes)
                if policy else None)
    from . import checkpoint
    from .session import _gate_identity
    held = dict(goal=goal, policy=(_preview or {}).get('hash'),
                limits=hashlib.sha256(json.dumps(_selected, sort_keys=True).encode()).hexdigest(),
                settings=checkpoint.settings_identity(_settings), gate=_gate_identity(gate),
                # The run configuration that decides dispatch: the same job
                # only under the same one (root review of aaa06a6).
                workflow=checkpoint.workflow_identity(
                    allow_writes=allow_writes, mode=mode, direct_tier=direct_tier,
                    decider=decider,
                    decider_labels=decider_labels, default_scope=default_scope,
                    capture_profile=capture_profile, readiness=list(readiness or ()),
                    security_verdict_json=security_verdict_json, repair_tests=list(repair_tests or ()),
                    explicit_tasks=list(tasks) if tasks is not None else None,
                    plan_gate=plan_gate is not None, ask_operator=ask_operator is not None))
    lineage = dict(root=run_dir.name, parent=None, episode=1)
    carried = None
    if parent is not None:
        from .deptree import DependencyGuard
        try:
            dependency = DependencyGuard(project.root).identity().digest
        except Exception as exc:  # noqa: BLE001 -- an unmeasurable tree refuses the resume
            dependency = f"unavailable: {type(exc).__name__}"
        body = checkpoint.load(parent)
        carried = checkpoint.validate(parent, body, checkpoint.identities(
            source=project.fingerprint(), dependency=dependency, **held))
        if run_limits is not None and (carried['calls'] >= run_limits.max_calls
                                       or carried['input_tokens'] + carried['output_tokens']
                                       >= run_limits.max_reported_tokens
                                       or carried['elapsed_seconds'] >= run_limits.wall_seconds):
            raise checkpoint.CheckpointRefused("the job's allowance is spent; nothing is left to resume with")
        if int(body['state'].get('slots_used') or 0) >= max_tasks and not body['state'].get('_final_audit'):
            # A checkpoint written during the cap's final challenge carries
            # the final audit, and resumes to finish that decision without
            # asking, auditing or executing again; any other is spent.
            raise checkpoint.CheckpointRefused("the job's task slots are spent; nothing is left to resume with")
        prior = body.get('lineage') or {}
        lineage = dict(root=prior.get('root') or parent.name, parent=parent.name,
                       episode=int(prior.get('episode') or 1) + 1)
        checkpoint.claim(parent, run_dir, lineage['episode'])
        if (parent / 'artifacts').is_dir():
            # The record's artifact references resolve in the new episode.
            shutil.copytree(parent / 'artifacts', run_dir / 'artifacts', dirs_exist_ok=True)
        if progress:
            progress(f"Resuming {parent.name} as episode {lineage['episode']} of {lineage['root']}; "
                     f"carried {carried['calls']} call(s), "
                     f"{carried['input_tokens'] + carried['output_tokens']:,} reported tokens")
    (run_dir / 'lineage.json').write_text(json.dumps(lineage, indent=2) + '\n', encoding='utf-8')
    # The run's durable job store (quadratus.jobstore): every budget
    # mutation, checkpoint and session effect is one committed generation
    # under one manifest; budget.json and checkpoint.json stay as exports.
    from .jobstore import JobStore
    job_store = JobStore(run_dir)
    budget = RunBudget(run_limits, path=run_dir / 'budget.json', carried=carried, store=job_store) if run_limits else None
    if isinstance(decider, str) or decider is None:
        from .decisions import decider_from_name
        decider = decider_from_name(decider, meter=meter, budget=budget)
    elif budget is not None and getattr(decider, "budget", None) is None and hasattr(decider, "budget"):
        # A decider object handed in directly (tests, embedding callers) is
        # bound to this run's budget too: no billed call escapes the limits.
        decider.budget = budget
    # A per-call ceiling with no turn cap stops a lead only after the
    # oversized call returns (both Stage B series on b1ff751); the cap
    # derived here acts before it, and the operator's own value wins.
    from .run_budget import effective_lead_turns
    lead_turns, lead_source = effective_lead_turns(getattr(settings, 'lead_max_turns', None), run_limits)
    if lead_turns is not None and getattr(settings, 'lead_max_turns', None) is None:
        settings = dataclasses.replace(settings, lead_max_turns=lead_turns)
        if progress:
            progress(f'Lead turn cap: {lead_turns} rounds, derived from max_tokens_per_call '
                     f'{run_limits.max_tokens_per_call:,}')
    # The allowance this run was given, saved before any model call so an
    # interrupted run still says what it was allowed (Codex GUI plan, #35),
    # with the lead turn cap the session is about to be built with.
    selected = _selected_limits(max_tasks, run_limits, survey, lead_turns, lead_source)
    (run_dir / 'run-limits.json').write_text(json.dumps(selected, indent=2) + '\n', encoding='utf-8')
    if progress:
        progress('Run limits: ' + selected['described'])
    config = SessionConfig(
        project=project.root, project_excludes=tuple(project.exclude),
        allow_writes=allow_writes, mode=mode, integration_gate=gate,
        lead_max_turns=getattr(settings, 'lead_max_turns', None),
        codebase_map=code_map, ask_operator=ask_operator, plan_gate=plan_gate,
        progress=progress, activity=activity.record, delegation_ledger=delegation,
        call_trace=_live_call_trace,
        default_scope=default_scope, repository_policy=policy,
        security_verdict_json=security_verdict_json,
        capture_profile=capture_profile,
        readiness_probes=tuple(readiness or ()),
        survey=survey,
        direct_tier=direct_tier,
        decider=decider,
        decider_labels=decider_labels,
        repair_tests=tuple(repair_tests),
        job_store=job_store,
    )

    def on_quiescent(session):
        # The models the job has resolved so far, anywhere in it, go into
        # this point's state before it is captured (root's checkpoint
        # control on bcd2b52: the field was copied once before the run and
        # the quiescent record carried an empty mapping).
        _record_models(session, fleet)
        # One snapshot, published as the budget record and into the
        # checkpoint's own record of the same generation, so the two agree
        # to the second (root review of aaa06a6).
        snapshot = budget.persist() if budget else {}
        watch = getattr(session, 'dependency_watch', None)
        baseline = getattr(watch, 'baseline', None)
        state_now = checkpoint.capture_state(session)
        uncarried = list(state_now.get('uncarried') or []) if 'uncarried' in state_now else []
        if snapshot.get('in_flight'):
            uncarried.append('a call in flight')
        settled_ops = {}
        for op in getattr(session, 'operations', []) or []:
            if op.get('status') == 'settled':
                owner = str(op.get('owner_id') or '')
                settled_ops[owner] = settled_ops.get(owner, 0) + 1
        checkpoint.write(run_dir, dict(
            version=checkpoint.VERSION, lineage=lineage, sealed=False, resumable=not uncarried,
            uncarried=uncarried, consumed=checkpoint.consumed(snapshot) if snapshot else None,
            identities=checkpoint.identities(source=session._source_fingerprint(),
                                             dependency=getattr(baseline, 'digest', None), **held),
            # What the record has consumed at this quiescent point: every
            # operation settled past it on restart is unconsumed work.
            watermark=dict(budget_settled=len(((snapshot or {}).get('operations') or {}).get('settled') or []),
                           session_settled=settled_ops),
            # The approval authority this point was recorded under.
            authority=session._publish_authority() if hasattr(session, '_publish_authority') else None,
            state={} if uncarried else state_now,
            written=datetime.now(timezone.utc).isoformat()), store=job_store,
            budget_snapshot=snapshot or None)
    config.on_quiescent = on_quiescent
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
    if parent is not None and hasattr(fleet, 'freeze'):
        # Before any seat or availability decision: the resumed episode
        # addresses the models the job already resolved; the current
        # catalogue is recorded beside them, never followed mid-job.
        frozen = ((body.get('state') or {}).get('model_resolution') or {}).get('resolutions')
        if frozen:
            fleet.freeze(frozen)
    try:
        fleet.progress = progress  # one live line per call as it ends
        # The checks are the commands a granted editing call may run
        # unapproved and the denials that count as a capability failure.
        from .integration import model_facing
        # What a seat is told to run: the harness's report declaration is
        # its own and never reaches a model's argv (series rule-58a4625 f3).
        fleet.check_commands = tuple(shlex.join(model_facing(g['argv'])) for g in plan if g.get('argv'))
    except Exception:  # noqa: BLE001 -- a fake fleet may refuse attributes
        pass
    in_flight = {}
    interrupted = False
    try:
        if preview and preview['blocked']:
            raise ValueError('; '.join(preview['blocked']))
        if policy is not None:
            _packet_floor(policy, default_scope)
        stale = []
        try:
            stale = list(fleet.stale_clients())
        except AttributeError:  # a fake fleet without the preflight (tests)
            stale = []
        if stale:
            # Before any call: a client older than the one this engine
            # qualified runs nothing (2026-10-21: codex 0.154 ended
            # two runs on a capacity 429 it could neither report nor wait
            # out). The versions are the error; nothing is substituted.
            from .cli_providers import StaleClient
            raise StaleClient('; '.join(stale))
        if progress:
            progress(f'Project: {project.root}; edits {"enabled" if allow_writes else "disabled"}')
        session = session_factory(
            goal, store, fleet=fleet, config=config,
            invariants=['Project source is available in the working directory. '
                        'Use actual files as evidence. Do not commit or publish changes.'],
        )
        if parent is not None:
            checkpoint.restore_state(session, body['state'])
            session._interrupted_admissions = list((carried or {}).get('interrupted_admissions') or [])
            _carry_recoveries(session, parent)
        _record_models(session, fleet)
        session.run(max_tasks=max_tasks, **({'tasks': tasks} if tasks is not None else {}))
    except (Exception, KeyboardInterrupt) as exc:  # persist partial work and its cause
        interrupted = isinstance(exc, KeyboardInterrupt)
        if interrupted and budget is not None:
            try:
                # The time up to the interruption is the job's spend too.
                budget.persist()
            except Exception:  # noqa: BLE001 -- the record keeps its last write
                pass
        error = f'{type(exc).__name__}: {exc}'
        # A stopped editing call already inspected the tree and kept whatever
        # was there. Carrying that forward is what makes the handoff resumable
        # rather than merely honest about having failed.
        partial = getattr(exc, 'partial', None)
        in_flight = dict(getattr(session, 'in_flight', {}) or partial or {})
        if in_flight:
            _publish_in_flight(job_store, run_dir, in_flight)
    finally:
        fleet.close()
    if not error and session is not None and getattr(session, 'stop_reason', ''):
        # A stop the session chose rather than one an exception forced (the
        # turn-limit breaker, unverified design evidence). Reported as the
        # error so the result says why; a breaker stop also lists the capped
        # tasks' preserved edits as in-flight work.
        error = session.stop_reason
        try:
            session._record_stop_rule(error)
        except Exception:  # noqa: BLE001 -- the improvement record never fails a run
            pass
        records = list((getattr(session, 'turn_limited_records', {}) or {}).values())
        failed = list((getattr(session, 'failed_records', {}) or {}).values())
        if error.startswith(('TurnLimitBreaker', 'TaskFailureBreaker')) and (records or failed):
            changed = sorted({name for r in records + failed for name in r.get('changed') or []})
            in_flight = dict(
                note='Stopped by the unfinished-task breaker; every capped or failed task\'s edits are preserved.',
                changed=changed, changed_lines=sum(r.get('changed_lines') or 0 for r in records + failed),
                turn_limited=records, failed=failed)
            _publish_in_flight(job_store, run_dir, in_flight)
    # What each call did inside its own session, from the vendors' transcripts.
    # Collected after the run so a slow copy never delays a model call.
    traces = []
    try:
        from .trace import build_traces
        traces = build_traces(run_dir, run_dir / 'invocations.jsonl', project.root)
    except Exception:  # noqa: BLE001 -- tracing never fails a run
        traces = []
    _observe_transcript_children(traces, delegation)
    diff = project.diff(before)
    completed = bool(session and session.completed and not error)
    # The terminal state is committed before it is announced: the record of
    # what the run ended as and the acceptance basis it ended on, in the
    # same generation as the sealed checkpoint. A run whose terminal state
    # cannot be committed has not finished, whatever memory says.
    explicit_record = _explicit_record(session, completed)
    terminal = dict(completed=completed, error=error, interrupted=interrupted,
                    acceptance=(session._acceptance_record() if session and hasattr(session, '_acceptance_record')
                                else None), written=datetime.now(timezone.utc).isoformat(),
                    # What a reader needs when no export exists: the goal,
                    # whether the goal was judged or only listed tasks ran,
                    # and each task's closing state.
                    goal=goal, explicit_tasks=(dict(listed=explicit_record.get('listed'),
                                                    goal_judged=explicit_record.get('goal_judged'))
                                               if explicit_record else None),
                    tasks=[dict(task_id=s.task_id, outcome=getattr(s, 'outcome', 'closed'))
                           for s in (getattr(session, 'history', None) or [])] if session else [],
                    budget_stop_reason=(budget.snapshot().get('stop_reason') if budget is not None else None))
    if interrupted:
        if session is not None:
            # The interrupted run's checkpoint is its last quiescent point;
            # occurrences and attempts made after it are kept here, so a
            # resumed episode carries them (no lost attempt, no reset retry).
            terminal['recoveries'] = checkpoint._encode(list(getattr(session, 'recoveries', []) or []))
        try:
            job_store.publish({'terminal': terminal})
            terminal_committed = True
        except Exception:  # noqa: BLE001 -- latched: an interrupted run's terminal is best effort
            terminal_committed = False
    else:
        _record_models(session, fleet)
        terminal_committed = checkpoint.seal(run_dir, error or ('completed' if completed else 'incomplete'),
                                             state_patch=_terminal_state_patch(session) if session else None,
                                             store=job_store, extra={'terminal': terminal})
    if not terminal_committed and not interrupted:
        completed = False
        error = error or f"JobStoreUnavailable: the terminal state could not be committed ({job_store.refused})"
        terminal = dict(terminal, completed=False, error=error)
        if session is not None:
            # The typed record says the same as the legacy one: a run whose
            # ending was never committed did not finish, and the stop names
            # the store (integrity: the record itself is what failed).
            session.completed = False
            outcome = getattr(session, 'run_outcome', None)
            if outcome is not None and not any(f.active and f.detail.startswith('JobStoreUnavailable')
                                               for f in outcome.facts):
                outcome.note('integrity', error[:400], legacy='JobStoreUnavailable', full=error)
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
    for line in _unfulfilled_requirements(session, completed):
        lines += [line, '']
    for repair in (getattr(session, 'test_repairs', None) or []) if session else []:
        seen = repair.get('checks') or []
        lines += [f"Authorized test repair: {repair['path']} (run-start bytes kept as artifact "
                  f"{repair['original_artifact']}). Original cases "
                  + ('kept and executed in the last check' if seen and seen[-1].get('passed') else
                     'NOT shown kept and executed') + '. Its assertions are not independently checked; '
                  'retained case names do not prove they were not weakened. Review the repaired file '
                  'against the kept original before relying on it.', '']
    for handoff in (getattr(session, 'verification_handoffs', None) or []) if session else []:
        lines += [f"Verification task {handoff['task']} was not dispatched: the harness had already verified "
                  f"source {handoff['source'][:12]} with its own runs. {', '.join(handoff['covers']) or 'Nothing'} "
                  "covered by that evidence, judged only by the final requirement audit.", '']
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
    lines += [delegation.render_report(), '']
    recovery_section = _improvement_section(session)
    if recovery_section:
        lines += [recovery_section]
    lines += ['## Task ledger', '', ledger]
    report = '\n'.join(lines)
    (run_dir / 'changes.diff').write_text(diff, encoding='utf-8')
    (run_dir / 'ledger.md').write_text(ledger, encoding='utf-8')
    (run_dir / 'report.md').write_text(report, encoding='utf-8')
    _record_models(session, fleet)
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
        # Ordinary calls refused to keep the finish, and what each would have
        # done (run_budget.FinishReserved); budget.json holds the admissions.
        'finish_refusals': list(getattr(session, 'finish_refusals', []) or []) if session else [],
        # Tasks whose first editing call was refused before any write: NOT RUN.
        'not_started': list(getattr(session, 'not_started', []) or []) if session else [],
        # Same-seat judgment completions decided this episode, and mandatory
        # judgments left without a valid verdict (session._complete_judgment).
        'judgment_completions': list(getattr(session, 'judgment_completions', []) or []) if session else [],
        'judgment_gaps': list(getattr(session, 'judgment_gaps', []) or []) if session else [],
        # Recovery occurrences and the proposals aggregated from them
        # (quadratus.recovery): deterministic, written by production paths,
        # never adopted automatically. Costs come from the invocation ledger;
        # unknown stays null.
        'recoveries': list(getattr(session, 'recoveries', []) or []) if session else [],
        'improvement': _improvement_record(session),
        # The last obligations view the planner or the audit read (quadratus.obligations).
        'obligations': dict(getattr(session, 'obligation_view', {}) or {}) if session else {},
        # Each family's resolution for this job and its provenance (the
        # codex catalogue as read, the grok listing), quadratus.latest.
        'models': dict(getattr(session, 'model_resolution', {}) or {}) if session else {},
        # Typed delivery records (quadratus.delivery): review findings with
        # per-id dispositions, evidence obligations with execution and
        # sufficiency apart, the plan admissions decided before each wave,
        # and the session's own effects (checks, captures, probes).
        'review_findings': list(getattr(session, 'review_findings', []) or []) if session else [],
        'evidence_obligations': list(getattr(session, 'evidence_obligations', []) or []) if session else [],
        'plan_admissions': list(getattr(session, 'plan_admissions', []) or []) if session else [],
        'open_findings': list(getattr(session, 'open_findings', []) or []) if session else [],
        'proof_binding_problems': list(getattr(session, 'proof_binding_problems', []) or []) if session else [],
        # The planner's scenario corrections and the EVIDENCE findings the
        # harness answered before any editor call (workflow repair).
        'scenario_corrections': list(getattr(session, 'scenario_corrections', []) or []) if session else [],
        'evidence_routes': list(getattr(session, 'evidence_routes', []) or []) if session else [],
        'close_decisions': list(getattr(session, 'close_decisions', []) or []) if session else [],
        # RESOLVES and CONTINUES settlements of review findings: who was asked
        # and what each recheck closed (session._settle_review_findings).
        'review_resolutions': list(getattr(session, 'review_resolutions', []) or []) if session else [],
        # Acceptance rows a parallel child could not execute, declared by the
        # harness and rechecked by the same reviewer after the merge gate.
        'deferred_evidence': list(getattr(session, 'deferred_evidence', []) or []) if session else [],
        # Characters each planning, plan-gate and lead prompt carried by
        # block, with the completed-work block as the index sent and as the
        # preview render it replaced (session._measure_context). Never tokens.
        'prompt_measurements': list(getattr(session, 'prompt_measurements', []) or []) if session else [],
        # Each unmet requirement's route at the finish (session._evidence_route).
        'finish_routing': list(getattr(session, 'finish_routing', []) or []) if session else [],
        # Slot accounting: each charge at dispatch (serial task or parallel wave),
        # each planner reply sent back, DONE sent back or task not started, with
        # the slots charged as it stood after the event (session._slot_event).
        **_slot_fields(session),
        'terminal_route': dict(getattr(session, '_terminal_route', None) or {}) or None if session else None,
        'operations': list(getattr(session, 'operations', []) or []) if session else [],
        'job_store': dict(generation=(job_store.current().number if job_store.current() else None),
                          refused=job_store.refused, terminal_committed=terminal_committed)
        if _store_readable(job_store) else dict(generation=None, refused=job_store.refused or 'unreadable',
                                                terminal_committed=terminal_committed),
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
        **_budget_fields(budget),
        'selected_limits': selected,
        'unnamed_test_files': list(getattr(session, 'unnamed_test_files', []) or []) if session else [],
        'original_test_runs': list(getattr(session, 'original_test_runs', []) or []) if session else [],
        # Harness runs of the task gate seats were shown; never task checks.
        'review_checks': list(getattr(session, 'review_checks', []) or []) if session else [],
        'test_repairs': list(getattr(session, 'test_repairs', []) or []) if session else [],
        'skip_baselines': list(getattr(session, 'skip_baselines', []) or []) if session else [],
        'unexecuted_acceptance': list(getattr(session, 'unexecuted_acceptance', []) or []) if session else [],
        # Readings an editing call could not make, routed to its review
        # (inspection), with the attestation that closed each, if any.
        'inspection_gaps': list(getattr(session, 'inspection_gaps', []) or []) if session else [],
        'generated_test_repairs': list(getattr(session, 'generated_test_repairs', []) or []) if session else [],
        'rejected_responses': list(getattr(session, 'rejected_responses', []) or []) if session else [],
        # Harness runs of a reviewer's scratch test in its copy; never a MET.
        'scratch_proofs': list(getattr(session, 'scratch_proofs', []) or []) if session else [],
        'evidence_requests': list(getattr(session, 'evidence_requests', []) or []) if session else [],
        'review_notes': list(getattr(session, 'review_notes', []) or []) if session else [],
        'stale_capture_fixtures': stale_fixtures,
        'lineage': lineage,
        'resumable': interrupted,
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


#: Tool names that start a vendor-native child agent inside one call.
_FANOUT_TOOLS = {'agent', 'task', 'spawn_subagent'}


def _observe_transcript_children(traces, delegation):
    """Record a native child a call's own transcript names but its output hid.

    Claude's ``--output-format json`` envelope carries no tool calls, so a
    child an unrestricted seat spawns is invisible to the provider. Batch 2
    recovery-v2 on 5d9f5ff: the orchestrator's second call ran an ``Agent``
    child to execute the checks, the trace recorded ``Agent: success``, and
    the delegation record still said zero native children. Each such call
    becomes an unidentified child with unknown usage: its spend is already
    inside the parent call's reported total, so nothing is added and no
    figure is guessed. Observational only; it never stops a run.
    """
    from .delegation import NativeChild
    for record in traces or ():
        for n, call in enumerate(record.get('tool_calls') or ()):
            name = str((call or {}).get('name') or '')
            if name.lower() not in _FANOUT_TOOLS:
                continue
            outcome = str(call.get('outcome') or 'unknown')
            where = (f"{record.get('task')} {record.get('role')} {record.get('model')} "
                     f"(session {record.get('session_id')})")
            if outcome.startswith('denied'):
                # The control holding: a refused spawn ran nothing (Codex
                # review of 6a338a1), so it is noted, never filed as a child.
                delegation.note_blind_spot(f'{name} call refused by permissions in {where}; no child ran')
                continue
            if outcome != 'success':
                # Errored or unresolved: an attempt whose execution is
                # unknown, never filed as a child the record would say ran
                # (Codex review of 7515f28).
                delegation.note_blind_spot(f'{name} call with outcome {outcome} in {where}: attempted; '
                                           'whether a child ran is unknown')
                continue
            # Keyed by the vendor's call id within the parent session, else
            # by the call's position in that session's transcript (stable
            # across rereads of one growing transcript), never by the parent
            # invocation: two invocations sharing a session read the same
            # calls (Codex review of 6a338a1).
            key = call.get('id') or f"#{n}"
            delegation.observe_native(NativeChild(
                session_id=f"unidentified:transcript:{record.get('session_id') or '?'}:{key}",
                parent_session_id=record.get('session_id'),
                tool_name=name,
                detail=(f"named in the saved transcript of {where} with a successful result; its usage "
                        "is inside the parent call's reported total and is not added"),
            ))


def _retire_stale_fixtures(root, run_dir, progress=None):
    """Retire capture-only samples left by an earlier run, bound to this
    project's own state directory.

    Task ids restart at t1 on every run, and an existing nonempty file under
    ``.quadratus/capture-fixtures/<task id>/`` counts as supplied, so a later
    run's t1 could upload bytes dictated for a different task (Codex review
    comment 4226680384 on #53). ``.quadratus`` is opened relative to the
    project root without following links and held as a directory handle;
    ``capture-fixtures`` is examined and renamed through that handle to
    ``capture-fixtures.retired-<run id>`` in the same directory. A rename
    within one directory is atomic, never crosses devices and never follows
    a link: the first draft checked pathnames and then moved by pathname,
    so a ``.quadratus`` replaced by a link in between moved another
    project's fixtures (Codex review of 391f3c8). The retired name is not a
    fixture path, so nothing in it can be uploaded again; the bytes stay
    readable there. A linked ``.quadratus`` or ``capture-fixtures`` is left
    alone and named. Only an absent folder means there is nothing to do:
    any other failure to read or rename raises before any model call
    (Codex review of 6a338a1: an unreadable state directory let the session
    start with the stale samples still in place). Returns None, or the
    retired folder's project-relative ``path``, whether ``.quadratus`` still
    names the directory it was retired in (``resolves``, read through the
    project pathname, so a project folder replaced as a whole is false), and that
    directory's ``state_directory`` identity: after a parent swap the bytes
    are safe but the path leads elsewhere, and the record says so.
    """
    import errno
    import stat

    def say(text):
        if progress:
            progress(text)

    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    def refuse(exc):
        return ValueError('Capture-only samples from an earlier run could not be retired '
                          f'({type(exc).__name__}: {exc}); the run stops before any model call '
                          'rather than start with them in place.')

    try:
        root_fd = os.open(str(root), flags)
    except OSError as exc:
        raise refuse(exc) from exc
    try:
        try:
            state_fd = os.open('.quadratus', flags | nofollow, dir_fd=root_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                say('Stale capture fixtures not retired: .quadratus is a symlink or not a folder')
                return None
            raise refuse(exc) from exc
        try:
            try:
                info = os.stat('capture-fixtures', dir_fd=state_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(info.st_mode):
                say('Stale capture fixtures not retired: .quadratus/capture-fixtures is a symlink')
                return None
            if not stat.S_ISDIR(info.st_mode):
                return None
            folder_fd = os.open('capture-fixtures', flags | nofollow, dir_fd=state_fd)
            try:
                if not os.listdir(folder_fd):
                    return None
            finally:
                os.close(folder_fd)
            retired = f'capture-fixtures.retired-{Path(run_dir).name}'
            os.rename('capture-fixtures', retired, src_dir_fd=state_fd, dst_dir_fd=state_fd)
            held = os.fstat(state_fd)
            moved = os.stat(retired, dir_fd=state_fd, follow_symlinks=False)
            # The locator is true only while the project's pathname, read
            # fresh from the operator's folder, still leads to the retired
            # bytes. Comparing .quadratus relative to the held root was not
            # enough (Codex review of 6a338a1, then of 7515f28): a project
            # folder replaced as a whole left the held root agreeing with
            # itself while <project>/<path> no longer existed.
            try:
                now = os.stat(os.path.join(str(root), '.quadratus'), follow_symlinks=False)
                there = os.stat(os.path.join(str(root), '.quadratus', retired), follow_symlinks=False)
                resolves = (stat.S_ISDIR(now.st_mode) and stat.S_ISDIR(there.st_mode)
                            and (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino)
                            and (there.st_dev, there.st_ino) == (moved.st_dev, moved.st_ino))
            except OSError:
                resolves = False
        except OSError as exc:
            raise refuse(exc) from exc
        finally:
            os.close(state_fd)
    finally:
        os.close(root_fd)
    where = f'.quadratus/{retired}'
    if resolves:
        say(f'Capture-only samples from an earlier run retired to {where}')
    else:
        say(f'Capture-only samples from an earlier run retired as {retired} inside the state directory '
            f'that was at .quadratus (device {held.st_dev}, inode {held.st_ino}); .quadratus no longer '
            'names that directory, so the path does not lead to them')
    return dict(path=where, resolves=resolves, state_directory=dict(device=held.st_dev, inode=held.st_ino))


def _selected_limits(max_tasks, run_limits, survey, lead_turns=None, lead_source=None):
    from .run_budget import SUMMARY_CALL_RESERVE_TOKENS, describe_limits
    reserve = run_limits.reserve_tokens_per_call if run_limits is not None else 0
    return {
        'max_tasks': max_tasks,
        'run_limits': dataclasses.asdict(run_limits) if run_limits is not None else None,
        'summary_call_reserve_tokens': min(reserve, SUMMARY_CALL_RESERVE_TOKENS) if reserve else None,
        'lead_max_turns': lead_turns,
        'lead_max_turns_source': lead_source,
        'survey_recovery_tasks': survey.recovery_tasks if survey is not None else None,
        'described': describe_limits(run_limits, survey, max_tasks, lead_turns, lead_source),
    }


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


def _slot_fields(session) -> dict:
    """The slot accounting a run record carries: the ledger of charges and
    uncharged events (session._slot_event), the slots the recorded count
    holds that no charge backs (non-zero only for a job resumed from a
    checkpoint written under the older rule of a slot per loop turn;
    reported, never acted on), and each one-shot recheck of a capped,
    gated, in-scope revision (session._capped_revision_recheck)."""
    if session is None:
        return dict(slot_ledger=[], slots_unaccounted=None, capped_handoffs=[], task_admissions=[],
                    finish_renewals=[])
    ledger = list(getattr(session, 'slot_ledger', []) or [])
    charged = sum(len(e.get('tasks') or []) for e in ledger if e.get('event') == 'charged')
    return dict(slot_ledger=ledger,
                slots_unaccounted=max(0, int(getattr(session, '_slots_used', 0) or 0) - charged),
                capped_handoffs=list(getattr(session, 'capped_handoffs', []) or []),
                # Each task or wave admitted to a slot before its first effect,
                # as committed to the job store (session._admit_dispatch), and
                # each renewal of the final-audit hold before a post-audit
                # writer (session._renew_audit_hold).
                task_admissions=list(getattr(session, 'task_admissions', []) or []),
                finish_renewals=list(getattr(session, 'finish_renewals', []) or []))


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
        # Each CONTINUES or RESOLVES line's recovery decision, as the dispatch
        # charged it (completion workflow, 2026-10-30).
        charges=list(session.survey.get("charges", [])),
        completed=completed,
    )


def _budget_fields(budget) -> dict:
    """``budget`` (the terminal snapshot) and ``budget_note``, derived from
    that same snapshot: token capacity only, against the ordinary and the
    summary-call reservations; never an admission prediction (R20 on c7e9449: 141,947 reported tokens remained under a 200,000
    ordinary-call reserve, the run stopped FindingsOpen, and nothing in the
    record said the allowance had run that low). An observation, never the
    run's stop, outcome or a failure fact."""
    if not budget:
        return {'budget': None, 'budget_note': None}
    snapshot = budget.snapshot()
    limits = snapshot.get('limits') or {}
    reserve = int(limits.get('reserve_tokens_per_call') or 0)
    total = limits.get('max_reported_tokens')
    used = snapshot.get('reported_tokens')
    if not reserve or total is None or used is None:
        return {'budget': snapshot, 'budget_note': None}
    remaining = int(total) - int(used)
    if remaining >= reserve:
        return {'budget': snapshot, 'budget_note': None}
    from .run_budget import SUMMARY_CALL_RESERVE_TOKENS
    # RunBudget.reserve's own shaping: the smaller of the two, never more.
    shaped = min(reserve, SUMMARY_CALL_RESERVE_TOKENS)
    unknown = int(snapshot.get('unknown_usage_attempts') or 0)
    covers = 'covers' if remaining >= shaped else 'does not cover'
    text = (f'At the end of the run {remaining:,} reported tokens remained, below the ordinary-call token '
            f'reservation of {reserve:,}. The token remainder {covers} the {shaped:,}-token reservation for a '
            'summary-only call whose CLI enforces one turn. This is a token-capacity observation, not the '
            'run\'s stop or an admission decision; other limits and workflow conditions are not assessed.'
            + (f' {unknown} attempt(s) reported no usage, so the true remainder may be lower.' if unknown else ''))
    return {'budget': snapshot, 'budget_note': {
        'remaining_reported_tokens': remaining, 'ordinary_call_reserve': reserve,
        'summary_call_reserve': shaped, 'stop_reason': snapshot.get('stop_reason') or '',
        'unknown_usage_attempts': unknown, 'text': text}}


def _unfulfilled_requirements(session, completed) -> list:
    """One report line naming the listed requirements no finished task
    covers when the run did not complete (workflow repair, 2026-10-24: the
    ticket run stopped on open findings with R23, the README, listed and
    reviewed but never scheduled; the ledger held that fact and the report
    did not say it). Read from the ledger as it stands; nothing is judged."""
    if completed or session is None:
        return []
    ledger = session.memory.ledger
    status = ledger.requirement_status
    left = [rid for rid in ledger.requirements if not str(status.get(rid, '')).startswith(('covered', 'met'))]
    if not left:
        return []
    return ['Requirements no finished task covers when the run stopped: '
            + '; '.join(f"{rid}: {str(ledger.requirements[rid])[:120]}" for rid in left)]


def _terminal_state_patch(session) -> dict:
    """Terminal facts the last quiescent checkpoint could not hold: the models
    the job resolved after it (root's control on bcd2b52) and the unfinished
    work as the session held it when the run ended (recovery handoff repair,
    2026-10-25: a parallel child whose call failed left its kept artifact and
    baselines in memory only, so the sealed record named a finding and no
    handoff). Carried keys only; a sealed run is never resumed, so this is
    the record of what was owed, not a resume point. Every present key is
    copied as it is now, an empty value included (root's provisional review:
    a continuation in the last slot clears the partial debt after the last
    quiescent point, and an omitted empty set would leave the sealed state
    saying the debt was still owed); the task lists travel with the records
    so the handoff and its lists agree."""
    patch = dict(model_resolution=dict(getattr(session, 'model_resolution', {}) or {}))
    # The whole carried state as the session ends (root's control on
    # 6c1b922: the sealed state of a last-slot batch held the pre-batch
    # review_findings=[] and unresolved_reviews=[] beside the end's capped
    # lists, so the terminal record said nothing was owed to any reviewer).
    # Each key is encoded on its own; one that cannot be is named under
    # terminal_patch_omitted, never a silent drop of the whole patch.
    from . import checkpoint
    omitted = []
    try:
        snapshot = checkpoint.carried_snapshot(session)
    except Exception as exc:  # noqa: BLE001 -- the record says what it could not read
        snapshot = {}
        omitted.append(f"snapshot: {type(exc).__name__}: {str(exc)[:160]}")
    for name, value in snapshot.items():
        try:
            patch[name] = checkpoint._encode(value)
        except TypeError as exc:
            omitted.append(f"{name}: {str(exc)[:160]}")
    if omitted:
        patch['terminal_patch_omitted'] = omitted
    return patch


def _record_models(session, fleet) -> None:
    """Copy the Fleet's resolution record onto the session (result.json
    ``models``, the checkpoint). Never raises: a fake fleet has none."""
    try:
        record = fleet.resolution_record()
    except Exception:  # noqa: BLE001 -- a fake fleet without resolutions (tests)
        return
    if session is not None and isinstance(record, dict):
        session.model_resolution = record


def _carry_recoveries(session, parent) -> None:
    """Recovery occurrences the parent episode recorded after its last
    quiescent checkpoint (its committed terminal record holds them): each id
    once, an occurrence's longer attempt list kept over the checkpoint's
    shorter one, nothing invented. The registry is the checkpoint's; only
    the evidence of what was tried, and so what may not be tried again,
    comes from the terminal record."""
    from . import checkpoint, jobstore
    try:
        terminal = jobstore.read_terminal(parent) or {}
        later = checkpoint._decode(terminal.get('recoveries') or [])
    except Exception:  # noqa: BLE001 -- an unreadable record carries nothing extra
        return
    known = {r.get('id'): i for i, r in enumerate(session.recoveries)}
    for record in later if isinstance(later, list) else []:
        if not isinstance(record, dict) or not record.get('id'):
            continue
        rid = record['id']
        if rid not in known:
            session.recoveries.append(dict(record, carried_from=parent.name))
            known[rid] = len(session.recoveries) - 1
            continue
        mine = session.recoveries[known[rid]]
        theirs = list(record.get('attempts') or [])
        if len(theirs) > len(mine.get('attempts') or []) and theirs[:len(mine.get('attempts') or [])] == list(
                mine.get('attempts') or []):
            session.recoveries[known[rid]] = dict(record, carried_from=parent.name)


def _live_call_trace(event):
    """One finished call's tool record, read from the vendor's own session
    store while the run is live (read-only, nothing copied): the full trace
    dict, which the session passes through quadratus.recovery's allowlist
    before anything reaches a prompt or a record. None when the call has no
    session id or its transcript is not in this environment."""
    from . import trace as _trace
    vendor = _trace.vendor_of(getattr(event, 'requested_model', None))
    sid = getattr(event, 'session_id', None)
    if not vendor or not sid:
        return None
    source = _trace.locate(vendor, sid, _trace.session_roots())
    if source is None:
        return None
    record = _trace.trace_call(vendor, source, None, origin=source)
    # How much of the transcript could be read, measured here: the parsers
    # skip a line they cannot read, so an empty tool list alone cannot tell
    # "no tools used" from "nothing readable" (root's review of a7f9c6c, L3).
    record['integrity'] = _transcript_integrity(Path(source))
    # Progress only from a native session folder's own event log (grok):
    # a claude or codex transcript names tool use inside its rows, which
    # these label counts would misread, so their records stay as they were.
    if Path(source).is_dir():
        record['progress'] = _transcript_progress(Path(source))
    return record


_PROGRESS_TIME_KEYS = ('ts', 'timestamp', 'time', 'created_at')
#: Epoch seconds a native event may carry: 2001 to 2100. Milliseconds are
#: the same range times 1000; anything else is unreadable timing.
_EPOCH_RANGE = (1.0e9, 4.1e9)


def _progress_time(row):
    """The row's event time in epoch seconds, ``None`` when it carries none,
    or ``False`` when it carries one that cannot be read for certain: a
    non-finite or out-of-range number, a naive or non-UTC-convertible
    string, or anything else (root's review of 56ab825, item 2). Never
    raises."""
    import math
    for key in _PROGRESS_TIME_KEYS:
        if key not in row:
            continue
        value = row.get(key)
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            # Range-checked as an integer first: a JSON integer such as
            # 10**400 cannot become a float (root's review of e8f3a40).
            for scale in (1, 1000):
                if _EPOCH_RANGE[0] * scale <= value <= _EPOCH_RANGE[1] * scale:
                    return value / scale
            return False
        if isinstance(value, float):
            if not math.isfinite(value):
                return False
            for scale in (1.0, 1000.0):
                seconds = value / scale
                if _EPOCH_RANGE[0] <= seconds <= _EPOCH_RANGE[1]:
                    return seconds
            return False
        if isinstance(value, str) and 0 < len(value) <= 40:
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    return False
                seconds = parsed.timestamp()
            except (ValueError, OverflowError, OSError):
                return False
            return seconds if _EPOCH_RANGE[0] <= seconds <= _EPOCH_RANGE[1] else False
        return False
    return None


def _transcript_progress(source: Path) -> dict:
    """What one call was doing, from a native session folder's own event
    log, as counts: events by allowlisted label (``recovery.GROK_PHASES`` and
    ``GROK_EVENT_TYPES``; any other phase or type is counted as
    ``other_phase``/``other_type`` and its text dropped), tool and reasoning
    counts, the last known phase, first and last event time, span and the
    longest silence. ``activity`` is the plain reading of the counts. A row
    that is not a JSON object makes the record ``partial``; a time that
    cannot be read for certain makes ``timing`` partial and is left out,
    never guessed. Nothing of the content is read, and nothing raises."""
    from .recovery import GROK_EVENT_TYPES, GROK_PHASES, GROK_TOOL_TYPES
    path = source / 'events.jsonl' if source.is_dir() else source
    labels = {}
    times = []
    events = invalid = timing_invalid = 0
    last_phase = None
    complete = True
    try:
        read = 0
        with open(path, 'rb') as handle:
            for raw in handle:
                read += len(raw)
                if read > _INTEGRITY_BYTES:
                    complete = False
                    break
                text = raw.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except (ValueError, RecursionError):
                    invalid += 1
                    continue
                if not isinstance(row, dict):
                    invalid += 1
                    continue
                events += 1
                phase, kind = row.get('phase'), row.get('type')
                if phase is not None:
                    label = phase if isinstance(phase, str) and phase in GROK_PHASES else 'other_phase'
                    last_phase = label
                elif isinstance(kind, str) and kind in GROK_EVENT_TYPES:
                    label = kind
                else:
                    label = 'other_type'
                labels[label] = labels.get(label, 0) + 1
                if isinstance(kind, str) and kind in GROK_TOOL_TYPES:
                    labels.setdefault('_tools', 0)
                    labels['_tools'] += 1
                stamp = _progress_time(row)
                if stamp is False:
                    timing_invalid += 1
                elif stamp is not None:
                    times.append(stamp)
    except OSError:
        return dict(status='unavailable', timing='unavailable', reason='the event log could not be read')
    if invalid:
        complete = False
    tools = labels.pop('_tools', 0)
    reasoning = labels.get('streaming_reasoning', 0)
    waiting = labels.get('waiting_for_model', 0)
    times.sort()
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]

    def iso(stamp):
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    activity = ('tool_use' if tools else 'reasoning' if reasoning else 'waiting' if waiting
                else 'other_events' if events else 'no_events')
    timing = ('unavailable' if not times else 'partial' if timing_invalid or not complete else 'observed')
    return dict(status='observed' if complete and events else 'partial' if events else 'unavailable',
                timing=timing, events=events, invalid=invalid, timing_invalid=timing_invalid,
                labels=dict(sorted(labels.items())), tool_events=tools, reasoning_events=reasoning,
                last_phase=last_phase,
                first_at=iso(times[0]) if times else None, last_at=iso(times[-1]) if times else None,
                span_seconds=round(times[-1] - times[0], 3) if len(times) > 1 else None,
                longest_gap_seconds=round(max(gaps), 3) if gaps else None, activity=activity)


#: Bound on the bytes read when measuring a transcript's integrity.
_INTEGRITY_BYTES = 64 * 1024 * 1024


def _transcript_integrity(source: Path) -> dict:
    """Lines, unreadable lines and missing files of one call's transcript (a
    JSONL file, or a grok session folder's two JSONL files). Counts only;
    nothing of the content is kept. ``complete`` only when every expected
    file was read whole and every non-blank line was a JSON object."""
    files = [source] if source.is_file() else [source / 'events.jsonl', source / 'chat_history.jsonl']
    lines = invalid = missing = 0
    truncated = False
    for path in files:
        try:
            if not path.is_file():
                missing += 1
                continue
            read = 0
            with open(path, 'rb') as handle:
                for raw in handle:
                    read += len(raw)
                    if read > _INTEGRITY_BYTES:
                        truncated = True
                        break
                    text = raw.strip()
                    if not text:
                        continue
                    lines += 1
                    try:
                        value = json.loads(text)
                    except ValueError:
                        invalid += 1
                        continue
                    if not isinstance(value, dict):
                        invalid += 1
        except OSError:
            missing += 1
    return dict(lines=lines, invalid=invalid, missing_files=missing, truncated=truncated,
                complete=bool(lines) and not invalid and not missing and not truncated)


def _improvement_record(session):
    """The proposals aggregated from the run's recovery occurrences
    (quadratus.recovery.proposals): deterministic, labelled proposed, never
    adopted by this record. Recomputed from the carried occurrences, so a
    resumed job neither loses nor duplicates them."""
    from . import recovery
    records = list(getattr(session, 'recoveries', []) or []) if session else []
    return dict(proposals=recovery.proposals(records), cost=recovery.costs(records),
                adoption='none: a proposal becomes engine behaviour only through a reviewed change')


def _improvement_section(session) -> str:
    from . import recovery
    records = list(getattr(session, 'recoveries', []) or []) if session else []
    return recovery.render(records, recovery.proposals(records)) if records else ''


def _requirements_record(session):
    if session is None:
        return None
    ledger = session.memory.ledger
    return {"listed": dict(ledger.requirements), "status": dict(ledger.requirement_status),
            "ambiguous": dict(ledger.ambiguous),
            "decided_by_orchestrator": dict(ledger.decisions),
            "operator_rulings": list(ledger.rulings),
            "reviews": list(getattr(session, "requirement_reviews", []) or []),
            "audits": list(getattr(session, "requirement_audits", []) or []),
            # Review-only tasks the harness's own verification made redundant:
            # their requirements were covered, never met, by that evidence.
            "verification_handoffs": list(getattr(session, "verification_handoffs", []) or []),
            "verification_dispatched": list(getattr(session, "verification_dispatched", []) or [])}


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
