"""Gradio web interface for the multi-LLM workflow."""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from .config import Settings, env_with_legacy
from .orchestrator import Orchestrator
from .providers import ProviderError, Turn
from .run_budget import LIMIT_FIELDS, LIMIT_LABELS, limits_from_fields, whole_number

# The run-limit fields, their labels and their validation above are the
# shared ones: the form and the command line bound a run by one rule.

_BRAND_DIR = Path(__file__).resolve().parent.parent / "brand"

# Gradio 6 moved `css` off the Blocks constructor and onto launch().
INTERFACE_CSS = ".gradio-container { max-width: 980px; margin: auto; }"


def brand_asset(name: str) -> Optional[Path]:
    """Return the path to a brand asset, or None if it is not on disk.

    ``pyproject.toml`` installs only the ``quadratus`` package; ``brand/`` sits
    beside it in a checkout and is absent from a wheel. The mark is therefore
    optional decoration -- the GUI falls back to a plain heading rather than
    failing to start.
    """
    path = _BRAND_DIR / name
    return path if path.is_file() else None


def inline_mark(size: int) -> Optional[str]:
    """Return the mark as inline SVG, drawn for the band ``size`` falls in.

    The set is three separate drawings, not one that scales: below 32px the
    ribbon's ink edge goes sub-pixel and the mark inverts. Picking by size here
    is what keeps that promise -- see ``brand/README.md``.
    """
    if size >= 96:
        name = "mark-large.svg"
    elif size >= 32:
        name = "mark-medium.svg"
    else:
        name = "mark-small.svg"
    asset = brand_asset(name)
    if asset is None:
        return None
    svg = asset.read_text(encoding="utf-8")
    svg = re.sub(r'width="\d+" height="\d+"', f'width="{size}" height="{size}"', svg, count=1)
    return svg


def read_file(file_obj, settings: Settings) -> str:
    """Read an uploaded file, enforcing size and length limits."""
    if not file_obj:
        return ""
    path = file_obj if isinstance(file_obj, str) else getattr(file_obj, "name", None)
    if not path:
        return ""
    try:
        if os.path.getsize(path) > settings.max_file_bytes:
            return f"[File too large; maximum is {settings.max_file_bytes // 1024} KB.]"
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > settings.max_file_chars:
            content = content[: settings.max_file_chars] + "\n[Content truncated...]"
        return content
    except Exception as exc:
        return f"[Error reading file: {exc}]"


def _history_to_turns(history, settings: Settings) -> List[Turn]:
    """Convert Gradio 'messages' history into orchestrator turns (capped)."""
    turns: List[Turn] = []
    for msg in history or []:
        role = msg.get("role")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            turns.append(Turn(role, content))
    return turns[-settings.max_memory_turns :]


def _format_response(result) -> str:
    """Render the final answer with collapsible per-model stages."""
    parts = [result.final]
    if len(result.stages) > 1:
        details = ["\n\n<details><summary>🔍 How the models collaborated</summary>\n"]
        for stage in result.stages:
            details.append(f"\n**{stage.label} — {stage.role}**\n\n{stage.content}\n")
        details.append("\n</details>")
        parts.append("".join(details))
    parts.append(f"\n\n_Synthesised by {result.final_provider}._")
    return "".join(parts)


class OperatorChannel:
    """The planner's ASK, answered from the Project UI while the run waits.

    Before this, the UI passed no callback: an ASK raised OperatorInputNeeded
    and surfaced as a bare "Run failed" with no way to answer (Codex review of
    #25). The requirements ledger makes ASK routine -- an ambiguous
    requirement needs an operator ruling -- so the UI now shows the question
    in the progress stream and hands the typed answer back to the waiting run.
    """

    def __init__(self, timeout: float = 3600.0):
        self.questions: "queue.Queue[str]" = queue.Queue()
        self.answers: "queue.Queue[str]" = queue.Queue()
        self.timeout = timeout
        self.waiting = False
        self._answer_lock = threading.Lock()

    def ask(self, question: str) -> str:
        from .session import OperatorInputNeeded
        # Each question owns its reply queue. A submit racing a timeout can
        # then never become a ruling for a later question or run.
        answers: "queue.Queue[str]" = queue.Queue(maxsize=1)
        with self._answer_lock:
            self.answers = answers
            self.waiting = True
            self.questions.put(question)
        try:
            return answers.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise OperatorInputNeeded(question) from exc
        finally:
            with self._answer_lock:
                if self.answers is answers:
                    self.waiting = False

    def answer(self, text: str) -> str:
        text = (text or "").strip()
        with self._answer_lock:
            if not self.waiting:
                return "No question is waiting for an answer."
            if not text:
                return "Type an answer first."
            # Consume the pending question before waking its worker: double
            # clicks must not leave a second answer for the next ASK.
            self.waiting = False
            self.answers.put_nowait(text)
        return "Answer sent; the run continues."


#: Named starting points for the Run limits form. A preset only fills the
#: fields; what runs is what the fields say when the run starts, and every
#: number is visible before dispatch. "Diagnostic" is the allowance Codex's
#: bounded GUI plan selected on #35 (2026-10-09); "Small" is RunLimits'
#: own defaults. Neither changes what a run without limits does.
RUN_LIMIT_PRESETS = {
    'Diagnostic: 90 calls, 5M tokens, 1 hour': dict(
        max_calls=90, max_reported_tokens=5_000_000, wall_seconds=3600, max_concurrent_workers=2,
        reserve_tokens_per_call=250_000, max_tokens_per_call=1_500_000, recovery_tasks=4),
    'Small: 24 calls, 500k tokens, 15 minutes': dict(
        max_calls=24, max_reported_tokens=500_000, wall_seconds=900, max_concurrent_workers=2,
        reserve_tokens_per_call=0, max_tokens_per_call=0, recovery_tasks=0),
}

#: The Project form's Trust section: each input's label, which names the
#: command-line flag it stands for, and one line saying what it allows.
TRUST_LABELS = {
    'trust_project_policy': "Approve this project's policy file (sha256, --trust-project-policy)",
    'allow_project_agent_config': ('Allow vendor agent configuration in the project '
                                   '(--allow-project-agent-config)'),
    'no_declared_checks': 'Run only my own checks (--no-declared-checks)',
    'accept_watched_paths': 'Start over watched paths the last run left changed (--accept-watched-paths)',
}
TRUST_ALLOWS = {
    'trust_project_policy': ("Allows the project's own .quadratus/policy.json to grant what it asks for "
                             '(a command check, external effects, a withheld instruction file). Paste '
                             'the hash the refusal prints: it approves exactly that file.'),
    'allow_project_agent_config': ('Allows the run to start in a project that carries .claude/settings.json, '
                                   '.mcp.json, .codex/ or .grok/. A vendor CLI started there can run their '
                                   'hooks and MCP servers as you, without asking.'),
    'no_declared_checks': ('Allows a run without the checks the project itself declares: only the test '
                           'command and the further checks entered above are run.'),
    'accept_watched_paths': ('Allows the run to start although the last run recorded a change to a path '
                             'outside the measured source (a git hook, an env file, a link) that is still '
                             'in place. The refusal names each path: inspect them first.'),
}

#: The Project form's Contests choice: what the form shows and the value the
#: shared runner takes (``run_project(compete=...)``, the command line's
#: ``--compete``). The first is the default and is the run made before.
CONTEST_LABEL = 'Contests (--compete)'
CONTEST_CHOICES = (('Off', 'off'), ('Complex tasks: two blind drafts, the better one merged', 'complex'))
CONTEST_INFO = ('With complex tasks on, each task rated complex is drafted twice by models from different '
                'vendors, each in a private copy; measured facts pick the draft that is merged. It costs '
                "about one extra draft per such task and needs 'Allow changes to project files'.")


def mode_choices():
    """The Collaboration modes the form offers, and a line about any left out.

    A mode the shared runner refuses before any call is not offered: solo
    while the requirements ledger is on, by the rule the runner and the
    command line apply (``registry.mode_audit_gap``). The dropdown used to
    list it and the run then ended as "Run failed". The line says why it is
    missing and how to get it back; it is "" when every mode is offered.
    """
    from .registry import MODE_ROSTERS, mode_audit_gap
    from .session import SessionConfig
    offered, left_out = [], []
    for mode in sorted(MODE_ROSTERS):
        gap = mode_audit_gap(mode)
        if gap and SessionConfig(mode=mode).requirements_ledger:
            left_out.append(f'{mode} is not offered: {gap}')
        else:
            offered.append(mode)
    return offered, ' '.join(left_out)


def limits_from_form(enabled, *values):
    """The validated RunLimits and SurveyConfig the form describes.

    The form's values in LIMIT_FIELDS order, read by the one rule the command
    line also uses (``run_budget.limits_from_fields``); this module keeps no
    copy of it. Returns ``(None, None)`` when limits are off and no recovery
    task is asked for, which is exactly the call a run without these options
    made before they existed. Raises ValueError naming the field otherwise,
    before any project, provider or model is touched.
    """
    return limits_from_fields(enabled, dict(zip(LIMIT_FIELDS, values, strict=True)))


def task_limit(value) -> int:
    """The form's Task limit as a whole number of at least 1, or ValueError
    naming the field, by the same rule as every other limit."""
    return whole_number(value, LIMIT_LABELS['max_tasks'], 1)


def limits_summary(max_tasks, enabled, *values, operator_turns=None) -> str:
    """What the form would run with, or why it would refuse. ``operator_turns``
    is ``Settings.lead_max_turns``, which outranks a derived lead turn cap."""
    from .run_budget import describe_limits, effective_lead_turns
    try:
        limits, survey = limits_from_form(enabled, *values)
        tasks = task_limit(max_tasks)
    except ValueError as exc:
        return f'**Run limits are not valid:** {exc}'
    return '**This run will use:** ' + describe_limits(limits, survey, tasks,
                                                       *effective_lead_turns(operator_turns, limits))


def preset_values(name):
    """The field values a named preset fills in, in LIMIT_FIELDS order."""
    preset = RUN_LIMIT_PRESETS[name]
    return [True, *(preset[f] for f in LIMIT_FIELDS)]


def run_project_ui(goal, folder, allow_writes, check, mode, max_tasks, settings,
                   *, forbid=(), declared_paths=(), channel: Optional[OperatorChannel] = None,
                   neutral: bool = False, capture_profile=None, extra_checks=(), readiness=None,
                   run_limits=None, survey=None, repair_tests=(),
                   trust_project_policy=None, allow_project_agent_config=False, declared_checks=True,
                   accept_watched_paths=False, compete='off'):
    """Stream progress while the shared project runner performs model calls.

    ``run_limits`` (a RunLimits) and ``survey`` (a SurveyConfig) go to the
    shared runner unchanged; leaving them out is the run this function made
    before they existed: no shared allowance and no recovery tasks.

    The four trust inputs are the command line's ``--trust-project-policy``,
    ``--allow-project-agent-config``, ``--no-declared-checks`` and
    ``--accept-watched-paths``. Each reaches the runner only when chosen, so
    a form that leaves the Trust section alone makes the call it made before
    the section existed. ``compete`` is the Contests choice (``--compete``)
    and reaches the runner only when it is not ``'off'``.

    The runner passes an interrupt on once the run's record is written. It
    is shown here as an interrupted run with its report, never as a failure
    with nothing to read.
    """
    import dataclasses

    from .project_run import run_project
    from .run_budget import describe_limits, effective_lead_turns
    if neutral:
        settings = dataclasses.replace(settings, neutral_preferences=True)
    events = queue.Queue()
    try:
        tasks = int(max_tasks)
    except (TypeError, ValueError):
        tasks = None  # the runner reports a bad Task limit, as it always has
    notes = ['**Run limits:** ' + describe_limits(
        run_limits, survey, tasks, *effective_lead_turns(getattr(settings, 'lead_max_turns', None), run_limits))]
    yield notes[0], '', []
    from .child_env import withheld_notice
    withheld = withheld_notice()
    if withheld:
        notes.append(withheld)
    trust = {}
    if str(trust_project_policy or '').strip():
        trust['trust_project_policy'] = str(trust_project_policy).strip()
    if allow_project_agent_config:
        trust['allow_project_agent_config'] = True
    if not declared_checks:
        trust['declared_checks'] = False
    if accept_watched_paths:
        trust['accept_watched_paths'] = True
    contests = {'compete': compete} if compete and compete != 'off' else {}
    interrupted = False
    with ThreadPoolExecutor(max_workers=1) as pool:
        # ``tasks``, not a second int(max_tasks): that one was unguarded, so a
        # cleared Task limit raised TypeError out of this generator instead
        # of reaching the runner's refusal (review 2026-11-10, gui_records 17).
        future = pool.submit(run_project, goal, folder, settings,
                             allow_writes=allow_writes, check=check,
                             mode=mode, max_tasks=tasks,
                             forbid=forbid, declared_paths=declared_paths,
                             capture_profile=capture_profile or None,
                             extra_checks=tuple(extra_checks), readiness=readiness or None,
                             run_limits=run_limits, survey=survey,
                             **({'repair_tests': tuple(repair_tests)} if repair_tests else {}),
                             **trust, **contests,
                             progress=events.put,
                             ask_operator=channel.ask if channel is not None else None)
        while not future.done():
            try:
                notes.append(events.get(timeout=0.25))
                yield "\n\n".join(notes), '', []
            except queue.Empty:
                pass
            if channel is not None:
                try:
                    question = channel.questions.get_nowait()
                except queue.Empty:
                    continue
                notes.append(f"**The planner asks:** {question}\n\n"
                             "_Type your answer under 'Answer the planner' and press Send answer. "
                             "It is recorded as a standing ruling for this run._")
                yield "\n\n".join(notes), '', []
        try:
            result = future.result()
        except KeyboardInterrupt as exc:
            # Passed on by the runner once the record was whole
            # (project_run.RunInterrupted carries the result).
            result, interrupted = getattr(exc, 'result', None), True
            if result is None:
                yield '**Run interrupted** before its record was written.', '', []
                return
        except Exception as exc:
            yield f"Run failed: {exc}", '', []
            return
    files, problem = downloadable_files(result.run_dir)
    report = result.report + (f"\n\n_{problem}_" if problem else "")
    if interrupted:
        report = ('**Run interrupted.** Its record was written first and is in '
                  f'`{result.run_dir}`.\n\n' + report)
    yield report, result.diff, files


#: The run files the Project tab offers for download, by exact name.
RUN_FILES = ('report.md', 'changes.diff', 'ledger.md', 'result.json')


def downloadable_files(run_dir):
    """Copies of the run's saved files that the GUI may serve, and a note.

    Gradio serves only files under its working directory, the system temp
    directory or ``allowed_paths``. The run's files live in the selected
    project's ``.quadratus/runs/<id>``, so returning them directly raised
    InvalidPathError and blanked the report, diff and downloads together
    (batch 2 gui-ui-v3 on 5d9f5ff, the first real GUI run). Widening
    ``allowed_paths`` would expose a whole project tree to the server.

    The copy is bound to descriptors, not pathnames (Codex review of
    7515f28: a link check followed by a pathname copy could be raced, and a
    static linked parent was followed). The run folder and the two folders
    above it (``.quadratus/runs/<id>``) must not be links; only the path
    above them, the project's own location, is resolved once (on macOS
    ``/var`` and ``/tmp`` are themselves links). Every component is then
    opened from ``/`` without following links; each named file is opened
    through that folder handle without following links, must be a regular file, and is
    read from its own descriptor into a new file created exclusively in a
    fresh private folder (mode 0700) under the system temp directory. Each
    file is examined without opening it first, then opened non-blocking and
    checked to be the same regular file before any byte is read: a FIFO in
    a file's slot blocked the open itself (Codex review of 35f198e). A
    missing file is simply not offered; a link, a non-regular file or one
    that cannot be read is not offered and is named in the note, with the
    run folder. A failure leaves the report and diff on screen.
    """
    import stat
    import tempfile
    run_dir = Path(run_dir)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    nofollow = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    copies, fds, skipped = [], [], []
    try:
        absolute = Path(os.path.abspath(run_dir))
        own = absolute.parts[-3:] if len(absolute.parts) > 3 else absolute.parts[-1:]
        above = Path(os.path.realpath(absolute.parents[len(own) - 1]))
        fds.append(os.open("/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)))
        for part in (*above.parts[1:], *own):
            fds.append(os.open(part, flags, dir_fd=fds[-1]))
        folder_fd = fds[-1]
        folder = None
        for name in RUN_FILES:
            try:
                seen = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                skipped.append(f"{name} (could not be examined: {exc.strerror or exc})")
                continue
            if stat.S_ISLNK(seen.st_mode):
                skipped.append(f"{name} (a link)")
                continue
            if not stat.S_ISREG(seen.st_mode):
                skipped.append(f"{name} (not a regular file)")
                continue
            try:
                fd = os.open(name, nofollow, dir_fd=folder_fd)
            except OSError as exc:
                skipped.append(f"{name} (could not be opened: {exc.strerror or exc})")
                continue
            try:
                held = os.fstat(fd)
                if not stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) != (seen.st_dev, seen.st_ino):
                    skipped.append(f"{name} (replaced while it was being opened)")
                    continue
                if folder is None:
                    folder = Path(tempfile.mkdtemp(prefix=f"quadratus-{run_dir.name}-"))
                target = folder / name
                out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    while True:
                        chunk = os.read(fd, 1 << 20)
                        if not chunk:
                            break
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(out, view):]
                except OSError as exc:
                    # A partial copy is never offered.
                    os.unlink(target)
                    skipped.append(f"{name} (could not be read: {exc.strerror or exc})")
                    continue
                finally:
                    os.close(out)
                copies.append(str(target))
            finally:
                os.close(fd)
    except OSError as exc:
        return [], f"The saved run files could not be offered for download ({exc}); they are in {run_dir}."
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass
    if skipped:
        return copies, f"Not offered for download: {', '.join(skipped)}; the run's files are in {run_dir}."
    return copies, ""


def policy_approval(text) -> str:
    """The Trust section's policy hash in canonical form, '' when the field
    is blank. Raises ValueError naming the field for anything that is not a
    sha256, so a mistyped approval is refused by name and never passed over
    in silence by a project that has no policy file."""
    from .policy import approval_digest
    return approval_digest(text, TRUST_LABELS['trust_project_policy'])


def policy_preview_ui(folder, paths='', forbid='', writing=False, trust_project_policy='',
                      own_checks_only=False):
    """Same resolver as CLI and dispatch; no project creation or provider construction.

    ``trust_project_policy`` is the Trust section's approval of the project's
    own policy file and ``own_checks_only`` its "Run only my own checks" box.
    Each is passed on only when chosen, so the preview is of the run the form
    would start."""
    from .policy import preview_policy, render_preview
    try:
        approval = policy_approval(trust_project_policy)
        plan = preview_policy(folder, [p.strip() for p in paths.splitlines() if p.strip()],
                              forbid=[p.strip() for p in forbid.splitlines() if p.strip()],
                              writing=writing,
                              **({'trust_project_policy': approval} if approval else {}),
                              **({'declared_checks': False} if own_checks_only else {}))
        return render_preview(plan)
    except (ValueError, OSError) as exc:
        return f'Policy preview failed: {exc}'


#: How often the Monitor tab re-reads the run directory, in seconds. Every
#: read is bounded (quadratus.monitor), so a short interval costs little.
MONITOR_REFRESH_SECONDS = 5
MONITOR_HISTORY = 10


def monitor_view(folder, series='', selected='', default='', limit=MONITOR_HISTORY):
    """The Monitor tab's one read: status markdown and the run history rows.

    Read-only by construction: it calls ``quadratus.monitor`` and nothing
    else, so a refresh can never start, stop or alter a run. A folder typed
    here wins; otherwise the project opened on the Project tab; only then
    ``default``, the project the GUI was started watching. The startup
    default used to fill the folder field and so outranked the project
    actually opened: the operator watched the old project's records while a
    new run was active. A blank series directory means the Stage B packet is
    not read.
    """
    from .monitor import history_rows, read_status, run_history
    from .monitor_story import render_story
    folder = (folder or '').strip() or (selected or '').strip() or (default or '').strip()
    series = (series or '').strip() or None
    if not folder and not series:
        return ('Enter a project folder (or open one on the Project tab), or a Stage B '
                'series directory, to watch its runs.', [])
    status = read_status(folder or None, series)
    project = status.get('project')
    history = run_history(project, limit=limit) if project and project != 'unknown' else []
    return render_story(status), history_rows(history)


def build_interface(settings: Optional[Settings] = None, *, watch_project=''):
    import gradio as gr

    from .project import Project
    from .repo_scan import scan_repo

    settings = settings or Settings.from_env()
    with gr.Blocks(title="Quadratus") as demo:
        svg = inline_mark(52)
        if svg:
            gr.HTML('<div style="display:flex;align-items:center;gap:14px">' + svg +
                    '<span style="font-family:Baskerville,Georgia,serif;font-size:30px;'
                    'letter-spacing:4px">QUADRATUS</span></div>')
        else:
            gr.Markdown('# Quadratus')
        gr.Markdown('Open a project. Give the team a task. Review the files, diff, and test results.')
        with gr.Tabs(selected='monitor' if watch_project else None):
            with gr.Tab('Project'):
                selected = gr.State('')
                project_path = gr.Textbox(label='Project folder', placeholder='/path/to/project')
                with gr.Row():
                    clone_url = gr.Textbox(label='GitHub URL (optional)', placeholder='https://github.com/owner/repo')
                    branch = gr.Textbox(label='New branch (optional)')
                open_button = gr.Button('Open or create project')
                project_info = gr.Markdown('Choose an existing folder or a destination for a new project.')
                source_files = gr.Textbox(label='Project files', lines=8, interactive=False)
                goal = gr.Textbox(label='What should change?', lines=4)
                modes, modes_note = mode_choices()
                with gr.Row():
                    edits = gr.Checkbox(label='Allow changes to project files', value=False)
                    mode = gr.Dropdown(modes, value='adversarial', label='Collaboration',
                                       info=modes_note or None)
                    max_tasks = gr.Number(value=20, minimum=1, precision=0, label='Task limit')
                check = gr.Textbox(label='Test or build command (optional)',
                                   placeholder='Auto-detect from the project, or enter a command')
                contests = gr.Dropdown(list(CONTEST_CHOICES), value='off', label=CONTEST_LABEL,
                                       info=CONTEST_INFO)
                with gr.Accordion('UI capture and further checks (optional)', open=False):
                    gr.Markdown('The same operator inputs as the command line. A capture profile lets the '
                                'harness start your app and capture UI tasks itself; it is validated before '
                                'any model call. Further checks run beside the test command and are required.')
                    capture_profile = gr.Textbox(label='Capture profile file (JSON, --capture-profile)',
                                                 placeholder='/path/to/capture-profile.json')
                    extra_checks = gr.Textbox(label='Further required checks (one command per line, --extra-check)',
                                              lines=2)
                    readiness = gr.Textbox(label='Readiness probes file (JSON, --readiness)',
                                           placeholder='/path/to/readiness.json')
                with gr.Accordion('Run limits (optional)', open=False):
                    gr.Markdown('Cap what one run may spend. Off means no shared allowance, as before. '
                                'A preset fills the fields; the run uses the fields as they read when it '
                                'starts. Token counts are what the CLIs report after each call returns, '
                                'so they are not a hard ceiling and not an invoice.')
                    with gr.Row():
                        limits_on = gr.Checkbox(label='Use run limits', value=False)
                        limits_preset = gr.Dropdown(list(RUN_LIMIT_PRESETS), value=None,
                                                    label='Fill from preset')
                    with gr.Row():
                        limit_calls = gr.Number(value=24, precision=0, minimum=1,
                                                label=LIMIT_LABELS['max_calls'])
                        limit_tokens = gr.Number(value=500_000, precision=0, minimum=1,
                                                 label=LIMIT_LABELS['max_reported_tokens'])
                        limit_seconds = gr.Number(value=900, precision=0, minimum=1,
                                                  label=LIMIT_LABELS['wall_seconds'])
                        limit_workers = gr.Number(value=2, precision=0, minimum=1,
                                                  label=LIMIT_LABELS['max_concurrent_workers'])
                    limit_recovery = gr.Number(value=0, precision=0, minimum=0,
                                               label=LIMIT_LABELS['recovery_tasks'])
                    with gr.Accordion('Advanced token rules', open=False):
                        gr.Markdown('The first is checked before a call starts: no call begins unless this '
                                    'many tokens of the total remain. The second is checked after a call '
                                    'returns: a single call that reported more stops the run, but the call '
                                    'itself has already run.')
                        with gr.Row():
                            limit_reserve = gr.Number(value=0, precision=0, minimum=0,
                                                      label=LIMIT_LABELS['reserve_tokens_per_call'])
                            limit_per_call = gr.Number(value=0, precision=0, minimum=0,
                                                       label=LIMIT_LABELS['max_tokens_per_call'])
                    def summarize(*values):
                        return limits_summary(*values, operator_turns=settings.lead_max_turns)
                    limits_info = gr.Markdown(summarize(20, False, 24, 500_000, 900, 2, 0, 0, 0))
                limit_inputs = [limits_on, limit_calls, limit_tokens, limit_seconds, limit_workers,
                                limit_reserve, limit_per_call, limit_recovery]
                limits_preset.change(lambda name: preset_values(name) if name else [gr.update()] * 8,
                                     inputs=[limits_preset], outputs=limit_inputs)
                for field in [max_tasks, *limit_inputs]:
                    field.change(summarize, inputs=[max_tasks, *limit_inputs], outputs=[limits_info])
                declared_paths = gr.Textbox(label='Paths this task may change (one per line)', lines=2)
                forbid_paths = gr.Textbox(label='Paths that must stay unchanged (one per line)', lines=2)
                repair_paths = gr.Textbox(label='Existing test files this run may repair (exact paths, one per line)',
                                          lines=1, info='Only these stand as repaired when the original tests are '
                                          're-run, and only while every original case still runs unskipped.')
                with gr.Accordion('Trust', open=False):
                    gr.Markdown('What this run may take from the project folder on trust: the command '
                                "line's four trust flags. Left alone, only the environment settings "
                                '(QUADRATUS_TRUST_PROJECT_POLICY, QUADRATUS_ALLOW_PROJECT_AGENT_CONFIG, '
                                'QUADRATUS_ACCEPT_WATCHED_PATHS) apply. A choice here is for the project '
                                'that is open: opening a project clears this section.')
                    trust_policy = gr.Textbox(label=TRUST_LABELS['trust_project_policy'], lines=1,
                                              placeholder='64 hexadecimal characters',
                                              info=TRUST_ALLOWS['trust_project_policy'])
                    trust_agent_config = gr.Checkbox(label=TRUST_LABELS['allow_project_agent_config'],
                                                     value=False,
                                                     info=TRUST_ALLOWS['allow_project_agent_config'])
                    trust_own_checks = gr.Checkbox(label=TRUST_LABELS['no_declared_checks'], value=False,
                                                   info=TRUST_ALLOWS['no_declared_checks'])
                    trust_watched = gr.Checkbox(label=TRUST_LABELS['accept_watched_paths'], value=False,
                                                info=TRUST_ALLOWS['accept_watched_paths'])
                trust_inputs = [trust_policy, trust_agent_config, trust_own_checks, trust_watched]
                preview_button = gr.Button('Preview policy')
                policy_info = gr.Markdown()
                preview_button.click(policy_preview_ui,
                                     inputs=[selected, declared_paths, forbid_paths, edits, trust_policy,
                                             trust_own_checks],
                                     outputs=[policy_info])
                neutral = gr.Checkbox(label='Run without my personal CLI settings '
                                            '(plugins, hooks, user config; account rules are only recorded)',
                                      value=False)
                run_button = gr.Button('Run project task', variant='primary', interactive=False)
                channel = OperatorChannel()
                with gr.Row():
                    answer = gr.Textbox(label='Answer the planner', lines=2)
                    answer_button = gr.Button('Send answer')
                answer_status = gr.Markdown()
                answer_button.click(channel.answer, inputs=[answer], outputs=[answer_status])
                report = gr.Markdown()
                diff = gr.Code(label='Source changes', language=None, interactive=False)
                downloads = gr.File(label='Saved run files', file_count='multiple', interactive=False)

                def open_project(folder, url, new_branch):
                    if not folder.strip():
                        raise gr.Error('Enter the project folder first.')
                    try:
                        project = Project.open(folder, clone_url=url.strip(), branch=new_branch.strip())
                        scan = scan_repo(project.root)
                        files = [p.relative_to(project.root).as_posix() for p in project.files()]
                    except (ValueError, ProviderError, OSError) as exc:
                        raise gr.Error(str(exc)) from exc
                    info = f'**Project:** {project.root}\n\n{scan.file_count} files found. '
                    info += 'Source changes stay in this folder; reports are saved under `.quadratus/runs`.'
                    return (str(project.root), info, '\n'.join(files[:150]), '', '',
                            gr.update(interactive=True))

                opened = open_button.click(open_project, inputs=[project_path, clone_url, branch],
                                           outputs=[selected, project_info, source_files, clone_url, branch,
                                                    run_button])

                def clear_trust():
                    # A trust choice is made for the project that is open. The
                    # section is folded shut, so a box ticked for the last
                    # project would otherwise stay ticked, unseen, for the next
                    # one opened or cloned here.
                    return '', False, False, False

                opened.success(clear_trust, outputs=trust_inputs)

                def run_selected(goal, folder, writes, command, mode, limit, paths, forbid, repairs, no_personal,
                                 profile_path, further, probes, *rest):
                    if not folder:
                        raise gr.Error('Open a project first.')
                    # The run-limit fields, the Trust section's four, then the
                    # Contests choice. A caller that stops early chose nothing
                    # in what it left out.
                    limit_values, rest = rest[:len(limit_inputs)], rest[len(limit_inputs):]
                    trust, rest = rest[:len(trust_inputs)], rest[len(trust_inputs):]
                    untouched = ('', False, False, False)
                    policy_hash, agent_config, own_checks, watched = (*trust, *untouched[len(trust):])[:4]
                    compete = (rest[0] if rest else 'off') or 'off'
                    if compete != 'off' and not writes:
                        # The command line's refusal, in the form's words.
                        raise gr.Error("Contests need 'Allow changes to project files': each draft is made "
                                       'in a private copy of a project that may be edited.')
                    try:
                        run_limits, survey = limits_from_form(*limit_values)
                        # The Task limit is refused here by name, in the words
                        # the form's own summary uses, like every other limit.
                        limit = task_limit(limit)
                    except ValueError as exc:
                        raise gr.Error(f'Run limits are not valid: {exc}') from exc
                    try:
                        approval = policy_approval(policy_hash)
                    except ValueError as exc:
                        raise gr.Error(str(exc)) from exc
                    yield from run_project_ui(goal, folder, writes, command, mode, limit, settings,
                                              declared_paths=[p.strip() for p in paths.splitlines() if p.strip()],
                                              forbid=[p.strip() for p in forbid.splitlines() if p.strip()],
                                              channel=channel, neutral=bool(no_personal),
                                              capture_profile=(profile_path or '').strip() or None,
                                              extra_checks=[c.strip() for c in (further or '').splitlines()
                                                            if c.strip()],
                                              readiness=(probes or '').strip() or None,
                                              run_limits=run_limits, survey=survey,
                                              repair_tests=[p.strip() for p in (repairs or '').splitlines()
                                                            if p.strip()],
                                              trust_project_policy=approval or None,
                                              allow_project_agent_config=bool(agent_config),
                                              declared_checks=not own_checks,
                                              accept_watched_paths=bool(watched),
                                              compete=compete)

                run_button.click(run_selected, inputs=[goal, selected, edits, check, mode, max_tasks, declared_paths,
                                                       forbid_paths, repair_paths, neutral, capture_profile,
                                                       extra_checks, readiness,
                                                       *limit_inputs, *trust_inputs, contests],
                                 outputs=[report, diff, downloads], concurrency_limit=1)
            with gr.Tab('Code discussion'):
                gr.Markdown('This is a separate chat for snippets. To follow the team working on your project, '
                            'open Monitor → Team discussion. Answers here do not create source files.')
                chatbot = gr.Chatbot(height=420, label='Conversation')
                msg = gr.Textbox(label='Your message', lines=2)
                file_upload = gr.File(label='Attach context (optional)',
                                      file_types=['.txt', '.md', '.py', '.js', '.ts', '.html', '.css', '.json', '.yaml'])
                with gr.Row():
                    submit_btn = gr.Button('Send')
                    clear_btn = gr.Button('Clear')

                def respond(message, history, file_obj):
                    history = history or []
                    content = read_file(file_obj, settings)
                    if content:
                        message = f'{message}\n\n=== Attached File ===\n{content}'
                    if not message.strip():
                        return history, '', None
                    orchestrator = Orchestrator(settings)
                    try:
                        result = orchestrator.run(message, history=_history_to_turns(history, settings))
                        reply = _format_response(result)
                    except ProviderError as exc:
                        reply = f'Error: {exc}'
                    finally:
                        for provider in orchestrator.providers:
                            cleanup = getattr(provider, 'cleanup', None)
                            if cleanup:
                                cleanup()
                    return history + [{'role': 'user', 'content': message},
                                      {'role': 'assistant', 'content': reply}], '', None

                submit_btn.click(respond, inputs=[msg, chatbot, file_upload], outputs=[chatbot, msg, file_upload])
                msg.submit(respond, inputs=[msg, chatbot, file_upload], outputs=[chatbot, msg, file_upload])
                clear_btn.click(lambda: ([], '', None), outputs=[chatbot, msg, file_upload])
            with gr.Tab('Monitor', id='monitor'):
                from .monitor import HISTORY_COLUMNS
                gr.Markdown('Follow the latest run: what the team is working on, what happened, and their saved replies. '
                            'This view refreshes every five seconds. Replies arrive after each call finishes. '
                            'Watching uses no model calls and cannot start or change a run.')
                with gr.Accordion('Choose the project to watch', open=False):
                    monitor_folder = gr.Textbox(label='Project folder (blank: the opened project)',
                                                placeholder=watch_project or '/path/to/project')
                    monitor_default = gr.State(watch_project)
                    monitor_series = gr.Textbox(label='Stage B series directory (optional)',
                                                placeholder='/abs/runs')
                with gr.Row():
                    monitor_refresh = gr.Button('Refresh now')
                    monitor_auto = gr.Checkbox(label='Live updates (pause to read replies)', value=True,
                                               info=f'Refreshes every {MONITOR_REFRESH_SECONDS} s; a refresh '
                                               'closes an open reply, so untick this while reading.')
                monitor_status = gr.HTML('<p>Enter a project folder or series directory.</p>')
                with gr.Accordion('Previous runs', open=False):
                    monitor_history = gr.Dataframe(headers=HISTORY_COLUMNS, value=[], interactive=False,
                                                   wrap=True, label=f'Last {MONITOR_HISTORY} runs')
                monitor_timer = gr.Timer(MONITOR_REFRESH_SECONDS, active=True)
                monitor_inputs = [monitor_folder, monitor_series, selected, monitor_default]
                monitor_outputs = [monitor_status, monitor_history]
                monitor_timer.tick(monitor_view, inputs=monitor_inputs, outputs=monitor_outputs,
                                   show_progress='hidden')
                monitor_refresh.click(monitor_view, inputs=monitor_inputs, outputs=monitor_outputs)
                monitor_auto.change(lambda on: gr.Timer(active=bool(on)), inputs=[monitor_auto],
                                    outputs=[monitor_timer])
    return demo


def resolve_share(settings) -> bool:
    """The project UI has local filesystem/command access and no remote auth."""
    raw = env_with_legacy("QUADRATUS_ALLOW_SHARE", "MULTI_LLM_ALLOW_SHARE")
    wants_share = raw.strip().lower() in ("1", "true", "yes")
    if not wants_share:
        return False
    print("Refusing to enable Gradio sharing: the project interface can access "
          "local files and run commands. It is available on localhost only.", file=sys.stderr)
    return False


DEFAULT_GUI_PORT = 7860


def _arguments(argv=None):
    """The parsed ``quadratus-gui`` command line.

    A real parser, because the hand-rolled one read only the two-token
    ``--port N``: ``--port=8000`` and every unknown flag were ignored in
    silence and the GUI served on 7860 (review 2026-11-10, gui_records 16).
    An unknown flag is now an error with the usage. ``--port`` alone still
    reaches ``gui_port``'s own message.
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog="quadratus-gui", allow_abbrev=False,
        description="Serve the Quadratus project interface on 127.0.0.1 only.")
    parser.add_argument("--port", nargs="?", const="", default=None, metavar="N",
                        help="Local port, 1024 to 65535 (default: QUADRATUS_GUI_PORT, else "
                             f"{DEFAULT_GUI_PORT}).")
    parser.add_argument("--no-browser", action="store_true",
                        help="Print the address and do not open it in a browser.")
    return parser.parse_args(sys.argv[1:] if argv is None else list(argv))


def _port_from(flag) -> int:
    raw, source = flag, "--port"
    if raw is None:
        raw, source = os.environ.get("QUADRATUS_GUI_PORT", "").strip() or None, "QUADRATUS_GUI_PORT"
    if raw is None:
        return DEFAULT_GUI_PORT
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 5 and 1024 <= int(raw) <= 65535):
        raise ValueError(f"{source} must be a port number from 1024 to 65535, not {raw!r}")
    return int(raw)


def gui_port(argv=None) -> int:
    """The local port to serve on: ``--port N`` or ``--port=N``, else
    ``QUADRATUS_GUI_PORT``, else 7860. Raises ValueError naming the bad
    value; an unknown flag ends the program with the usage, as argparse does."""
    return _port_from(_arguments(argv).port)


def _port_busy(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _free_port_near(port: int, tries: int = 50):
    """A free port to suggest after ``port``, inside the 1024..65535 range the
    parser accepts: counting up and wrapping to 1024, so 65535 never suggests
    65536 (Codex installed GUI review, P3). None if the bounded search finds
    nothing free."""
    span = 65535 - 1024 + 1
    for step in range(1, min(tries, span - 1) + 1):
        candidate = 1024 + (port - 1024 + step) % span
        if not _port_busy(candidate):
            return candidate
    return None


#: Set to 1 to serve without the per-launch access token. The Host check and
#: the own-page check stay on; nothing turns those off.
NO_TOKEN_ENV = "QUADRATUS_GUI_NO_TOKEN"


def launch_token() -> Optional[str]:
    """A fresh access token for this launch, or None when the operator turned
    the token off with ``QUADRATUS_GUI_NO_TOKEN=1``."""
    from .local_http import new_token
    if os.environ.get(NO_TOKEN_ENV, "").strip().lower() in ("1", "true", "yes"):
        return None
    return new_token()


def guarded_app(demo, port: int, token: Optional[str], *, favicon=None):
    """The application a launch serves: the interface behind ``LocalGuard``.

    The Project tab opens folders and runs the operator's check commands, and
    loopback alone did not protect it: a request with ``Host: evil.example``
    created a folder and ran ``git init`` in it, which is what a DNS
    rebinding page or any other local user can send (review 2026-11-10,
    gui_records 1). ``Blocks.launch()`` has no place to put a check in front
    of every route, and its own start-up request would be refused by one, so
    the interface is mounted on an application of our own and the whole of
    it is wrapped. Nothing is reachable around the wrapper: pages, API
    routes, the queue, event streams, file routes and websockets all pass
    the Host check, the own-page check and (unless turned off) the token.

    Server-side rendering is off by name: with it on, Gradio starts a Node
    process in front of this one, on a port and with headers we do not set.
    """
    import fastapi
    import gradio as gr

    from .local_http import LocalGuard
    app = fastapi.FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    gr.mount_gradio_app(app, demo, path="/", server_name="127.0.0.1", server_port=port,
                        css=INTERFACE_CSS, ssr_mode=False,
                        favicon_path=str(favicon) if favicon is not None else None)
    return LocalGuard(app, port=port, token=token)


def local_server(app, port: int):
    """The loopback-only server for ``app``; ``.run()`` serves until stopped."""
    import uvicorn
    # A short grace period: the Monitor tab holds an event stream open, and
    # without one Ctrl-C would wait for the browser to let go of it.
    return uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                         timeout_graceful_shutdown=3))


def _has_desktop() -> bool:
    # Without one, the "browser" may be a text browser that takes over the terminal.
    return sys.platform in ("darwin", "win32") or bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_in_browser(url: str) -> Optional[Path]:
    """Open ``url`` in the operator's browser, through a private file.

    The address carries the access token, and a browser started with it on
    its command line shows it to every user of the machine in the process
    list. So the browser is given a file only this user can read (mode 0600
    in a 0700 folder), and that file moves on to the address. Returns the
    folder to remove when the server stops, or None when nothing was opened;
    never raises, because the printed address always works.
    """
    import html
    import tempfile
    import webbrowser
    if not _has_desktop():
        return None
    folder = None
    try:
        folder = Path(tempfile.mkdtemp(prefix="quadratus-gui-"))
        page = folder / "open.html"
        target = html.escape(url, quote=True)
        fd = os.open(page, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                      f'<meta http-equiv="refresh" content="0; url={target}"><title>Quadratus</title></head>'
                      f'<body><p><a href="{target}">Open Quadratus</a></p></body></html>')
        webbrowser.open(page.as_uri())
    except Exception:  # noqa: BLE001 -- opening a browser is a convenience, never a launch failure
        pass
    return folder


def main(argv=None) -> int:
    args = _arguments(argv)
    try:
        import gradio  # noqa: F401
    except ImportError:
        print("Gradio is not installed. Run: pip install gradio", file=sys.stderr)
        return 1
    try:
        port = _port_from(args.port)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if _port_busy(port):
        free = _free_port_near(port)
        how = (f"quadratus-gui --port {free}" if free is not None
               else "quadratus-gui --port N, with N from 1024 to 65535,")
        print(f"Port {port} on 127.0.0.1 is already in use. Start on another port with "
              f"{how} (or set QUADRATUS_GUI_PORT).", file=sys.stderr)
        return 2
    demo = build_interface(watch_project=os.environ.get('QUADRATUS_MONITOR_PROJECT', ''))
    # Says so when sharing was asked for. Nothing below can share: the
    # server binds to 127.0.0.1 and no tunnel is ever started.
    resolve_share(Settings.from_env())
    token = launch_token()
    app = guarded_app(demo, port, token, favicon=brand_asset("favicon.svg"))
    address = f"http://127.0.0.1:{port}/"
    if token is None:
        url = address
        print(f"Quadratus is serving on {address} with the access token turned off ({NO_TOKEN_ENV}): "
              "any user or program on this computer can open it and run commands as you.", flush=True)
    else:
        url = f"{address}?token={token}"
        print(f"Quadratus is serving on {url}", flush=True)
        print("Open that address: it carries this launch's access token, and the page answers "
              f"nothing without it. ({NO_TOKEN_ENV}=1 starts without a token.)", flush=True)
    server = local_server(app, port)
    opened: List[Optional[Path]] = []
    if not args.no_browser:
        def open_when_listening():
            import time
            deadline = time.monotonic() + 60
            while not server.started and not server.should_exit and time.monotonic() < deadline:
                time.sleep(0.05)
            if server.started and not server.should_exit:
                opened.append(open_in_browser(url))
        threading.Thread(target=open_when_listening, daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        import shutil
        for folder in opened:
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
