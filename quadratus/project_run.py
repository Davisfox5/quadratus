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
from .project_watch import ALLOW_AGENT_CONFIG_ENV
from .providers import ProviderError
from .repo_scan import scan_repo, seed_map
from .run_budget import RunBudget
from .safe_io import make_private_dir, prepare_state_dir, tighten_files, write_private
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


class RunInterrupted(KeyboardInterrupt):
    """The operator's interrupt, passed on once the run's record is written.

    A KeyboardInterrupt, so a driver that loops over runs stops on it as it
    did. ``result`` is the finished run's ProjectResult: the record is whole
    and on disk, and a caller that catches the interrupt can say where."""

    def __init__(self, result):
        super().__init__('interrupted after the run record was written')
        self.result = result


def _publish_in_flight(store, run_dir, in_flight: dict) -> None:
    """Preserved partial work: a record of the committed generation when the
    store still publishes, and the historical export either way."""
    try:
        store.publish({'in_flight': in_flight})
    except Exception:  # noqa: BLE001 -- a latched store keeps its last generation
        log.warning('could not publish the in-flight record', exc_info=True)
    write_private(run_dir / 'in-flight.json', json.dumps(in_flight, indent=2))


def _store_readable(store) -> bool:
    try:
        store.current()
        return not store.refused
    except Exception:  # noqa: BLE001 -- a corrupt manifest is reported, never read around
        return False


@contextmanager
def _project_lock(project):
    folder = project.root / '.quadratus'
    # Owner-only, and ignored by git, from the moment it exists: it holds run
    # records, transcripts and samples, never source.
    prepare_state_dir(folder)
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
                session_options=None,
                state_dir=None, max_tasks=20, mode='adversarial',
                progress=None, ask_operator=None, plan_gate=None,
                default_scope=None, run_limits=None, forbid=(), declared_paths=(),
                security_verdict_json=False, gates=None, extra_checks=(), capture_profile=None,
                readiness=None, survey=None, direct_tier=False, tasks=None, decider=None,
                decider_labels="unstated", repair_tests=(), test_changes=None,
                trust_project_policy=None, allow_project_agent_config=False, declared_checks=True,
                compete="off", accept_watched_paths=False, resume_from=None):
    """Keep both successful and interrupted runs next to their source tree.

    ``compete`` is ``"off"`` (the run as it always was) or ``"complex"``: a
    task routed at complex difficulty is drafted twice, blind, by two senior
    seats from different vendors, each in its own copy, and measured facts
    pick the draft that is merged (quadratus.contest). The result's
    ``contests`` section records each one, ``reviewers`` records what each
    review seat raised, and both are appended to the project's
    ``scoreboard.jsonl`` in the state folder, a record nothing reads back.

    ``accept_watched_paths`` starts a run although an earlier run on this
    project recorded a change to a watched path (a git hook, an env file, a
    link: quadratus.project_watch) that is still in place (also
    ``QUADRATUS_ACCEPT_WATCHED_PATHS=1``). By default such a project is
    refused before any model call, with the paths named. An acceptance
    lasts: it is written to this run's record (``trust``), and later runs
    read it there and do not ask about the same paths again.

    ``trust_project_policy`` is the operator's approval of the project's own
    ``.quadratus/policy.json``, by that file's sha256 (also
    ``QUADRATUS_TRUST_PROJECT_POLICY``). Without it a policy that grants
    anything, such as a command gate, is refused before any model call; one
    that only narrows loads as before (quadratus.policy).

    ``allow_project_agent_config`` lets a run start in a project that carries
    vendor agent configuration (``.claude/settings.json``, ``.mcp.json``,
    ``.codex/``, ``.grok/``; also ``QUADRATUS_ALLOW_PROJECT_AGENT_CONFIG=1``).
    By default such a project is refused before any model call.

    ``declared_checks`` False runs only the operator's own checks (``check``,
    ``extra_checks``, ``gates``): nothing the project declares is added or
    used in their place. True is the run as it always was.

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

    ``test_changes`` is an operator-authored JSON envelope, or its file path,
    binding intentional existing-test changes to this goal, original bytes
    and exact cases. A planned migration still needs approval of its concrete
    resulting diff unless its exact current_sha256 was approved in advance.
    """
    # Before anything else, in words: None (a cleared form field) used to end
    # as a TypeError from the comparison below, and a fraction went on to the
    # session. Nothing is coerced here; a caller that parses text hands over
    # a real whole number.
    if isinstance(max_tasks, bool) or not isinstance(max_tasks, int):
        raise ValueError(f'max_tasks must be a whole number of at least 1, not {max_tasks!r}')
    from .runtime import Fleet, new_session

    if not goal.strip():
        raise ValueError('Describe the project change to make.')
    if decider_labels not in ('unstated', 'all'):
        raise ValueError("decider_labels must be 'unstated' or 'all'")
    from .contest import COMPETE_MODES
    if compete not in COMPETE_MODES:
        raise ValueError(f"compete must be one of {', '.join(COMPETE_MODES)}")
    if max_tasks < 1:
        raise ValueError('max_tasks must be at least 1')
    # Refused here, before the lock or any call. A mode with no peer from a
    # vendor other than the orchestrator's cannot pass the requirements audit
    # (registry.mode_audit_gap), and a session setting outside the table or
    # under its floor is a caller's mistake (session_knobs).
    from .registry import mode_audit_gap
    from .session_knobs import session_options as _checked_options
    audit_gap = mode_audit_gap(mode)
    if audit_gap and SessionConfig(mode=mode).requirements_ledger:
        raise ValueError(audit_gap)
    session_options = _checked_options(session_options)
    if tasks is not None:
        tasks = list(tasks)
        if not tasks or any(not str(t).strip() for t in tasks):
            raise ValueError('tasks must name at least one task, each a non-empty text')
        if len(tasks) > max_tasks:
            raise ValueError(f'max_tasks ({max_tasks}) must cover the {len(tasks)} listed task(s)')
    if not isinstance(project, Project):
        project = Project(project)
    state = _state_folder(project, state_dir)
    _refuse_scratch_in_project(project)

    repair_tests = _repair_tests(project.root, repair_tests, forbid)
    test_originals = None
    if resume_from is not None and test_changes:
        # Read only the digest-verified original bytes here. These are the
        # baseline the operator's approval names, not the already edited
        # project. _run still validates the whole checkpoint, current source,
        # authority and workflow before restoring or granting anything.
        from . import checkpoint
        parent = _resume_parent(state, resume_from)
        body = checkpoint.load(parent)
        try:
            test_originals = checkpoint._decode(body['state']['_original_tests'])
        except (KeyError, TypeError, ValueError) as exc:
            raise checkpoint.CheckpointRefused('the checkpoint has no readable original-test baseline') from exc
        if not isinstance(test_originals, dict) or any(
                not isinstance(k, str) or not isinstance(v, bytes) for k, v in test_originals.items()):
            raise checkpoint.CheckpointRefused('the checkpoint has no readable original-test baseline')
    test_changes = _test_changes(project.root, test_changes, goal=goal, forbid=forbid, originals=test_originals)
    agent_config = _refuse_agent_config(project, allow_project_agent_config, progress)
    watched_accepted = _refuse_standing_watch_findings(project, state, accept_watched_paths, progress)
    # The built-in policy names the scanned check as a gate; with the
    # project's declarations declined it is built from a scan without them,
    # and says the operator declined (policy.load_policy).
    policy = load_policy(project.root, forbid=forbid, scan=None,
                         trust_project_policy=trust_project_policy,
                         **({} if declared_checks else {'declared_checks': False}))
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
        _sweep_stale_scratch(progress)
        return _run(goal, project, settings, state=state, allow_writes=allow_writes,
                    check=check, max_tasks=max_tasks, mode=mode, progress=progress,
                    ask_operator=ask_operator, plan_gate=plan_gate,
                    default_scope=default_scope,
                    run_limits=run_limits, policy=policy, gates=gates,
                    security_verdict_json=security_verdict_json,
                    session_options=session_options,
                    fleet_type=Fleet, session_factory=new_session, extras=extras,
                    capture_profile=profile, readiness=probes, survey=survey, direct_tier=direct_tier,
                    tasks=list(tasks) if tasks is not None else None, decider=decider,
                    decider_labels=decider_labels, repair_tests=repair_tests, test_changes=test_changes,
                    declared_checks=declared_checks, agent_config=agent_config, compete=compete,
                    watched_accepted=watched_accepted, resume_from=resume_from)


def _refuse_scratch_in_project(project):
    """Refuse, before anything is created, a scratch root inside the project:
    copies made there would be measured and reviewed as its source. Shared by
    every run that edits a project (a project run, a baseline)."""
    from .providers import scratch_inside
    misplaced = scratch_inside(project.root)
    if misplaced:
        raise ValueError(misplaced + ' Nothing was run and no model was called.')


def _sweep_stale_scratch(progress=None):
    """Remove what a killed run left under the scratch root, a day old or
    more, and say so. Called under the project lock."""
    from .providers import sweep_scratch
    swept = sweep_scratch()
    if swept and progress:
        progress(f'Removed {len(swept)} leftover scratch folder(s) or file(s) from earlier runs, '
                 'each more than 24 hours old.')
    return swept


def _refuse_agent_config(project, allowed=False, progress=None):
    """Refuse, before any model call, a project that carries configuration a
    vendor's agent CLI reads from its working directory.

    A headless CLI skips the trust prompt an interactive one shows, so hooks
    and MCP servers named in ``.claude/settings.json``, ``.mcp.json``,
    ``.codex/`` or ``.grok/`` can run as the operator on the first call. No
    review copy ever carries these files, but an editing call runs in the
    project itself, so the operator decides. Returns what was found and
    allowed, path to the record taken of it: the exact files the operator
    saw, which Fleet holds every later editing call to.
    """
    from .project_watch import ProjectWatch, WatchUnavailable, agent_config, agent_config_permitted
    try:
        # The state folder holds no agent configuration and has its own
        # preflight (the stale-fixture retirement), so it is left unopened.
        found = agent_config(ProjectWatch(project.root, exclude=project.exclude,
                                          state_files=False).listing())
    except WatchUnavailable as exc:
        raise ValueError('The project could not be checked for vendor agent configuration, so the '
                         f'run stops before any model call: {exc}') from exc
    if not found:
        return {}
    names = list(found)
    named = ', '.join(names[:20]) + (f' and {len(names) - 20} more' if len(names) > 20 else '')
    if agent_config_permitted(allowed):
        if progress:
            progress(f'Vendor agent configuration in the project, allowed by the operator: {named}')
        return found
    raise ValueError(
        f'The project carries vendor agent configuration: {named}. A headless vendor CLI started '
        'in this folder can run hooks and start MCP servers from these files without asking, as '
        'you, on the first call. Nothing was run and no model was called. Read them, then remove '
        'them or allow them with --allow-project-agent-config '
        f'(run_project(allow_project_agent_config=True)) or {ALLOW_AGENT_CONFIG_ENV}=1.')


#: The most of an earlier run's ``result.json`` the preflight reads, how many
#: run folders it lists, and how many of the newest records it reads (by
#: name, and again by when they were written).
_MAX_OLD_RECORD_BYTES = 32 * 1024 * 1024
_MAX_RUNS_LISTED = 20_000
_NEWEST_RUNS_TRIED = 20
#: The name under which a record that may not name every path its run
#: changed is asked about and accepted. Not a path: no listing holds it.
_UNNAMED_PATHS = '(watched paths the record does not name)'


class _TooManyRunFolders(Exception):
    """More folders under ``runs`` than the preflight lists, so the newest
    records cannot be told from the rest."""


def _read_bounded(path, limit):
    """The file's bytes, or raise OSError: a regular file only, never
    through a link, never more than ``limit``."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    try:
        import stat as _stat
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise OSError('not a regular file')
        if info.st_size > limit:
            raise OSError(f'larger than {limit:,} bytes')
        chunks, left = [], limit + 1
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b''.join(chunks)
        if len(data) > limit:
            raise OSError(f'larger than {limit:,} bytes')
        return data
    finally:
        os.close(fd)


def _recent_run_records(state):
    """``(records, notes)``: the earlier run records the standing-finding
    preflight reads, oldest run name first. Each is ``dict(run, written,
    paths, accepted)``: the folder's name, when its ``result.json`` was last
    written (lstat ctime, which no write can set back), the watched paths it
    recorded as changed and the ones its operator accepted at its start.

    Not one record but a window of them. The newest-named folder alone was
    the test, and ``runs`` is a folder an editing call can write: the call
    that planted a path added one folder holding ``{}`` and the finding was
    gone (final review, trust_transport 3). The window is the newest
    ``_NEWEST_RUNS_TRIED`` records by name and the same number by when they
    were written. A run's own record is written after its last call, so no
    folder that run's calls made can be written later than it, however many
    there are and whatever they are named. A folder with no record takes no
    place in either. An old record is untrusted input: nothing about it
    raises, and one that cannot be read is a note.

    Two bounds, and neither is passed in silence (review of the fix set):
    the listing stopped at ``_MAX_RUNS_LISTED`` folders in whatever order
    the filesystem gave them, so a call that made more than that could leave
    its own run's record unread, and that is now ``_TooManyRunFolders``; and
    a record holds at most ``WATCH_RECORD_LIMIT`` changed paths, so a call
    that changed more (a hundred decoy run folders are enough) could keep
    the one that mattered off it. A record that is full, or says its
    findings could not be kept, is marked ``unnamed``."""
    from .project_watch import WATCH_RECORD_LIMIT
    runs = Path(state) / 'runs'
    try:
        names = []
        with os.scandir(runs) as listing:
            for entry in listing:
                if entry.is_dir(follow_symlinks=False):
                    if len(names) >= _MAX_RUNS_LISTED:
                        raise _TooManyRunFolders(
                            f'There are more than {_MAX_RUNS_LISTED:,} folders under {runs}')
                    names.append(entry.name)
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f'the run records under {runs} could not be listed ({type(exc).__name__})']
    written = {}
    for name in names:
        try:
            written[name] = os.lstat(runs / name / 'result.json').st_ctime_ns
        except OSError:
            continue        # a run that ended before it wrote a record
    window = set(sorted(written, reverse=True)[:_NEWEST_RUNS_TRIED])
    window |= set(sorted(written, key=lambda name: (written[name], name), reverse=True)[:_NEWEST_RUNS_TRIED])
    records, notes = [], []
    for name in sorted(window):
        try:
            data = _read_bounded(runs / name / 'result.json', _MAX_OLD_RECORD_BYTES)
        except OSError as exc:
            notes.append(f'the record of run {name} could not be read ({exc})')
            continue
        try:
            record = json.loads(data.decode('utf-8', errors='replace'))
        except (ValueError, RecursionError) as exc:
            notes.append(f'the record of run {name} is not readable JSON ({type(exc).__name__})')
            continue
        if not isinstance(record, dict):
            notes.append(f'the record of run {name} is not a JSON object')
            continue
        watch, trust = record.get('project_watch'), record.get('trust')
        paths = watch.get('paths') if isinstance(watch, dict) else None
        accepted = trust.get('watched_paths_accepted') if isinstance(trust, dict) else None
        records.append(dict(
            run=name, written=written[name],
            unnamed=bool(isinstance(watch, dict) and (
                watch.get('unavailable') or (isinstance(paths, list) and len(paths) >= WATCH_RECORD_LIMIT))),
            paths=[p for p in paths if isinstance(p, dict) and isinstance(p.get('path'), str)]
            if isinstance(paths, list) else [],
            accepted={a['path'] for a in accepted if isinstance(a, dict) and isinstance(a.get('path'), str)}
            if isinstance(accepted, list) else set()))
    return records, notes


def _recorded_watch_paths(records):
    """Every watched path the records name as changed, oldest record first,
    each with the ``run`` that recorded it: the union, so no one record can
    clear what another recorded.

    A path a later run's operator accepted is left out, or one acceptance
    would have to be repeated for every run that still has the old record
    in its window. Later means by name and by when the record was written:
    a record made during the run that planted the path can name itself
    anything, but it was written before that run's own record and so
    accepts nothing in it.

    A record marked ``unnamed`` adds one entry under ``_UNNAMED_PATHS``:
    its run may have changed paths the record does not hold, so no listing
    can show them gone. It stands until a later run's operator accepts it,
    like any other."""
    entries = []
    for index, old in enumerate(records):
        accepted = set()
        for later in records[index + 1:]:
            if later['written'] > old['written']:
                accepted |= later['accepted']
        entries += [dict(entry, run=old['run']) for entry in old['paths'] if entry['path'] not in accepted]
        if old.get('unnamed') and _UNNAMED_PATHS not in accepted:
            entries.append(dict(path=_UNNAMED_PATHS, what='changed', kind='', record=None,
                                run=old['run'], unnamed=True))
    return entries


def _say_carried(carried, progress):
    """``carried`` again, said once: watched paths an earlier run's operator
    accepted that are still in place. Not a refusal and not a question."""
    if carried and progress:
        from .project_watch import describe
        named = describe((c['path'] + (f" [{c['kind']}]" if c['kind'] else ''), c['what']) for c in carried)
        progress(f'Watched paths an earlier run left changed and its operator accepted are still in '
                 f"place: {named}. This run starts over them and its record says so.")
    return carried


def _carried_acceptances(project, earlier, progress):
    """The accepted paths in ``earlier`` that are still in place, when no
    other finding stands. A project that cannot be listed carries none: the
    acceptance already stands, and this is a record, never a refusal."""
    if not earlier:
        return []
    from .project_watch import ProjectWatch, WatchUnavailable, still_in_place
    try:
        listing = ProjectWatch(project.root, exclude=project.exclude).listing()
    except WatchUnavailable:
        return []
    return _say_carried(still_in_place(earlier, listing), progress)


def _accepted_watch_paths(records):
    """The watched paths the records name as changed that a later run's
    operator accepted: what ``_recorded_watch_paths`` leaves out. They are
    not asked about again, but a run that starts while one is still in
    place says so in its own record (review of the run-lifecycle fixes,
    2026-11-11: such a run recorded nothing and the truth check called it
    verified over the same planted path)."""
    entries = []
    for index, old in enumerate(records):
        accepted = set()
        for later in records[index + 1:]:
            if later['written'] > old['written']:
                accepted |= later['accepted']
        entries += [dict(entry, run=old['run']) for entry in old['paths'] if entry['path'] in accepted]
    return entries


def _refuse_standing_watch_findings(project, state, accepted=False, progress=None):
    """Refuse, before any model call, a project in which a watched path an
    earlier run recorded as changed still holds what that run recorded.

    The in-place stop ends with its run (quadratus.project_watch). Started
    again with nothing removed, the new run listed the planted git hook or
    link as its baseline, every later check passed on top of it, and no
    diff or review copy ever showed it. So the finding is carried in the
    run's record and asked about here, from every recent record and not
    only the newest (``_recent_run_records``). The operator removes or
    restores the path, or accepts it, which later runs then read from the
    accepting run's record and do not ask about again. A record that cannot
    be read is said and is not a refusal: it proves nothing either way.
    """
    from .project_watch import (
        ACCEPT_WATCHED_PATHS_ENV,
        ProjectWatch,
        WatchUnavailable,
        describe,
        still_in_place,
        watched_paths_accepted,
    )
    try:
        records, notes = _recent_run_records(state)
    except _TooManyRunFolders as exc:
        # Fail closed: which of them is the last run's record is not known.
        if watched_paths_accepted(accepted):
            if progress:
                progress(f'Watched paths: {exc}, so the records of earlier runs were not read. Accepted '
                         'by the operator for this run; the next run asks again while they are there.')
            return []
        raise ValueError(
            f'{exc}, more than a preflight reads, so whether an earlier run left a watched path '
            'changed is not known. Nothing was run and no model was called. Remove the folders there '
            'that are not run records you need (a run\'s folder holds its result.json), or start '
            f'anyway with --accept-watched-paths or {ACCEPT_WATCHED_PATHS_ENV}=1.') from exc
    for note in notes[:_NEWEST_RUNS_TRIED] if progress else ():
        progress(f'Watched paths: {note}, so whether that run left a watched path changed is '
                 'not known from it.')
    paths = _recorded_watch_paths(records)
    earlier = _accepted_watch_paths(records)
    if not paths:
        return _carried_acceptances(project, earlier, progress)
    # Said as it works: an acceptance is written to the accepting run's
    # record and later runs read it there, so they do not ask again about the
    # same paths (final review of the 1110 batch, run_and_surfaces 3).
    way_out = ('Inspect each one, then remove or restore it, or accept them as they are '
               'with --accept-watched-paths (accept_watched_paths=True for run_project or run_baseline) '
               f'or {ACCEPT_WATCHED_PATHS_ENV}=1. An acceptance is written to that run\'s record, and '
               'later runs do not ask about the same paths again.')
    try:
        listing = ProjectWatch(project.root, exclude=project.exclude).listing()
    except WatchUnavailable as exc:
        if watched_paths_accepted(accepted):
            return []
        named = ', '.join(sorted({entry['run'] for entry in paths})[-3:])
        raise ValueError(
            f'Run {named} recorded changes to watched paths in this project, and the project could '
            f'not be listed to see whether they are still there ({exc}). Nothing was run and no model '
            f'was called. {way_out}') from exc
    standing = still_in_place(paths, listing)
    # A record that may not name every path its run changed: nothing in the
    # listing can show those gone, so it stands until it is accepted.
    unnamed = [entry for entry in paths if entry.get('unnamed')][-1:]
    standing += [dict(path=_UNNAMED_PATHS, what='not checked', kind='', by=entry['run'], since=False)
                 for entry in unnamed]
    carried = [entry for entry in still_in_place(earlier, listing)
               if entry['path'] not in {s['path'] for s in standing}]
    if not standing:
        return _say_carried(carried, progress)
    # The latest record of a path is the one that was compared.
    recorded_by = {entry['path']: entry['run'] for entry in paths}
    by = sorted({recorded_by[s['path']] for s in standing})
    named = describe((f"{s['path']}" + (f" [{s['kind']}]" if s['kind'] else ''),
                      s['what'] + (', and written to since' if s.get('since') else '')) for s in standing)
    if watched_paths_accepted(accepted):
        if progress:
            # Not for this run alone: an acceptance lasts (the integrator's ruling).
            progress(f"Watched paths run {', '.join(by[-3:])} left changed, accepted by the operator "
                     f"for this run and the ones after it: {named}. The acceptance is written to this "
                     "run's record, and later runs do not ask about the same paths again.")
        return standing + _say_carried(carried, progress)
    last = max(record['run'] for record in records)
    who = (f'The last run on this project ({last})' if by == [last] else
           f"Earlier runs on this project ({', '.join(by[-3:])}{' and more' if len(by) > 3 else ''})")
    more = ''
    if unnamed:
        from .project_watch import WATCH_RECORD_LIMIT
        more = (f" Run {unnamed[0]['run']} changed more watched paths than its record holds "
                f'({WATCH_RECORD_LIMIT}), so some are named nowhere: before you accept, look through the '
                'project for links, git hooks, env files and files in tool caches that you did not put there.')
    raise ValueError(
        f'{who} recorded that an editing call changed paths outside '
        f'the measured source that no task may change, and the change is still in place: {named}.{more} '
        'No measured diff or review copy shows them, so a new run would take them as its starting '
        f'point and every check would run on top of them. Nothing was run and no model was called. {way_out}')


def _watch_record(fleet):
    """The run's project-watch findings for ``result.json``: each finding as
    the Fleet kept it, and for every changed path what it is (``kind``), what
    happened (``what``), what it holds now (``record``) and what it was
    before the run touched it (``before``), which is what the next run's
    preflight compares. Never raises."""
    try:
        findings = [dict(f) for f in (getattr(fleet, 'watch_findings', None) or []) if isinstance(f, dict)]
        records = dict(getattr(fleet, 'watch_records', None) or {})
        paths = [dict(path=str(path), kind=str(r.get('kind') or ''), what=str(r.get('what') or ''),
                      record=list(r['record']) if r.get('record') is not None else None,
                      before=list(r['before']) if r.get('before') is not None else None,
                      by=str(r.get('by') or ''))
                 for path, r in sorted(records.items()) if isinstance(r, dict)]
        json.dumps([findings, paths])
    except Exception:  # noqa: BLE001 -- a record never fails a run
        return dict(findings=[], paths=[], unavailable=True)
    return dict(findings=findings, paths=paths)


def _carry_watch(watch, parent_record):
    """``watch`` with the watched-path findings an earlier episode of the
    same job recorded in front of this episode's: the final record of a
    resumed job says what the whole job changed outside the measured source,
    as the same job run in one episode would. A later record of a path
    stands over an earlier one. Never raises."""
    earlier = parent_record.get('project_watch') if isinstance(parent_record, dict) else None
    if not isinstance(earlier, dict):
        return watch
    try:
        named = {entry['path'] for entry in watch['paths']}
        paths = [dict(e) for e in earlier.get('paths') or []
                 if isinstance(e, dict) and isinstance(e.get('path'), str) and e['path'] not in named]
        findings = [dict(f) for f in earlier.get('findings') or [] if isinstance(f, dict)]
        merged = dict(watch, findings=findings + watch['findings'], paths=paths + watch['paths'])
        if earlier.get('unavailable'):
            merged['unavailable'] = True
        json.dumps(merged)
        return merged
    except Exception:  # noqa: BLE001 -- a record never fails a run
        return dict(watch, unavailable=True)


def _watch_report_lines(watch):
    """The report's lines for a run's watched-path changes (``_watch_record``),
    or []. One wording for a project run and a baseline."""
    if watch['paths']:
        from .project_watch import ACCEPT_WATCHED_PATHS_ENV, describe
        named = describe(((w['path'] + (f" [{w['kind']}]" if w['kind'] else '')), w['what']) for w in watch['paths'])
        return [f"Watched paths changed: an editing call of this run changed {len(watch['paths'])} "
                f'path(s) outside the measured source that no task may change: {named}. Nothing was '
                'reverted. The next run on this project is refused while a path still holds that '
                'content: remove or restore it, or accept it with --accept-watched-paths '
                f'({ACCEPT_WATCHED_PATHS_ENV}=1). result.json lists each under project_watch.', '']
    if watch['findings']:
        return [f"Watched paths changed: {len(watch['findings'])} editing call(s) changed paths outside "
                "the measured source in a parallel task's own copy of the project. That copy is "
                'deleted and none of those paths was merged. result.json lists each under '
                'project_watch.', '']
    return []


def _accepted_report_lines(accepted):
    """The report's lines for watched paths the run started over (accepted by
    its operator now or in an earlier run), or []. One wording for a project
    run and a baseline."""
    accepted = [a for a in accepted or () if isinstance(a, dict)]
    if not accepted:
        return []
    from .project_watch import describe
    named = describe((str(a.get('path')) + (f" [{a.get('kind')}]" if a.get('kind') else ''),
                      str(a.get('what') or '')) for a in accepted)
    return [f'Watched paths accepted: this run started over {len(accepted)} path(s) an earlier run '
            'changed outside the measured source, which the operator accepted: '
            f'{named}. No diff or check of this run covers what they hold.', '']


#: A stored artifact's name: the text, its first reference, or one more.
_ARTIFACT_NAME = re.compile(r'([0-9a-f]{12})(?:\.txt|\.json|\.[0-9a-f]{12}\.json)')
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


def _copy_artifacts(source, target):
    """Copy a parent run's stored artifacts into ``target`` so the record's
    references resolve in the new episode. Returns ``(copied, skipped)``.

    Never through a link, the way the store itself reads: the folder is
    opened without following one, and only regular files named as the store
    names them are read (non-blocking, so a FIFO cannot stall the resume).
    A text whose bytes do not hash to its id is not the artifact the record
    names. Everything else is skipped and counted, never copied and never an
    error: ``shutil.copytree`` followed a planted link and stored an outside
    file where any seat could FETCH it, and raised on a dangling one after
    the parent was already marked resumed."""
    import stat as _stat

    from .safe_io import create_in
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    directory = getattr(os, 'O_DIRECTORY', 0)
    try:
        src = os.open(source, flags | directory)
    except FileNotFoundError:
        return 0, 0
    except OSError:
        return 0, 1     # a link or a file in the folder's place: nothing of it is taken
    copied = skipped = 0
    try:
        dst = os.open(target, flags | directory)
        try:
            for name in sorted(os.listdir(src)):
                match = _ARTIFACT_NAME.fullmatch(name)
                data = None
                if match:
                    try:
                        fd = os.open(name, flags | getattr(os, 'O_NONBLOCK', 0), dir_fd=src)
                    except OSError:
                        fd = None
                    if fd is not None:
                        try:
                            info = os.fstat(fd)
                            if _stat.S_ISREG(info.st_mode) and info.st_size <= _MAX_ARTIFACT_BYTES:
                                chunks = []
                                while True:
                                    chunk = os.read(fd, 1 << 20)
                                    if not chunk:
                                        break
                                    chunks.append(chunk)
                                data = b''.join(chunks)
                        except OSError:
                            data = None
                        finally:
                            os.close(fd)
                if data is not None and name.endswith('.txt') and (
                        hashlib.sha256(data).hexdigest()[:12] != match.group(1)):
                    data = None
                if data is None:
                    skipped += 1
                    continue
                create_in(dst, name, data)
                copied += 1
        finally:
            os.close(dst)
    finally:
        os.close(src)
    return copied, skipped


def _parent_record(parent):
    """The parent run's own ``result.json``, after holding its committed
    state to it, or CheckpointRefused.

    The run folder is inside the project, so an editing call can rewrite the
    manifest, the records and the checkpoint, and every digest in them is a
    digest of the file itself. What it cannot rewrite unseen is the record
    the run wrote after its last call: the watch lists ``result.json``, and
    it names the manifest the run's own store committed, from memory. A
    parent whose manifest is not that one, or that left no such record (a
    process that was killed, an older engine), is not resumed: nothing says
    its checkpoint is the one the harness wrote."""
    from . import checkpoint, jobstore
    try:
        record = json.loads(_read_bounded(parent / 'result.json', _MAX_OLD_RECORD_BYTES)
                            .decode('utf-8', errors='replace'))
    except (OSError, ValueError, RecursionError) as exc:
        raise checkpoint.CheckpointRefused(
            f'{parent.name} left no readable record of its own (result.json: {type(exc).__name__}), so '
            'nothing outside its checkpoint says which state the run committed; it is not resumed') from exc
    held = record.get('committed_manifests') if isinstance(record, dict) else None
    if not isinstance(held, list) or not held:
        raise checkpoint.CheckpointRefused(
            f"{parent.name}'s record names no committed state (it was written by an older engine, or the "
            'run committed nothing); the checkpoint cannot be held to it and is not resumed')
    try:
        manifest = jobstore.read_manifest(parent)
    except jobstore.GenerationCorrupt as exc:
        raise checkpoint.CheckpointRefused(f"the parent's committed state is corrupt: {exc}") from exc
    if manifest is None or manifest.digest not in held:
        raise checkpoint.CheckpointRefused(
            f"the committed state in {parent.name} is not the one the run's own record names; its manifest "
            'or records were written after the run ended, and the run is not resumed')
    return record


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


def _test_changes(root, raw, *, goal, forbid=(), originals=None):
    """Load and validate the operator's exact, goal-bound test migrations."""
    from .integration import test_change_authorizations
    if isinstance(raw, (str, Path)):
        path = Path(raw).expanduser()
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError(f'Test changes file {path} could not be read as JSON: {exc}') from exc
    return test_change_authorizations(raw, goal=goal, root=root, forbid=forbid, originals=originals)


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
            or identity[:2] in (('npm', 'test'), ('npm', 't'), ('cargo', 'test')))


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
                          minimum_tests=1 if _is_test_suite(c) else None)
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


def _state_folder(project, state_dir):
    """Where a run's records go, resolved against the project and left out of
    its measured source. Never a folder that contains the source."""
    state = Path(state_dir).expanduser() if state_dir else Path('.quadratus')
    if not state.is_absolute():
        state = project.root / state
    state = state.resolve()
    if state == project.root or project.root.is_relative_to(state):
        raise ValueError('Run state must not contain the project source folder.')
    project.exclude.add(state)
    return state


def _restore_run_folder(run_dir) -> bool:
    """Make the run's own record folder again when it is gone. True when it was.

    An editing call can remove it. In the reproduced case (review of the
    trust-boundary fixes, 2026-11-11) the same call planted a git hook, the
    watch saw the hook, writing the record then raised, no result.json
    existed, and the next run started over the hook. The folder is made
    again so the record always lands, and the caller makes the loss the
    run's stop. A link left in the folder's place is removed first: the
    name is the harness's own, and a record is never written through one.
    """
    run_dir = Path(run_dir)
    if run_dir.is_dir() and not run_dir.is_symlink():
        return False
    if run_dir.is_symlink():
        run_dir.unlink()
    prepare_state_dir(run_dir.parent.parent)
    make_private_dir(run_dir.parent)
    make_private_dir(run_dir)
    return True


def _new_run_dir(state):
    """A fresh, owner-only ``runs/<stamp>-<id>`` folder under ``state``."""
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run_dir = state / 'runs' / f'{stamp}-{uuid.uuid4().hex[:8]}'
    # Closed to other users before the first record is written, not when the
    # run ends: the goal, the diff and check output were readable by anyone
    # on the machine for the whole run, and for good if it died early.
    prepare_state_dir(state)
    make_private_dir(state / 'runs')
    os.mkdir(run_dir, 0o700)
    return run_dir


#: The id of the one ``gate-plan.json`` entry that is not a command: the
#: operator's choice to decline the project's declared checks.
DECLINED_CHECKS_ID = 'declared-checks'


def _decline_declared_checks(scan):
    """``(scan, declined)`` for a run whose operator declined the project's
    declared checks: the scan without them, and the commands that were left
    out, so the record can say what was declined and not only that nothing
    was detected."""
    from .policy import without_declared_checks
    declined = []
    for command in ([scan.check_command] if scan.check_command else []) + list(scan.declared_checks or []):
        if list(command) not in declined:
            declined.append(list(command))
    return dataclasses.replace(without_declared_checks(scan), interpreter_note=''), declined


def _declared_checks_record(declined):
    """The operator's choice about the project's declared checks, for
    ``gate-plan.json`` and ``result.json``. ``declined`` is None when they
    were used as declared, else the commands that were left out. With the
    choice unrecorded, a run that declined them and gave no check of its own
    left an empty gate plan and a policy plan saying no test command was
    detected."""
    if declined is None:
        return dict(declined=False)
    return dict(declined=True, by='operator', commands=[list(c) for c in declined],
                note="The operator declined the project's declared checks (--no-declared-checks, "
                     'declared_checks=False). None of them ran as a check of this run: only the '
                     "operator's own checks did"
                     + ('.' if declined else ', and the project declared none that was read.'))


def _trust_record(policy, agent_config, declared, watched_accepted=()):
    """What the operator let a run take on trust, for ``result.json``: the
    project's own policy file and whether it was approved, the vendor agent
    configuration files allowed at the start, whether the project's declared
    checks were declined, and the watched paths the last run left changed
    that were accepted for this one. Each was a progress line or a refusal
    and nothing a reader of the record could find afterwards."""
    return dict(
        project_policy=dict(in_project=bool(getattr(policy, 'explicit', False)),
                            approved=bool(getattr(policy, 'approved', False)),
                            sha256=str(getattr(policy, 'source_sha256', '') or '')),
        agent_config_allowed=sorted(str(name) for name in (agent_config or {})),
        declared_checks_declined=bool((declared or {}).get('declined')),
        watched_paths_accepted=[dict(path=str(s.get('path')), kind=str(s.get('kind') or ''),
                                     what=str(s.get('what') or ''))
                                for s in watched_accepted or () if isinstance(s, dict)])


def _child_environment_record():
    """The environment variables no check, preview, probe or vendor CLI of
    this run was given, by name and never by value (quadratus.child_env), so
    a skipped credential-gated test or a failed check can be explained from
    the record. Never raises."""
    try:
        from .child_env import PASS_VARIABLE, withheld_names
        return dict(withheld=[str(name) for name in withheld_names()], keep_with=PASS_VARIABLE)
    except Exception:  # noqa: BLE001 -- a record never fails a run
        return dict(withheld=[], unavailable=True)


def _declared_checks_lines(record):
    """The report's line for declined declared checks, or []."""
    if not record.get('declined'):
        return []
    return [f"Declared checks: declined by the operator (--no-declared-checks). "
            f"{len(record.get('commands') or [])} command(s) the project declares did not run as a "
            "check of this run; result.json lists them under declared_checks.", '']


def _gate_for(project, check, extras, gates, scan, run_dir, progress=None, declined=None):
    """The run's gate and its plan: ``(gate or None, plan)``. The plan is
    written to ``gate-plan.json`` and told to the operator here.
    ``declined`` is the commands the operator declined with the project's
    declared checks (None when they were not declined); the file then ends
    with one entry that says so, which is not a command and is not in the
    plan returned."""
    command = shlex.split(check) if check else scan.check_command
    gates = _merge_extras(list(gates), extras) if gates is not None else _gate_plan(command, extras, scan)
    plan = [dict(id=g.id, argv=list(g.argv), cwd=g.cwd, required=g.required, minimum_tests=g.minimum_tests)
            for g in gates or ()] or (
        [dict(id='check', argv=list(command), cwd='.', required=True,
              minimum_tests=1 if _is_test_suite(command) else None)] if command else [])
    # The declared Python check says so when the project's own interpreter
    # was passed over for the harness's (repo_scan._project_interpreter).
    fallback = [g for g in plan if scan.interpreter_note and g['argv'] in scan.declared_checks
                and g['argv'][1:3] == ['-m', 'pytest']]
    for g in fallback:
        g['note'] = scan.interpreter_note
    # Shown to the operator before any task, and kept with the run. Never put
    # into a model prompt: a gate command can name an examiner path.
    choice = _declared_checks_record(declined)
    written = plan + ([dict(id=DECLINED_CHECKS_ID, argv=[], cwd='.', required=False, minimum_tests=None,
                            **choice)] if choice['declined'] else [])
    write_private(run_dir / 'gate-plan.json', json.dumps(written, indent=2))
    if progress:
        progress('Checks: ' + ('; '.join(f"{g['id']}: {' '.join(g['argv'])}" for g in plan) or 'none'))
        if choice['declined']:
            progress(f"The project's declared checks were declined by the operator: {len(declined)} "
                     'declared command(s) not run.')
        if fallback:
            progress(scan.interpreter_note)
    gate = (GateSuite(gates, cwd=project.root, exclude=project.exclude) if gates is not None
            else IntegrationGate(command, cwd=project.root,
                                 minimum_tests=1 if _is_test_suite(command) else None) if command else None)
    return gate, plan


def _lead_cap(settings, run_limits, progress=None):
    """``(settings, lead_turns, lead_source)`` with the lead turn cap applied."""
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
    return settings, lead_turns, lead_source


def _fleet_for(fleet_type, settings, *, project, allow_writes, meter, delegation, budget, progress,
               plan, agent_config):
    """The run's Fleet, told its progress line, the checks it may run and the
    vendor agent configuration the operator allowed."""
    fleet = fleet_type(settings, project=project, allow_writes=allow_writes,
                       usage_meter=meter, delegation_ledger=delegation,
                       **({'run_budget': budget} if budget else {}))
    try:
        fleet.progress = progress  # one live line per call as it ends
        # The checks are the commands a granted editing call may run
        # unapproved and the denials that count as a capability failure.
        from .integration import model_facing
        # What a seat is told to run: the harness's report declaration is
        # its own and never reaches a model's argv (series rule-58a4625 f3).
        fleet.check_commands = tuple(shlex.join(model_facing(g['argv'])) for g in plan if g.get('argv'))
        # The vendor agent configuration the operator saw and allowed at the
        # preflight, if any. An editing call refuses anything else.
        fleet.agent_config_allowed = dict(agent_config or {})
    except Exception:  # noqa: BLE001 -- a fake fleet may refuse attributes
        pass
    return fleet


def _collect_traces(run_dir, project, delegation, progress=None):
    """``(traces, interrupted)``: what each call did inside its own session,
    with the native children those transcripts name put on the record."""
    traces, trace_interrupted = [], False
    try:
        from .trace import build_traces
        traces = build_traces(run_dir, run_dir / 'invocations.jsonl', project.root)
    except KeyboardInterrupt:
        # A second Ctrl-C lands here, in the slow copy, more often than
        # anywhere else. It ends the copy and nothing more: the run's own
        # record below is quick, and without it the run left no result.
        traces, trace_interrupted = [], True
        if progress:
            progress('Interrupted while copying call transcripts; writing the run record without them.')
    except Exception:  # noqa: BLE001 -- tracing never fails a run
        traces = []
    _observe_transcript_children(traces, delegation)
    return traces, trace_interrupted


def _run(goal, project, settings, *, state, allow_writes, check, max_tasks,
         session_options=None,
         mode, progress, ask_operator, plan_gate, fleet_type, session_factory,
         default_scope=None, run_limits=None, policy=None, gates=None, security_verdict_json=False,
         extras=(), capture_profile=None, readiness=(), survey=None, direct_tier=False, tasks=None,
         decider=None, decider_labels="unstated", repair_tests=(), test_changes=(),
         declared_checks=True, agent_config=None, compete="off", watched_accepted=(), resume_from=None):
    parent = _resume_parent(state, resume_from) if resume_from is not None else None
    run_dir = _new_run_dir(state)
    from .run_activity import RunActivity
    activity = RunActivity(run_dir, progress)
    progress = activity
    activity.record('run-started', goal=str(goal)[:4000])
    stale_fixtures = _retire_stale_fixtures(project.root, run_dir, progress)
    before = project.contents()
    scan = scan_repo(project.root)
    # A damaged map costs the damaged lines, never the run: this load is
    # outside the run's own error handling.
    code_map = CodebaseMap(state / 'codebase-map.jsonl')
    map_notices = _report_map(code_map, progress, 0)
    seed_map(scan, code_map)
    declined = None
    if not declared_checks:
        # The operator's own checks only: nothing the project declares is
        # added beside them or run in their place. The map above still says
        # what the project declares; that is a fact about it, not a gate.
        scan, declined = _decline_declared_checks(scan)
    gate, plan = _gate_for(project, check, extras, gates, scan, run_dir, progress, declined=declined)
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
                    plan_gate=plan_gate is not None, ask_operator=ask_operator is not None,
                    **({'test_changes': list(test_changes)} if test_changes else {}),
                    # Only when set, so a run without them keeps its digest:
                    # a resume under other contest or session settings is
                    # another job.
                    **({'compete': compete} if compete != 'off' else {}),
                    **({'session_options': dict(sorted(session_options.items()))} if session_options else {})))
    lineage = dict(root=run_dir.name, parent=None, episode=1)
    carried = None
    parent_record, carried_diff = None, ''
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
        parent_record = _parent_record(parent)
        prior = body.get('lineage') or {}
        lineage = dict(root=prior.get('root') or parent.name, parent=parent.name,
                       episode=int(prior.get('episode') or 1) + 1)
        # The record's artifact references resolve in the new episode. Copied
        # before the claim, so a copy that fails leaves the parent resumable.
        try:
            _, not_copied = _copy_artifacts(parent / 'artifacts', run_dir / 'artifacts')
        except OSError as exc:
            raise checkpoint.CheckpointRefused(
                f"the parent's artifacts could not be copied ({type(exc).__name__}: {exc})") from exc
        if not_copied and progress:
            progress(f"{not_copied} entry(ies) in {parent.name}'s artifacts were not copied: a link, "
                     'something that is not a regular file, a name the store does not write, or a text '
                     'that does not match its id.')
        # The job's diff, not the episode's: measured from the job's own
        # run-start source where the checkpoint carries it, else the
        # parent's diff goes in front of this episode's.
        try:
            start = checkpoint._decode((body.get('state') or {}).get('_run_start_contents'))
        except (TypeError, ValueError):
            start = None
        if isinstance(start, dict) and all(isinstance(k, str) and isinstance(v, bytes) for k, v in start.items()):
            before = start
        else:
            try:
                carried_diff = _read_bounded(parent / 'changes.diff', _MAX_OLD_RECORD_BYTES).decode(
                    'utf-8', errors='replace')
            except OSError:
                carried_diff = ("The earlier episode's changes could not be read "
                                f'({parent.name}/changes.diff).\n')
        checkpoint.claim(parent, run_dir, lineage['episode'])
        if progress:
            progress(f"Resuming {parent.name} as episode {lineage['episode']} of {lineage['root']}; "
                     f"carried {carried['calls']} call(s), "
                     f"{carried['input_tokens'] + carried['output_tokens']:,} reported tokens")
    write_private(run_dir / 'lineage.json', json.dumps(lineage, indent=2) + '\n')
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
    if decider is not None and progress:
        # Said before any task text is sent, and kept in the activity record.
        from .decisions import disclosure
        progress(disclosure(decider))
    settings, lead_turns, lead_source = _lead_cap(settings, run_limits, progress)
    # The allowance this run was given, saved before any model call so an
    # interrupted run still says what it was allowed (Codex GUI plan, #35),
    # with the lead turn cap the session is about to be built with.
    selected = _selected_limits(max_tasks, run_limits, survey, lead_turns, lead_source,
                                session_settings=session_options)
    write_private(run_dir / 'run-limits.json', json.dumps(selected, indent=2) + '\n')
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
        repair_tests=tuple(repair_tests), test_changes=tuple(test_changes or ()),
        compete=compete,
        job_store=job_store,
    )
    # Session settings the caller chose (session_knobs); none is the old run.
    for name, value in (session_options or {}).items():
        setattr(config, name, value)
    if session_options and progress:
        progress('Session settings: ' + ', '.join(f'{k}={v}' for k, v in sorted(session_options.items())))

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
    write_private(run_dir / 'policy-plan.json', json.dumps(preview, indent=2))
    session, error = None, ''
    if run_limits:
        config.worker_budget = WorkerBudget(
            # This permissive per-task ceiling cannot widen the shared run cap:
            # every worker transport attempt also reserves from RunBudget.
            max_per_task=run_limits.max_calls,
            max_concurrent=run_limits.max_concurrent_workers,
        )
    fleet = _fleet_for(fleet_type, settings, project=project, allow_writes=allow_writes, meter=meter,
                       delegation=delegation, budget=budget, progress=progress, plan=plan,
                       agent_config=agent_config)
    if parent is not None and hasattr(fleet, 'freeze'):
        # Before any seat or availability decision: the resumed episode
        # addresses the models the job already resolved; the current
        # catalogue is recorded beside them, never followed mid-job.
        frozen = ((body.get('state') or {}).get('model_resolution') or {}).get('resolutions')
        if frozen:
            fleet.freeze(frozen)
    in_flight, folder_lost = {}, False
    interrupted = False
    scored = (0, 0)
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
            # The parent already put these on the scoreboard.
            scored = (len(getattr(session, 'contests', None) or []),
                      len(getattr(session, 'reviewer_records', None) or []))
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
        folder_lost = _restore_run_folder(run_dir)
        if in_flight:
            _publish_in_flight(job_store, run_dir, in_flight)
    finally:
        fleet.close()
    # Before anything below is written. The record carries a watch finding
    # to the next run's preflight, so it must land whatever a call did to
    # the folder it lands in.
    folder_lost = _restore_run_folder(run_dir) or folder_lost
    if folder_lost:
        lost = ('RunRecordLost: this run\'s own record folder was removed while the run was going. '
                'Its artifacts and call records are gone, so nothing in this record can be checked '
                'against them.')
        error = f'{error} {lost}' if error else lost
        if progress:
            progress(lost)
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
    traces, trace_interrupted = _collect_traces(run_dir, project, delegation, progress)
    diff = carried_diff + project.diff(before)
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
    watch = _carry_watch(_watch_record(fleet), parent_record)
    lines += _watch_report_lines(watch)
    lines += _accepted_report_lines(watched_accepted)
    declared = _declared_checks_record(declined)
    lines += _declared_checks_lines(declared)
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
    for decision in (getattr(session, 'test_change_decisions', None) or []) if session else []:
        lines += [f"Test change {decision['status']}: {decision['path']} "
                  f"(operator decision {decision['id']}; original {decision['original_sha256'][:12]}, "
                  f"delivered {decision['current_sha256'][:12]}). "
                  "The original bytes remain recorded; approval covers this goal and these test bytes.", '']
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
    # What the map could not load, save or keep in this run, for the
    # operator. Never shown to a model.
    _report_map(code_map, progress, map_notices)
    map_record = code_map.record()
    if map_record['notices'] or map_record['not_kept']:
        lines += ['## Codebase map', '', *[f'- {notice}' for notice in map_record['notices']],
                  *([f"- {map_record['not_kept']} note(s) a model wrote were not kept; "
                     'result.json lists each with the reason.'] if map_record['not_kept'] else []), '']
    lines += [delegation.render_report(), '']
    recovery_section = _improvement_section(session)
    if recovery_section:
        lines += [recovery_section]
    lines += ['## Task ledger', '', ledger]
    report = '\n'.join(lines)
    contests = list(getattr(session, 'contests', []) or []) if session else []
    reviewers = list(getattr(session, 'reviewer_records', []) or []) if session else []
    # Each file is written whole under a temporary name and renamed in, so a
    # reader (the monitor refreshes every few seconds) never sees half of
    # one and an interrupt leaves the old file or the new, never a fragment.
    write_private(run_dir / 'changes.diff', diff)
    write_private(run_dir / 'ledger.md', ledger)
    write_private(run_dir / 'report.md', report)
    _record_models(session, fleet)
    write_private(run_dir / 'result.json', json.dumps({
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
        # DONEs whose requirements the record already held not met before an
        # audit, and the cap's goal question when it was not asked because the
        # record made completion impossible (cycle02).
        'done_gates': list(getattr(session, 'done_gates', []) or []) if session else [],
        'goal_question_skipped': getattr(session, 'goal_question_skipped', None) if session else None,
        'planning_refusals': list(getattr(session, 'planning_refusals', []) or []) if session else [],
        'scope_declarations': list(getattr(session, 'scope_declarations', []) or []) if session else [],
        'scope_gate_skips': list(getattr(session, 'scope_gate_skips', []) or []) if session else [],
        'scope_sendbacks': list(getattr(session, 'scope_sendbacks', []) or []) if session else [],
        'done_declarations': list(getattr(session, 'done_declarations', []) or []) if session else [],
        'receipt_coverage': list(getattr(session, 'receipt_coverage', []) or []) if session else [],
        'resolves_normalized': list(getattr(session, 'resolves_normalized', []) or []) if session else [],
        'inherited_not_run': list(getattr(session, 'inherited_not_run', []) or []) if session else [],
        'boundary_notes': list(getattr(session, 'boundary_notes', []) or []) if session else [],
        'restatement_judgments': list(getattr(session, 'restatement_judgments', []) or []) if session else [],
        'packet_refusals': list(getattr(session, 'packet_refusals', []) or []) if session else [],
        'packet_extensions': dict(
            requests=list(getattr(session, 'packet_requests', []) or []),
            rejected_decisions=list(getattr(session, 'packet_decisions_rejected', []) or []),
        ) if session else dict(requests=[], rejected_decisions=[]),
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
        # Two blind drafts of one task and what decided between them, and
        # what each review seat raised (quadratus.contest). Records only.
        'contests': contests,
        'reviewers': reviewers,
        'trace': {'calls': len(traces),
                  **({'interrupted': True} if trace_interrupted else {}),
                  'transcripts_found': sum(1 for t in traces if t.get('tool_calls') is not None),
                  'calls_outside_project': sum(1 for t in traces if t.get('outside_project')),
                  'unserved_requests': sum(len(t.get('protocol_attempts') or []) for t in traces),
                  'injected_rules': sorted({r['source'] for t in traces for r in t.get('injected_rules') or []})},
        'decisions': list(getattr(session, 'decisions', []) or []) if session else [],
        'in_flight': in_flight,
        'policy_preview': preview,
        # The operator's choice about the project's own declared checks, and
        # the commands left out when they were declined.
        'declared_checks': declared,
        # What the operator let this run take on trust, in one place.
        'trust': _trust_record(policy, agent_config, declared, watched_accepted),
        'child_environment': _child_environment_record(),
        'policy_plans': getattr(session, 'policy_plans', []),
        **_budget_fields(budget),
        'selected_limits': selected,
        # The chosen session settings again, at the top level where a reader
        # of the record looks for how the run was set up; absent when none.
        **({'session_settings': selected['session_settings']} if 'session_settings' in selected else {}),
        'unnamed_test_files': list(getattr(session, 'unnamed_test_files', []) or []) if session else [],
        'original_test_runs': list(getattr(session, 'original_test_runs', []) or []) if session else [],
        # Harness runs of the task gate seats were shown; never task checks.
        'review_checks': list(getattr(session, 'review_checks', []) or []) if session else [],
        'test_repairs': list(getattr(session, 'test_repairs', []) or []) if session else [],
        'test_change_decisions': list(getattr(session, 'test_change_decisions', []) or []) if session else [],
        'test_change_authorizations': list(getattr(getattr(session, 'config', None), 'test_changes', ()) or ()),
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
        'codebase_map': map_record,
        # Changes to watched paths (quadratus.project_watch), with what each
        # path holds now: the next run's preflight reads this.
        'project_watch': watch,
        # By task: names its editing calls changed in build output and the
        # like, which no diff reads. Told to review seats and the audit.
        'unmeasured_changes': dict(getattr(session, 'unmeasured_changes', {}) or {}) if session else {},
        'lineage': lineage,
        'resumable': interrupted,
        # The manifest this run's store committed, from the store's memory
        # and never read back from disk. A resume holds the run folder to it
        # (_parent_record): this record is written after the last call and
        # is a watched path, the manifest beside it is neither.
        'committed_manifests': job_store.committed_digests(),
        'delegation': reconcile(delegation.events, delegation.native_children.values()),
        'scope_reports': [
            {'within_scope': r.within_scope, 'out_of_scope': r.out_of_scope,
             'changed': r.changed, 'changed_lines': r.changed_lines, 'max_lines': r.max_lines,
             'code_lines': r.code_lines, 'test_lines': r.test_lines, 'oversized': r.oversized}
            for r in (session.scope_reports if session else [])
        ],
    }, indent=2))
    write_private(run_dir / 'delegation.md', delegation.render_report())
    _append_scoreboard(state, run_dir.name, contests[scored[0]:], reviewers[scored[1]:], progress)
    write_private(run_dir / 'findings.json',
                  json.dumps(list(getattr(session, 'findings', []) or []) if session else [], indent=2))
    # A record another module appended under the usual mode is closed here.
    # The folder has been owner-only since it was made, so this is the file's
    # own mode catching up, not the first protection.
    tighten_files(run_dir)
    result = ProjectResult(completed, report, run_dir, diff, error)
    if trace_interrupted:
        # The record above is whole. The interrupt is the operator's, so it
        # goes on to whoever called: a series driver that loops over runs
        # must stop here, and an absorbed Ctrl-C started its next run. It
        # carries the result, so a caller can say where the record is.
        raise RunInterrupted(result)
    return result


def _append_scoreboard(state, run_id, contests, reviewers, progress=None):
    """Add this run's contests and reviews to the project's scoreboard, one
    line each. Append-only and owner-only, and a record nothing in the engine
    reads back (quadratus.contest). A scoreboard that cannot be written costs
    the lines, never the run: result.json already holds the same records."""
    from .contest import append_scoreboard, scoreboard_rows
    try:
        append_scoreboard(state / 'scoreboard.jsonl', scoreboard_rows(run_id, contests, reviewers))
    except (OSError, TypeError, ValueError) as exc:
        if progress:
            progress(f'Scoreboard not updated ({type(exc).__name__}); this run\'s records are in result.json.')


def _report_map(code_map, progress, shown):
    """Tell the operator each map notice once; returns how many are told."""
    notices = list(getattr(code_map, 'notices', ()) or ())
    for notice in notices[shown:]:
        if progress:
            progress('Codebase map: ' + notice)
    return len(notices)


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


def _selected_limits(max_tasks, run_limits, survey, lead_turns=None, lead_source=None, session_settings=None):
    """What this run was allowed and how it was set up, for run-limits.json.

    ``session_settings`` are the session settings the caller chose
    (session_knobs). Only those that differ from SessionConfig's own default
    are recorded, under ``session_settings``, and the key is absent when none
    does: the record of a run that chose nothing is the record it always had.
    Before this they were only a progress line, so a run made with
    ``design_cross_check`` off said so nowhere in its own record.
    """
    from .run_budget import SUMMARY_CALL_RESERVE_TOKENS, describe_limits
    reserve = run_limits.reserve_tokens_per_call if run_limits is not None else 0
    # The class itself, not this module's name for it: an embedding caller or
    # a test may put a wrapper under that name, and a wrapper has no fields.
    from .session import SessionConfig as config_type
    defaults = {f.name: f.default for f in dataclasses.fields(config_type)}
    changed = {name: value for name, value in sorted((session_settings or {}).items())
               if name not in defaults or defaults[name] != value}
    return {
        'max_tasks': max_tasks,
        'run_limits': dataclasses.asdict(run_limits) if run_limits is not None else None,
        'summary_call_reserve_tokens': min(reserve, SUMMARY_CALL_RESERVE_TOKENS) if reserve else None,
        'lead_max_turns': lead_turns,
        'lead_max_turns_source': lead_source,
        'survey_recovery_tasks': survey.recovery_tasks if survey is not None else None,
        'described': describe_limits(run_limits, survey, max_tasks, lead_turns, lead_source),
        **({'session_settings': changed} if changed else {}),
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

    Every pattern is compiled here, when the file is loaded. A bad one is a
    ValueError naming the entry before any model call; it used to surface as
    a regex error when the first question arrived, after calls were spent.
    """
    import json as _json
    import re as _re
    entries = _json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and isinstance(e.get("about"), str) and isinstance(e.get("answer"), str)
            for e in entries):
        raise ValueError(f"{path}: expected a list of {{'about': regex, 'answer': text}}")
    rulings = []
    for index, entry in enumerate(entries, 1):
        try:
            rulings.append((_re.compile(entry["about"], _re.IGNORECASE), entry["answer"]))
        except (_re.error, OverflowError, RecursionError) as exc:
            # The compiler has two more ways to refuse a pattern: a repeat
            # count too large for it, and nesting too deep for it.
            raise ValueError(f"{path}: ruling {index} has a pattern that is not a valid regular "
                             f"expression ({entry['about'][:200]!r}: {type(exc).__name__}: {exc})") from exc

    def ask(question: str) -> str:
        for about, answer in rulings:
            if about.search(question):
                return answer
        if fallback is not None:
            return fallback(question)
        from .session import OperatorInputNeeded
        raise OperatorInputNeeded(question)
    return ask
