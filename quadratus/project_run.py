"""The project workflow shared by the terminal and the GUI."""

from __future__ import annotations

import fcntl
import json
import shlex
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import ArtifactStore
from .codebase_map import CodebaseMap
from .integration import IntegrationGate
from .project import Project
from .providers import ProviderError
from .repo_scan import scan_repo, seed_map
from .session import SessionConfig
from .usage import UsageMeter


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
                progress=None, ask_operator=None, plan_gate=None):
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

    with _project_lock(project):
        return _run(goal, project, settings, state=state, allow_writes=allow_writes,
                    check=check, max_tasks=max_tasks, mode=mode, progress=progress,
                    ask_operator=ask_operator, plan_gate=plan_gate,
                    fleet_type=Fleet, session_factory=new_session)


def _run(goal, project, settings, *, state, allow_writes, check, max_tasks,
         mode, progress, ask_operator, plan_gate, fleet_type, session_factory):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run_dir = state / 'runs' / f'{stamp}-{uuid.uuid4().hex[:8]}'
    run_dir.mkdir(parents=True)
    before = project.contents()
    scan = scan_repo(project.root)
    code_map = CodebaseMap(state / 'codebase-map.jsonl')
    seed_map(scan, code_map)
    command = shlex.split(check) if check else scan.check_command
    gate = IntegrationGate(command, cwd=project.root) if command else None
    store = ArtifactStore(run_dir / 'artifacts')
    meter = UsageMeter(run_dir / 'usage.jsonl')
    config = SessionConfig(
        project=project.root, project_excludes=tuple(project.exclude),
        allow_writes=allow_writes, mode=mode, integration_gate=gate,
        codebase_map=code_map, ask_operator=ask_operator, plan_gate=plan_gate,
        progress=progress,
    )
    session, error = None, ''
    fleet = fleet_type(settings, project=project, allow_writes=allow_writes, usage_meter=meter)
    try:
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
    finally:
        fleet.close()
    diff = project.diff(before)
    completed = bool(session and session.completed and not error)
    checks = session.checks if session else []
    ledger = session.memory.render(current='') if session else ''
    status = 'Goal reported complete' if completed else 'Run incomplete'
    lines = [f'# {status}', '', f'Project: {project.root}', '',
             f'Edits: {"enabled" if allow_writes else "disabled"}', '',
             f'Run files: {run_dir}', '']
    if error:
        lines += [f'Error: {error}', '']
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
    lines += [meter.render_report(), '', '## Task ledger', '', ledger]
    report = '\n'.join(lines)
    (run_dir / 'changes.diff').write_text(diff, encoding='utf-8')
    (run_dir / 'ledger.md').write_text(ledger, encoding='utf-8')
    (run_dir / 'report.md').write_text(report, encoding='utf-8')
    (run_dir / 'result.json').write_text(json.dumps({
        'project': str(project.root), 'goal': goal, 'completed': completed,
        'allow_writes': allow_writes, 'error': error, 'checks': checks,
        'source_changed': bool(diff), 'source_fingerprint': project.fingerprint(),
        'tasks': len(session.history) if session else 0,
    }, indent=2), encoding='utf-8')
    return ProjectResult(completed, report, run_dir, diff, error)
