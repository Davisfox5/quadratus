"""Gradio web interface for the multi-LLM workflow."""

from __future__ import annotations

import math
import os
import queue
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from .config import Settings, env_with_legacy
from .orchestrator import Orchestrator
from .providers import ProviderError, Turn

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

    def ask(self, question: str) -> str:
        from .session import OperatorInputNeeded
        self.waiting = True
        self.questions.put(question)
        try:
            return self.answers.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise OperatorInputNeeded(question) from exc
        finally:
            self.waiting = False

    def answer(self, text: str) -> str:
        text = (text or "").strip()
        if not self.waiting:
            return "No question is waiting for an answer."
        if not text:
            return "Type an answer first."
        self.answers.put(text)
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

#: Form order of the limit fields, shared by the preset filler and the form reader.
LIMIT_FIELDS = ('max_calls', 'max_reported_tokens', 'wall_seconds', 'max_concurrent_workers',
                'reserve_tokens_per_call', 'max_tokens_per_call', 'recovery_tasks')

_LIMIT_LABELS = {
    'max_tasks': 'Task limit',
    'max_calls': 'Maximum model calls',
    'max_reported_tokens': 'Total reported tokens',
    'wall_seconds': 'Time limit (seconds)',
    'max_concurrent_workers': 'Parallel workers',
    'reserve_tokens_per_call': 'Tokens that must remain before a call starts',
    'max_tokens_per_call': 'Stop after a single call reports more than (tokens, 0 for none)',
    'recovery_tasks': 'Extra recovery tasks after failures (0 for none)',
}


def _whole(value, field, minimum):
    label = _LIMIT_LABELS[field]
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f'{label}: enter a whole number.')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label}: {value!r} is not a number.') from None
    if not math.isfinite(number) or number != int(number) or number < minimum:
        raise ValueError(f'{label} must be a whole number of at least {minimum}.')
    return int(number)


def limits_from_form(enabled, *values):
    """The validated RunLimits and SurveyConfig the form describes.

    Returns ``(None, None)`` when limits are off and no recovery task is
    asked for, which is exactly the call a run without these options made
    before they existed. Raises ValueError naming the field otherwise, before
    any project, provider or model is touched.
    """
    from .run_budget import RunLimits
    from .session import SurveyConfig
    fields = dict(zip(LIMIT_FIELDS, values, strict=True))
    recovery = _whole(fields.get('recovery_tasks', 0) or 0, 'recovery_tasks', 0)
    survey = SurveyConfig(recovery_tasks=recovery) if recovery else None
    if not enabled:
        return None, survey
    per_call = _whole(fields.get('max_tokens_per_call', 0) or 0, 'max_tokens_per_call', 0)
    limits = RunLimits(
        max_calls=_whole(fields.get('max_calls'), 'max_calls', 1),
        max_reported_tokens=_whole(fields.get('max_reported_tokens'), 'max_reported_tokens', 1),
        wall_seconds=_whole(fields.get('wall_seconds'), 'wall_seconds', 1),
        max_concurrent_workers=_whole(fields.get('max_concurrent_workers'), 'max_concurrent_workers', 1),
        reserve_tokens_per_call=_whole(fields.get('reserve_tokens_per_call', 0) or 0,
                                       'reserve_tokens_per_call', 0),
        max_tokens_per_call=per_call or None,
    )
    return limits, survey


def limits_summary(max_tasks, enabled, *values, operator_turns=None) -> str:
    """What the form would run with, or why it would refuse. ``operator_turns``
    is ``Settings.lead_max_turns``, which outranks a derived lead turn cap."""
    from .run_budget import describe_limits, effective_lead_turns
    try:
        limits, survey = limits_from_form(enabled, *values)
        tasks = _whole(max_tasks, 'max_tasks', 1)
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
                   run_limits=None, survey=None):
    """Stream progress while the shared project runner performs model calls.

    ``run_limits`` (a RunLimits) and ``survey`` (a SurveyConfig) go to the
    shared runner unchanged; leaving them out is the run this function made
    before they existed: no shared allowance and no recovery tasks.
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
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_project, goal, folder, settings,
                             allow_writes=allow_writes, check=check,
                             mode=mode, max_tasks=int(max_tasks),
                             forbid=forbid, declared_paths=declared_paths,
                             capture_profile=capture_profile or None,
                             extra_checks=tuple(extra_checks), readiness=readiness or None,
                             run_limits=run_limits, survey=survey,
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
        except Exception as exc:
            yield f"Run failed: {exc}", '', []
            return
    files, problem = downloadable_files(result.run_dir)
    report = result.report + (f"\n\n_{problem}_" if problem else "")
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
    ``allowed_paths`` would expose a whole project tree to the server; this
    copies exactly these named regular files into a fresh private folder
    (mode 0700) under the system temp directory, named after the run. The
    originals stay where they are and are the record. A file that is
    missing or a link is skipped; a copy that fails leaves the report and
    diff on screen with a note naming the run folder.
    """
    import shutil
    import tempfile
    run_dir = Path(run_dir)
    present = [run_dir / name for name in RUN_FILES
               if (run_dir / name).is_file() and not (run_dir / name).is_symlink()]
    if not present:
        return [], ""
    try:
        folder = Path(tempfile.mkdtemp(prefix=f"quadratus-{run_dir.name}-"))
        copies = []
        for source in present:
            target = folder / source.name
            shutil.copyfile(source, target)
            copies.append(str(target))
    except OSError as exc:
        return [], f"The saved run files could not be offered for download ({exc}); they are in {run_dir}."
    return copies, ""


def policy_preview_ui(folder, paths='', forbid='', writing=False):
    """Same resolver as CLI and dispatch; no project creation or provider construction."""
    from .policy import preview_policy, render_preview
    try:
        plan = preview_policy(folder, [p.strip() for p in paths.splitlines() if p.strip()],
                              forbid=[p.strip() for p in forbid.splitlines() if p.strip()],
                              writing=writing)
        return render_preview(plan)
    except (ValueError, OSError) as exc:
        return f'Policy preview failed: {exc}'


def build_interface(settings: Optional[Settings] = None):
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
        with gr.Tabs():
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
                with gr.Row():
                    edits = gr.Checkbox(label='Allow changes to project files', value=False)
                    mode = gr.Dropdown(['adversarial', 'collaborative', 'solo'], value='adversarial', label='Collaboration')
                    max_tasks = gr.Number(value=20, minimum=1, precision=0, label='Task limit')
                check = gr.Textbox(label='Test or build command (optional)',
                                   placeholder='Auto-detect from the project, or enter a command')
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
                                                label=_LIMIT_LABELS['max_calls'])
                        limit_tokens = gr.Number(value=500_000, precision=0, minimum=1,
                                                 label=_LIMIT_LABELS['max_reported_tokens'])
                        limit_seconds = gr.Number(value=900, precision=0, minimum=1,
                                                  label=_LIMIT_LABELS['wall_seconds'])
                        limit_workers = gr.Number(value=2, precision=0, minimum=1,
                                                  label=_LIMIT_LABELS['max_concurrent_workers'])
                    limit_recovery = gr.Number(value=0, precision=0, minimum=0,
                                               label=_LIMIT_LABELS['recovery_tasks'])
                    with gr.Accordion('Advanced token rules', open=False):
                        gr.Markdown('The first is checked before a call starts: no call begins unless this '
                                    'many tokens of the total remain. The second is checked after a call '
                                    'returns: a single call that reported more stops the run, but the call '
                                    'itself has already run.')
                        with gr.Row():
                            limit_reserve = gr.Number(value=0, precision=0, minimum=0,
                                                      label=_LIMIT_LABELS['reserve_tokens_per_call'])
                            limit_per_call = gr.Number(value=0, precision=0, minimum=0,
                                                       label=_LIMIT_LABELS['max_tokens_per_call'])
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
                preview_button = gr.Button('Preview policy')
                policy_info = gr.Markdown()
                preview_button.click(policy_preview_ui,
                                     inputs=[selected, declared_paths, forbid_paths, edits],
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

                open_button.click(open_project, inputs=[project_path, clone_url, branch],
                                  outputs=[selected, project_info, source_files, clone_url, branch, run_button])

                def run_selected(goal, folder, writes, command, mode, limit, paths, forbid, no_personal,
                                 profile_path, further, probes, *limit_values):
                    if not folder:
                        raise gr.Error('Open a project first.')
                    try:
                        run_limits, survey = limits_from_form(*limit_values)
                    except ValueError as exc:
                        raise gr.Error(f'Run limits are not valid: {exc}') from exc
                    yield from run_project_ui(goal, folder, writes, command, mode, limit, settings,
                                              declared_paths=[p.strip() for p in paths.splitlines() if p.strip()],
                                              forbid=[p.strip() for p in forbid.splitlines() if p.strip()],
                                              channel=channel, neutral=bool(no_personal),
                                              capture_profile=(profile_path or '').strip() or None,
                                              extra_checks=[c.strip() for c in (further or '').splitlines()
                                                            if c.strip()],
                                              readiness=(probes or '').strip() or None,
                                              run_limits=run_limits, survey=survey)

                run_button.click(run_selected, inputs=[goal, selected, edits, check, mode, max_tasks, declared_paths,
                                                       forbid_paths, neutral, capture_profile, extra_checks, readiness,
                                                       *limit_inputs],
                                 outputs=[report, diff, downloads], concurrency_limit=1)
            with gr.Tab('Code discussion'):
                gr.Markdown('Discuss snippets without opening a project. Answers here do not create source files.')
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


def gui_port(argv=None) -> int:
    """The local port to serve on: ``--port N``, else ``QUADRATUS_GUI_PORT``,
    else 7860. Raises ValueError naming the bad value."""
    raw, source = None, "QUADRATUS_GUI_PORT"
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--port" in argv:
        at = argv.index("--port")
        raw, source = (argv[at + 1] if at + 1 < len(argv) else ""), "--port"
    elif os.environ.get("QUADRATUS_GUI_PORT", "").strip():
        raw = os.environ["QUADRATUS_GUI_PORT"].strip()
    if raw is None:
        return DEFAULT_GUI_PORT
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        raise ValueError(f"{source} must be a port number from 1024 to 65535, not {raw!r}")
    return int(raw)


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


def main(argv=None) -> int:
    try:
        import gradio  # noqa: F401
    except ImportError:
        print("Gradio is not installed. Run: pip install gradio", file=sys.stderr)
        return 1
    try:
        port = gui_port(argv)
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
    demo = build_interface()
    favicon = brand_asset("favicon.svg")
    print(f"Quadratus is serving on http://127.0.0.1:{port}", flush=True)
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        css=INTERFACE_CSS,
        share=resolve_share(Settings.from_env()),
        favicon_path=str(favicon) if favicon is not None else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
