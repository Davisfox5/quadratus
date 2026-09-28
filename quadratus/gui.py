"""Gradio web interface for the multi-LLM workflow."""

from __future__ import annotations

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


def run_project_ui(goal, folder, allow_writes, check, mode, max_tasks, settings,
                   *, forbid=(), declared_paths=(), channel: Optional[OperatorChannel] = None,
                   neutral: bool = False):
    """Stream progress while the shared project runner performs model calls."""
    import dataclasses

    from .project_run import run_project
    if neutral:
        settings = dataclasses.replace(settings, neutral_preferences=True)
    events = queue.Queue()
    notes = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_project, goal, folder, settings,
                             allow_writes=allow_writes, check=check,
                             mode=mode, max_tasks=int(max_tasks),
                             forbid=forbid, declared_paths=declared_paths,
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
    yield result.report, result.diff, [str(result.run_dir / name)
                                      for name in ('report.md', 'changes.diff', 'ledger.md', 'result.json')]


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

                def run_selected(goal, folder, writes, command, mode, limit, paths, forbid, no_personal):
                    if not folder:
                        raise gr.Error('Open a project first.')
                    yield from run_project_ui(goal, folder, writes, command, mode, limit, settings,
                                              declared_paths=[p.strip() for p in paths.splitlines() if p.strip()],
                                              forbid=[p.strip() for p in forbid.splitlines() if p.strip()],
                                              channel=channel, neutral=bool(no_personal))

                run_button.click(run_selected, inputs=[goal, selected, edits, check, mode, max_tasks, declared_paths, forbid_paths, neutral],
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


def main() -> int:
    try:
        import gradio  # noqa: F401
    except ImportError:
        print("Gradio is not installed. Run: pip install gradio", file=sys.stderr)
        return 1
    demo = build_interface()
    favicon = brand_asset("favicon.svg")
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        css=INTERFACE_CSS,
        share=resolve_share(Settings.from_env()),
        favicon_path=str(favicon) if favicon is not None else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
