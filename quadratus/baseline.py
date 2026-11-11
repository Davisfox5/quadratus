"""One model, alone, on the same project, goal, checks and limits as a project run.

The whole-repo review of 2026-11-10 found no measurement that the multi-model
pipeline beats one model working alone on the same task and budget: no
single-model arm, and nothing to compare one with. ``run_baseline`` is that
arm. It is a measurement tool and nothing in the session engine reads it.

What the single model gets, and what it does not:

* The same lock, run folder, preflights and policy as ``run_project``: the
  vendor agent configuration check, the project policy's trust check, the
  operator's forbidden paths, the gate plan, the dependency-tree watch, the
  ``RunLimits`` budget and the lead turn cap. The helpers are ``project_run``'s
  own, not copies.
* One write-granted editing call through ``runtime.Fleet``, which is the
  transport, flags, check guidance, project watch and metering a lead's editing
  call gets. The prompt is the goal verbatim, the checks the harness will run,
  the path bounds (the operator's declared and forbidden paths, the dependency
  folders) and the round cap when the call carries one: every bound the call
  is held to is one its prompt states.
* The same deterministic feedback a lead gets: after the call the harness runs
  the project's gate, run-start test files included in their run-start form.
  A failure that is an attributable assertion failure buys one more editing
  call with the gate's output, up to ``fix_rounds`` times (default 1, the one
  fix round the integration gate gives a lead). The command checks of a
  project policy the operator approved are bound into that gate by the
  session's own function (``policy.task_gate``) and named to the model by id.
  Where the policy binds further checks by path rule, the baseline needs
  declared paths to bind them by, and is refused before any call without.
* None of the model review: no orchestrator, no decomposition, no reviewers,
  no workers, no consults and no close-out call.

A baseline never reports complete, met or verified. ``result.json`` carries
``mode: "baseline"``, ``completed: false`` and ``completion: "not judged"``
always; the run is judged from outside (frozen graders, ``tools/truth_check.py``,
``tools/compare_runs.py``).
"""

from __future__ import annotations

import dataclasses
import json
import time
import uuid
from types import SimpleNamespace

from .artifacts import ArtifactStore
from .delegation import (
    DelegationLedger,
    capture_invocations,
    invocation,
    invocation_context,
    reconcile,
)
from .deptree import DEPENDENCY_DIRS
from .project import Project
from .project_run import (
    ProjectResult,
    RunInterrupted,
    _accepted_report_lines,
    _calls_by_task,
    _child_environment_record,
    _collect_traces,
    _declared_checks_lines,
    _declared_checks_record,
    _decline_declared_checks,
    _extra_gate_commands,
    _fleet_for,
    _gate_for,
    _lead_cap,
    _new_run_dir,
    _preferences_record,
    _project_lock,
    _refuse_agent_config,
    _refuse_scratch_in_project,
    _refuse_standing_watch_findings,
    _selected_limits,
    _state_folder,
    _sweep_stale_scratch,
    _trust_record,
    _watch_record,
    _watch_report_lines,
)
from .repo_scan import scan_repo
from .run_budget import RunBudget
from .safe_io import tighten_files, write_private
from .usage import UsageMeter

__all__ = ["run_baseline", "baseline_model", "baseline_prompt", "fix_prompt", "round_budget", "MODE",
           "COMPLETION"]

MODE = "baseline"
#: What a baseline says about the goal, always.
COMPLETION = "not judged"
#: The one task id a baseline's calls and checks are filed under.
TASK = "baseline"

#: A rule the reply is held to, so the prompt states it (tests pin both). The
#: transport's own editing text mentions FETCH, CONSULT and WORKER requests;
#: nothing here serves one.
ALONE_RULE = ("You are working alone. No FETCH, CONSULT or WORKER request is served in this run: "
              "a reply that is such a request is kept as your whole answer and nothing is sent back.")
FEEDBACK = ("the project's own checks, run by the harness after each editing call, and their output "
            "on a failure the application can repair; no orchestrator, reviewer, worker, consult or "
            "close-out call")
#: The dependency watch stops a baseline as it stops a project run, so the
#: prompt says which folders it guards, named from the watch's own constant.
DEPENDENCY_RULE = ("Do not add, change or delete anything inside a dependency folder ("
                   + ", ".join(sorted(DEPENDENCY_DIRS)) + "): a change there stops the run and no "
                   "check is accepted after it.")


class PathBoundsExceeded(RuntimeError):
    """The editing call changed a path outside the operator's declared paths,
    or one the operator or the policy forbids."""


def baseline_model(model):
    """The roster row for ``model``, or ValueError before any call: the key
    must be an active, unrestricted seat. A restricted row is a bounded
    worker call with reduced tools, which is a different job from doing the
    whole goal, and an unknown key is nothing the Fleet can invoke."""
    from .registry import ROSTER, resolve
    spec = resolve(str(model or ""))
    seats = ", ".join(m.key for m in ROSTER if not m.restricted)
    if spec is None:
        raise ValueError(f"Baseline model {model!r} is not in the active roster. Choose one of: {seats}.")
    if spec.restricted:
        raise ValueError(f"Baseline model {model} is a restricted worker seat (reduced tools, bounded "
                         f"effort), not a model working alone on the goal. Choose one of: {seats}.")
    return spec


def _path_rules(scope) -> str:
    """The path bounds the measured diff is held to, as the prompt states them."""
    parts = []
    if scope.permitted_paths:
        parts.append("You may change only these paths: " + ", ".join(scope.permitted_paths) + ".")
    if scope.forbidden_paths:
        parts.append("Never change: " + ", ".join(scope.forbidden_paths) + ".")
    if parts:
        parts.append("A change outside these bounds stops the run with your work kept.")
    return " ".join(parts)


def round_budget(model, limit) -> str:
    """The call's round cap in words, or "" when the Fleet applies none.

    The Fleet binds a baseline's calls by ``Settings.lead_max_turns`` as it
    binds a lead's (roles ``lead`` and ``gate-fix``), and a per-call token
    ceiling derives one. Run 14: two leads were capped with nothing usable
    on disk because neither prompt said a cap existed. A cap the model is
    not told about measures the cap, not the model. Text only: the Fleet
    applies the cap, and a CLI with no turn flag (codex) has none."""
    if not limit:
        return ""
    from .cli_providers import CLI_SPECS
    spec = CLI_SPECS.get(str(model).partition(":")[0])
    if spec is None or not spec.max_turns_flag:
        return ""
    write_by = max(1, (2 * limit) // 3)
    return (f"This call has at most {limit} tool rounds. At round {limit} it stops with whatever is on "
            "disk and the run ends there: the checks are run once on what is there and no further "
            f"call follows. Plan against it: have the edits written by about round {write_by} and "
            "keep the last rounds for running the checks.")


#: How a check the project's approved policy binds is named to the model: by
#: id only, as a lead's role packet names it (policy.role_packet). A command
#: line can name a file a seat must not open.
POLICY_CHECK = "{id}: a check the project's policy adds. The harness runs it; its command is not shown to you."


def baseline_prompt(goal, checks, scope, fix_rounds, rounds="", policy_checks=()) -> str:
    """The single model's whole brief: the goal verbatim, the checks the
    harness will run, the path bounds, the round cap when there is one
    (``rounds``, from :func:`round_budget`), and nothing about orchestration.

    ``policy_checks`` are the ids of the checks an approved project policy
    adds to the gate (:func:`_policy_checks`). They are run and can buy a fix
    call like any other, so the list would be untrue without them: with a
    policy check and no operator check the prompt said "none configured" and
    "you get this one call"."""
    listed = "\n".join([f"- {c}" for c in checks]
                       + ["- " + POLICY_CHECK.format(id=name) for name in policy_checks]) or "- none configured"
    after = (f"If a check fails on something your change can repair, you are shown its output and get "
             f"{fix_rounds} more call{'s' if fix_rounds != 1 else ''} to fix it."
             if fix_rounds and (checks or policy_checks)
             else "You get this one call: a failing check is recorded, not sent back.")
    parts = ["Make the change described below in the files of your working directory. Do all of it "
             "in this call.",
             f"Goal (verbatim):\n{goal}",
             f"Checks the harness runs after your call:\n{listed}\n{after}",
             rounds, _path_rules(scope), DEPENDENCY_RULE, ALONE_RULE]
    return "\n\n".join(p for p in parts if p)


def fix_prompt(goal, gate_text, scope, used, fix_rounds, rounds="") -> str:
    """One fix call: the goal again and the gate's output, as a lead's gate-fix gets."""
    parts = [f"Goal (verbatim):\n{goal}",
             f"The project's own integration check failed after your work:\n{gate_text}",
             f"Fix the failure in the project files. This is fix call {used} of {fix_rounds}.",
             rounds, _path_rules(scope), DEPENDENCY_RULE, ALONE_RULE]
    return "\n\n".join(p for p in parts if p)


def run_baseline(goal, project, settings, *, model, check='', extra_checks=(), gates=None,
                 run_limits=None, state_dir=None, fix_rounds=1, forbid=(), declared_paths=(),
                 progress=None, readiness=None, trust_project_policy=None,
                 allow_project_agent_config=False, declared_checks=True, accept_watched_paths=False):
    """Run ``model`` alone on ``goal`` in ``project`` and record what happened.

    ``check``, ``extra_checks``, ``gates``, ``run_limits``, ``state_dir``,
    ``forbid``, ``declared_paths``, ``readiness``, ``trust_project_policy``,
    ``allow_project_agent_config``, ``declared_checks`` and
    ``accept_watched_paths`` mean what they mean to ``run_project``.
    ``fix_rounds`` is how many more editing calls a failing gate may buy (0
    for none).

    Returns a ``ProjectResult`` whose ``completed`` is always False: the run
    is not judged here. ``error`` names what stopped it, if anything did. An
    interrupt that lands while the call transcripts are copied is passed on
    as ``RunInterrupted`` once the record is written, as ``run_project``
    passes it on.
    """
    from .policy import load_policy
    from .scope import TaskScope

    if not str(goal or '').strip():
        raise ValueError('Describe the project change to make.')
    if type(fix_rounds) is not int or fix_rounds < 0:
        raise ValueError('fix_rounds must be a whole number of at least 0')
    baseline_model(model)
    if not isinstance(project, Project):
        project = Project(project)
    state = _state_folder(project, state_dir)
    # The preflights a project run makes, by its own helpers: a scratch root
    # inside the project, vendor agent configuration, and a watched path the
    # last run (of either kind) left changed.
    _refuse_scratch_in_project(project)
    agent_config = _refuse_agent_config(project, allow_project_agent_config, progress)
    watched_accepted = _refuse_standing_watch_findings(project, state, accept_watched_paths, progress)
    policy = load_policy(project.root, forbid=forbid, trust_project_policy=trust_project_policy,
                         **({} if declared_checks else {'declared_checks': False}))
    # The whole goal is one call here, so the task-size ceiling does not
    # apply; the path protections do, exactly as a task's scope carries them.
    scope = TaskScope(permitted_paths=tuple(declared_paths or ()),
                      forbidden_paths=tuple(policy.scope(None).forbidden_paths))
    extras = _extra_gate_commands(extra_checks)
    probes = ()
    if readiness:
        from .readiness import probes_from
        probes = probes_from(readiness, project.root)
    with _project_lock(project):
        _sweep_stale_scratch(progress)
        return _baseline(goal, project, settings, model=model, state=state, check=check, extras=extras,
                         gates=gates, run_limits=run_limits, fix_rounds=fix_rounds, progress=progress,
                         policy=policy, scope=scope, agent_config=agent_config,
                         watched_accepted=watched_accepted,
                         declared_checks=declared_checks, probes=probes)


def _stop_cause(exc) -> str:
    """What stopped the baseline, from typed facts and never from wording."""
    from .deptree import DependencyIdentityUnavailable, DependencyTreeChanged
    from .integration import CheckUnattributable
    from .providers import ProviderRefusal, TurnLimitReached
    from .run_budget import RunBudgetExceeded
    if isinstance(exc, KeyboardInterrupt):
        return 'interrupted'
    if getattr(exc, 'window_exhausted', False):
        return 'window exhausted'
    for kind, name in ((RunBudgetExceeded, 'budget'), (ProviderRefusal, 'refusal'),
                       (TurnLimitReached, 'turn limit'), (PathBoundsExceeded, 'path bounds'),
                       (CheckUnattributable, 'check not attributable'),
                       ((DependencyTreeChanged, DependencyIdentityUnavailable), 'dependency tree')):
        if isinstance(exc, kind):
            return name
    return type(exc).__name__


def _command_ids(plan) -> set:
    return {g['id'] for g in plan['gates'] if g.get('runner') == 'command'}


def _checks_bound_by_path(policy, preview, scope) -> str:
    """The command checks an approved project policy binds by path rule that
    this baseline's plan does not hold, named for the refusal; "" when there
    are none.

    A project run binds a policy's checks task by task, from each task's
    declared paths (``Session._prepare_dispatch``). A baseline is one call
    with no task scope. With no declared paths its plan is the policy's
    default, so a rule that binds a further command check to the very files
    the call goes on to change would never run, and the gate would pass on
    less than a project run's (review of 41e72f2: a rule on ``add.py`` bound
    ``lint``, the call changed ``add.py``, and ``lint`` did not run). Declared
    paths settle it: the call may change nothing else, and the plan is the
    one a project task declaring those paths gets."""
    if not policy.explicit or scope.permitted_paths:
        return ''
    bound = _command_ids(preview)
    missing = []
    for rule in policy.document.get('path_rules', []):
        ids = sorted(_command_ids(policy.resolve(rule['paths'])) - bound)
        if ids:
            missing.append(f"{', '.join(rule['paths'])}: {', '.join(ids)}")
    return '; '.join(missing)[:600]


def _policy_checks(gate, shown) -> tuple:
    """Ids of the checks the bound gate runs whose command the model was not
    shown: the ones the policy added. The operator's own stay as commands."""
    import shlex

    from .integration import model_facing
    return tuple(c.id for c in getattr(gate, 'commands', ()) or ()
                 if shlex.join(model_facing(c.argv)) not in shown)


def _ready(probes, project, watch, store, receipts):
    """The operator's readiness probes, once, before any model call, as the
    session runs them: a failure is an operator handoff with no call made,
    and a probe may not change project source or a dependency tree."""
    from .readiness import CapabilityProbeFailed, failure_message, run_probes
    before = project.fingerprint()
    receipts.extend(run_probes(probes, project.root))
    for receipt in receipts:
        try:
            receipt['output_artifact'] = store.put(receipt['output'], kind='readiness-output',
                                                   author='harness').id
        except Exception:  # noqa: BLE001 -- recorded, never hidden
            receipt['output_artifact'] = 'unavailable'
    watch.verify('during readiness probes')
    if project.fingerprint() != before:
        raise RuntimeError('A readiness probe changed project source; it must only observe.')
    failed = [r for r in receipts if not r['passed']]
    if failed:
        raise CapabilityProbeFailed(failure_message(failed[0]))


def _baseline(goal, project, settings, *, model, state, check, extras, gates, run_limits, fix_rounds,
              progress, policy, scope, agent_config, declared_checks, probes=(), fleet_type=None,
              watched_accepted=()):
    from .deptree import DependencyGuard, DependencyWatch
    from .integration import CheckUnattributable, attribute, original_tests, selection_sections
    from .run_activity import RunActivity
    from .runtime import Fleet
    from .scope import changed_paths, count_change_lines
    from .taskmeta import lead_request

    started = time.monotonic()
    run_dir = _new_run_dir(state)
    activity = RunActivity(run_dir, progress)
    progress = activity
    activity.record('run-started', goal=str(goal)[:4000], mode=MODE, model=model)
    before = project.contents()
    scan, declined = scan_repo(project.root), None
    if not declared_checks:
        scan, declined = _decline_declared_checks(scan)
    gate, plan = _gate_for(project, check, extras, gates, scan, run_dir, progress, declined=declined)
    store = ArtifactStore(run_dir / 'artifacts')
    meter = UsageMeter(run_dir / 'usage.jsonl')
    delegation = DelegationLedger(path=run_dir / 'invocations.jsonl')
    budget = RunBudget(run_limits, path=run_dir / 'budget.json') if run_limits else None
    settings, lead_turns, lead_source = _lead_cap(settings, run_limits, progress)
    selected = dict(_selected_limits(None, run_limits, None, lead_turns, lead_source),
                    mode=MODE, model=model, fix_rounds=fix_rounds)
    write_private(run_dir / 'run-limits.json', json.dumps(selected, indent=2) + '\n')
    progress(f'Baseline: {model} alone, up to {fix_rounds} fix call(s) after a failing check. '
             + selected['described'])
    preview = policy.resolve(scope.permitted_paths, writing=True)
    write_private(run_dir / 'policy-plan.json', json.dumps(preview, indent=2))
    fleet = _fleet_for(fleet_type or Fleet, settings, project=project, allow_writes=True, meter=meter,
                       delegation=delegation, budget=budget, progress=progress, plan=plan,
                       agent_config=agent_config)
    checks_shown = tuple(getattr(fleet, 'check_commands', ()) or ())
    # The cap the Fleet will apply to these calls, stated to the model.
    rounds = round_budget(model, getattr(settings, 'lead_max_turns', None))
    watch = DependencyWatch(DependencyGuard(project.root))
    originals, selection = original_tests(before), selection_sections(before)
    calls, attempts, checks, scope_reports, original_runs, not_run, ready = [], [], [], [], [], [], []
    # Names the editing calls changed in folders the measured diff does not
    # read (Fleet._watch_after). A project run's record carries them; the
    # baseline's did not, so a gate that passed on top of a build file read
    # the same as one that passed on the measured source.
    unmeasured = []
    state_ = SimpleNamespace(fixes=0, in_call=False, turn_limited=False)

    def edit(role, prompt):
        """One write-granted call, bracketed as a lead's is: the dependency
        identity before and after, the measured diff against the path
        bounds, the prompt and the reply kept."""
        watch.verify(f'before {role} ({TASK})')
        entry = dict(role=role, prompt_artifact=None, reply_artifact=None, changed_report=None,
                     unserved_request=None, outcome='failed')
        calls.append(entry)
        try:
            entry['prompt_artifact'] = store.put(prompt, kind='prompt', author=model).id
        except Exception:  # noqa: BLE001 -- evidence never fails a call
            pass
        call = dict(call_id=uuid.uuid4().hex, task=TASK, role=role, model=model)
        activity.record('call-started', **call)
        state_.in_call = True
        try:
            with capture_invocations(), invocation(TASK, role, prompt_artifact=entry['prompt_artifact']):
                try:
                    reply = fleet.invoke(model, prompt, allow_writes=True)
                finally:
                    # Also after a call that failed: what it left is there either way.
                    _keep_unmeasured(unmeasured, (invocation_context.get() or {}).get('unmeasured_changes'))
                context = invocation_context.get() or {}
                entry['changed_report'] = context.get('changed_report')
                report = scope.assess(project.diff(before))
                scope_reports.append(report)
                if report.blocking:
                    raise PathBoundsExceeded('The call changed a path outside its bounds; work '
                                             'preserved. ' + report.render())
        except BaseException as primary:
            entry['outcome'] = type(primary).__name__
            partial = getattr(primary, 'partial_text', None)
            if partial:
                # A capped call's narration: kept as evidence, never read as a result.
                try:
                    entry['reply_artifact'] = store.put(partial, kind='capped-partial', author=model).id
                except Exception:  # noqa: BLE001 -- evidence never replaces the stop
                    pass
            watch.after_failure(f'during {role} ({TASK})', primary)
            raise
        finally:
            activity.record('call-ended', **call)
        watch.verify(f'during {role} ({TASK})')
        state_.in_call = False
        entry['outcome'] = 'ok'
        try:
            entry['reply_artifact'] = store.put(reply, kind='monitor-reply', author=model).id
            activity.record('reply', artifact=entry['reply_artifact'], **call)
        except Exception:  # noqa: BLE001 -- evidence never fails a call
            pass
        request = lead_request(reply)
        if request is not None:
            # Stated in the prompt (ALONE_RULE): nobody answers, the reply stands.
            entry['unserved_request'] = request.split(None, 1)[0].rstrip(':')[:20]
        for item, why in _not_run_lines(reply):
            # ``checks`` holds one entry, the last gate run, which always
            # follows the reply that reported this.
            not_run.append(dict(task=TASK, role=role, author=model, item=item, why=why,
                                requirements=[], after_check=0))
        return reply

    def run_gate(*, observed=False):
        """The gate on the current source, with the run-start tests held in
        their run-start form. One typed attempt on the record per run."""
        result = _checked(gate, project, watch, activity, originals, selection, original_runs)
        source = project.fingerprint()
        try:
            output = store.put(result.output or '', kind='check-output', author='harness').id
        except Exception:  # noqa: BLE001 -- the receipt stands without its artifact
            output = 'unavailable'
        attempt = dict(attempt=len(attempts) + 1, passed=result.passed, returncode=result.returncode,
                       gate='full', output_artifact=output, source=source, report=result.report,
                       receipts=[dict(id=r.id, status=r.status, reason=r.reason, tests=r.tests,
                                      returncode=r.returncode, cached=r.cached, source_hash=r.source_hash,
                                      runner_hash=r.runner_hash, report=r.report) for r in result.receipts],
                       **({'observed_after_stop': True} if observed else {}))
        if not result.passed:
            attempt['attribution'] = attribute(result)
        attempts.append(attempt)
        # One entry, the latest: what the run's final check says.
        checks[:] = [_check_record(gate, result, project, source, observed)]
        return result

    error, cause = '', None
    try:
        if preview['blocked']:
            raise ValueError('; '.join(preview['blocked']))
        # A project run binds an approved policy's command checks to every
        # task (policy.task_gate). The baseline runs the same ones, or its
        # gate result would mean less than the policy plan it writes says
        # (final review of the 1110 batch, run_and_surfaces 4). Inside the
        # try: a policy that cannot be bound is a recorded stop, no call made.
        from .policy import PolicyError, task_gate
        gate = task_gate(policy, preview, gate, exclude=project.exclude)
        by_path = _checks_bound_by_path(policy, preview, scope)
        if by_path:
            # Refused, not run on the lesser gate: the result would read as a
            # project run's gate and mean less.
            raise PolicyError(
                "The project's approved policy binds command checks by path that a baseline with no "
                f"declared paths would not run ({by_path}). A project run binds them task by task from "
                "each task's declared paths; a baseline is one call with no task scope, so its gate "
                "would not be a project run's. Nothing was run and no model was called. Name the paths "
                "this baseline may change (--path, declared_paths=...): the checks the policy binds to "
                "them are then run.")
        # The checks the policy added, by id: the model is told every check
        # the harness runs, and never a policy check's command.
        added = _policy_checks(gate, checks_shown) if policy.explicit else ()
        # Before any model call: over the bounds is a stop with no call made.
        watch.start()
        if probes:
            _ready(probes, project, watch, store, ready)
        edit('lead', baseline_prompt(goal, checks_shown, scope, fix_rounds, rounds, added))
        result = run_gate() if gate is not None else None
        while result is not None and not result.passed and state_.fixes < fix_rounds:
            verdict = attempts[-1]['attribution']
            if not verdict['product']:
                # The rule a lead's gate-fix is held to (integration.attribute):
                # only an assertion failure in a test body is the application's.
                raise CheckUnattributable(
                    'the check failed without an attributable assertion failure '
                    f"({'; '.join(verdict['reasons'])[:400]}); no fix call was made. Work preserved.")
            state_.fixes += 1
            edit('gate-fix', fix_prompt(goal, result.for_models(), scope, state_.fixes, fix_rounds, rounds))
            result = run_gate()
    except (Exception, KeyboardInterrupt) as exc:  # the record says why, with the work kept
        from .providers import TurnLimitReached
        error, cause = f'{type(exc).__name__}: {exc}', _stop_cause(exc)
        state_.turn_limited = isinstance(exc, TurnLimitReached)
        if (gate is not None and state_.in_call and not isinstance(exc, KeyboardInterrupt)
                and not watch.failure and not getattr(fleet, 'watch_findings', None)):
            # A call that stopped may have left its work on disk (a turn cap,
            # a token threshold reported after the call returned). The gate
            # costs no model call, so work no check has seen is checked once
            # and marked as such; it buys no fix and never replaces the stop.
            # Never after the project watch found a sensitive change: a check
            # run over a planted conftest proves nothing.
            try:
                unseen = (project.fingerprint() != attempts[-1]['source'] if attempts
                          else project.contents() != before)
                if unseen:
                    run_gate(observed=True)
            except BaseException as later:  # noqa: BLE001 -- the stop above is the outcome
                progress(f'The check after the stop did not finish: {type(later).__name__}')
    finally:
        fleet.close()

    traces, trace_interrupted = _collect_traces(run_dir, project, delegation, progress)
    diff = project.diff(before)
    changed = changed_paths(diff)
    in_flight = {}
    if error and diff:
        in_flight = dict(note='The baseline stopped; the edits made so far are kept.', changed=sorted(changed),
                         changed_lines=count_change_lines(diff))
        write_private(run_dir / 'in-flight.json', json.dumps(in_flight, indent=2))
    invoked = [e for e in delegation.events if getattr(e, 'invoked', False)]
    known = [e.total_tokens for e in invoked if e.tokens_known]
    last = checks[-1] if checks else None
    record = {
        'mode': MODE, 'project': str(project.root), 'goal': goal,
        # Never a claim: a baseline has no judge of its own (module docstring).
        'completed': False, 'completion': COMPLETION,
        'allow_writes': True, 'error': error,
        'model': model, 'models': [model],
        'baseline': dict(
            model=model, fix_rounds=fix_rounds, fix_rounds_used=state_.fixes,
            editing_calls=len(calls), calls=len(invoked),
            reported_tokens=sum(known) if known else None,
            unknown_usage_calls=len(invoked) - len(known),
            seconds=round(time.monotonic() - started, 1),
            model_seconds=round(sum(e.seconds or 0.0 for e in invoked), 1),
            stop_cause=cause, feedback=FEEDBACK,
            size_ceiling='none: the whole goal is one call, so the per-task line ceiling does not apply',
            gate=dict(configured=gate is not None, attempts=len(attempts),
                      passed=last['passed'] if last else None,
                      observed_after_stop=bool(last and last.get('observed_after_stop'))),
            edit_calls=calls,
            note='One model alone. This record makes no claim about the goal or its requirements; '
                 'judge it from outside (graders, tools/truth_check.py, tools/compare_runs.py).'),
        'checks': checks,
        'source_changed': bool(diff), 'source_fingerprint': project.fingerprint(),
        'tasks': 0,
        'turn_limited_tasks': [TASK] if state_.turn_limited else [],
        'failed_tasks': [],
        'personal_preferences': _preferences_record(settings),
        'requirements': None, 'design_checks': [], 'findings': [],
        'dependency_identity': watch.record,
        'workflow': {'tasks': [dict(task_id=TASK, checks=attempts)],
                     'calls_by_task': _calls_by_task(SimpleNamespace(
                         config=SimpleNamespace(delegation_ledger=delegation))),
                     'run': {'readiness': ready}},
        'survey': None, 'explicit_tasks': None, 'parallel_batches': [],
        'trace': {'calls': len(traces), **({'interrupted': True} if trace_interrupted else {}),
                  'transcripts_found': sum(1 for t in traces if t.get('tool_calls') is not None)},
        'decisions': [], 'in_flight': in_flight, 'policy_preview': preview,
        'declared_checks': _declared_checks_record(declined),
        'trust': _trust_record(policy, agent_config, _declared_checks_record(declined), watched_accepted),
        'child_environment': _child_environment_record(),
        'budget': budget.snapshot() if budget else None,
        'selected_limits': selected,
        'original_test_runs': original_runs,
        'unexecuted_acceptance': not_run,
        # In a project run's shape, by task: tools/truth_check.py reads it.
        'unmeasured_changes': {TASK: unmeasured} if unmeasured else {},
        'watch_findings': list(getattr(fleet, 'watch_findings', []) or []),
        # In a project run's own shape: the next run's preflight, of either
        # kind, reads this to refuse a watched path this call left changed.
        'project_watch': _watch_record(fleet),
        'delegation': reconcile(delegation.events, delegation.native_children.values()),
        'scope_reports': [
            {'within_scope': r.within_scope, 'out_of_scope': r.out_of_scope, 'changed': r.changed,
             'changed_lines': r.changed_lines, 'max_lines': r.max_lines, 'code_lines': r.code_lines,
             'test_lines': r.test_lines, 'oversized': r.oversized} for r in scope_reports],
    }
    report = _report(record, run_dir, diff, meter, delegation, traces)
    write_private(run_dir / 'changes.diff', diff)
    write_private(run_dir / 'report.md', report)
    write_private(run_dir / 'result.json', json.dumps(record, indent=2))
    write_private(run_dir / 'delegation.md', delegation.render_report())
    tighten_files(run_dir)
    result = ProjectResult(False, report, run_dir, diff, error)
    if trace_interrupted:
        # The record is whole. The operator's interrupt goes on to the caller
        # as a project run's does, so a loop over baselines stops here.
        raise RunInterrupted(result)
    return result


def _keep_unmeasured(kept, names) -> None:
    """Add one call's unmeasured names to the run's list: once each, bounded
    as the session bounds a task's. An observation; never raises."""
    try:
        from .session import _UNMEASURED_NAMES
        for name in names or ():
            name = str(name)[:300]
            if name not in kept and len(kept) < _UNMEASURED_NAMES:
                kept.append(name)
    except Exception:  # noqa: BLE001 -- an observation never fails a call
        pass


def _not_run_lines(reply):
    """Each ``NOT RUN:`` or ``BLOCKED:`` line of a reply as ``(item, why)``,
    read with the session's own pattern."""
    from .session import _NOT_RUN_LINE
    for match in _NOT_RUN_LINE.finditer(reply or ''):
        item, _, why = match.group(1).partition(' - ')
        item = item.strip().strip('`')
        if item and item.lower() not in ('none', 'nothing', 'n/a'):
            yield item[:300], why.strip()[:300]


def _checked(gate, project, watch, activity, originals, selection, original_runs):
    """Run the gate as the session's check does: the dependency identity
    before and after, a tree that moved under the check fails it and names
    what moved, and a changed run-start test file brings the original tests."""
    from .session import _describe_tree_change
    watch.verify(f'before check ({TASK})')
    current = project.contents()
    activity.record('check-started', task=TASK)
    try:
        result = gate.run()
    finally:
        activity.record('check-ended', task=TASK)
    # A receipt taken with a changed dependency tree is never accepted.
    watch.verify(f'during check ({TASK})')
    after = project.contents()
    if current != after:
        return dataclasses.replace(result, passed=False, output=_describe_tree_change(current, after))
    return _with_original_tests(gate, result, project, watch, originals, selection, after, original_runs)


def _with_original_tests(gate, result, project, watch, originals, selection, current, original_runs):
    """The gate's result joined by the run-start tests when the call changed
    one (integration.run_original_tests), so a baseline cannot pass its gate
    by editing the tests the gate runs. No approval or repair path exists
    here, and no finding to carry a doubt: when the original tests cannot be
    established, in whole or for one skipped command, the run is recorded as
    such (``passed: None``) and a passing check is not accepted as a pass. A
    failing check stays its own failure."""
    from .integration import (
        GateReceipt,
        added_selectors,
        changed_originals,
        changed_selection,
        run_original_tests,
    )
    if not originals:
        return result
    moved = changed_selection(selection, current)
    def not_established(names, problem, partial=()):
        if not result.passed:
            return result
        inner = result.receipts or (GateReceipt(
            id='check', status='passed', reason='exit 0', required=True, command=result.command,
            returncode=result.returncode, output=result.output, report=result.report, cases=result.cases),)
        text = (f"Run-start test files changed ({', '.join(names)}) and the original tests were not "
                f"established: {problem}. The check is not accepted as a pass.")
        return dataclasses.replace(
            result, passed=False, returncode=result.returncode or 1,
            output=f'{result.output}\n\n{text}'[-8_000:],
            receipts=tuple(inner) + tuple(partial) + (GateReceipt(
                id='original-tests', status='blocked', reason=str(problem)[:300], required=True),))

    if moved:
        problem = (f"test selection in {', '.join(moved)} changed since run start, so the original "
                   "tests cannot be run as they were")
        original_runs.append(dict(task=TASK, changed=moved, passed=None, receipts=[], problem=problem))
        return not_established(moved, problem)
    added = added_selectors(originals, current)
    changed = changed_originals(originals, current) + added
    if not changed:
        return result
    restored, problem = run_original_tests(gate, project.root, originals, current)
    watch.verify(f'during original tests ({TASK})')
    if project.contents() != current:
        return dataclasses.replace(result, passed=False,
                                   output='Project source changed while the original tests ran.')
    # A command that names a test file the call added cannot run in the copy
    # and is skipped there, with every run-start file it also names. The
    # session carries that as an open finding (never a pass); a baseline has
    # no findings, so a restored run that passed only by skipping a command
    # is not established either.
    skipped = [r for r in (restored.receipts if restored else ()) if r.status == 'skipped']
    unrun = restored is not None and restored.passed and bool(skipped)
    if unrun:
        problem = '; '.join(f'{r.id} did not run against the run-start test files ({r.reason})'
                            for r in skipped)[:600]
    original_runs.append(dict(
        task=TASK, changed=changed, added=added,
        passed=None if restored is None or unrun else restored.passed,
        problem=problem, receipts=[dict(id=r.id, status=r.status, reason=r.reason, tests=r.tests)
                                   for r in (restored.receipts if restored else ())]))
    if restored is None or unrun:
        return not_established(changed, problem, restored.receipts if restored else ())
    inner = result.receipts or (GateReceipt(
        id='check', status='passed' if result.passed else 'failed',
        reason='exit 0' if result.passed else 'nonzero exit', required=True, command=result.command,
        returncode=result.returncode, output=result.output, report=result.report, cases=result.cases),)
    heading = (f"Original tests (run-start versions of {', '.join(changed)}) against the current source: "
               f"{'PASSED' if restored.passed else 'FAILED'}")
    return dataclasses.replace(
        result, passed=result.passed and restored.passed,
        returncode=result.returncode if not result.passed else restored.returncode,
        output=f'{result.output}\n\n{heading}\n{restored.output}'[-8_000:],
        receipts=tuple(inner) + tuple(restored.receipts))


def _check_record(gate, result, project, source, observed):
    """One check in the shape a project run's ``checks`` list carries."""
    from .integration import IntegrationGate, config_selection
    from .session import _gate_identity
    selection = ''
    if isinstance(gate, IntegrationGate):
        try:
            selection = config_selection(gate.command, project.root, project.root)
        except Exception as exc:  # noqa: BLE001 -- unknown selection is selection
            selection = f'selection unreadable: {type(exc).__name__}'
    return {'passed': result.passed, 'command': result.command, 'gate': _gate_identity(gate),
            'output': result.output, 'cwd': str(project.root), 'selection': selection,
            'command_cwd': '.', 'cases': result.cases, 'report': result.report, 'source': source,
            'receipts': [dataclasses.asdict(r) for r in result.receipts],
            **({'observed_after_stop': True} if observed else {})}


def _report(record, run_dir, diff, meter, delegation, traces):
    info = record['baseline']
    tokens = info['reported_tokens']
    lines = [f"# Baseline run: {info['model']} alone; the goal was not judged", '',
             f"Project: {record['project']}", '', f'Run files: {run_dir}', '',
             f"Calls: {info['calls']}; reported tokens: {'unknown' if tokens is None else f'{tokens:,}'}"
             + (f" ({info['unknown_usage_calls']} call(s) reported no usage)"
                if info['unknown_usage_calls'] else '')
             + f"; {info['seconds']} s.", '',
             f"Fix calls after a failing check: {info['fix_rounds_used']} of {info['fix_rounds']} allowed.", '',
             f"The model was given {FEEDBACK}.", '']
    lines += _watch_report_lines(record['project_watch'])
    lines += _accepted_report_lines(record['trust']['watched_paths_accepted'])
    lines += _declared_checks_lines(record['declared_checks'])
    if record['error']:
        lines += [f"Stopped ({info['stop_cause']}): {record['error']}", '']
    lines += ['Source changes are saved in the project folder.' if diff else
              'No source changes were produced. Any passing checks describe the existing tree.', '']
    for check in record['checks']:
        lines += [f"Check {'PASSED' if check['passed'] else 'FAILED'}"
                  + (' (run once after the stop, with no fix call)' if check.get('observed_after_stop') else '')
                  + f": {check['command']}", '', check['output'], '']
    if not record['checks']:
        lines += ['No integration check ran; this result has not been test-verified.', '']
    for run in record['original_test_runs']:
        state = {True: 'passed', False: 'FAILED', None: 'were not established'}[run['passed']]
        lines += [f"Run-start test files changed ({', '.join(run['changed'])}): the original tests {state}"
                  + (f" ({run['problem']})" if run.get('problem') else '') + '.', '']
    lines += [info['note'], '', meter.render_report(), '']
    if traces:
        from .trace import render_timeline
        lines += [render_timeline(traces), '']
    lines += [delegation.render_report()]
    return '\n'.join(lines)
