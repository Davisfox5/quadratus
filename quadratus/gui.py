"""Gradio web interface for the multi-LLM workflow."""

from __future__ import annotations

import os
import re
import sys
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


def build_interface(settings: Optional[Settings] = None):
    import gradio as gr

    settings = settings or Settings.from_env()
    orchestrator = Orchestrator(settings)
    available = [p.status for p in orchestrator.available]
    status_md = (
        "**Active collaborators:** " + ", ".join(available)
        if available
        else "⚠️ **No providers configured.** Set at least one API key in `.env`."
    )

    with gr.Blocks(title="Quadratus") as demo:
        svg = inline_mark(52)
        if svg is None:
            gr.Markdown("# Quadratus")
        else:
            gr.HTML(
                '<div style="display:flex;align-items:center;gap:14px">'
                f"{svg}"
                '<span style="font-family:Baskerville,Georgia,serif;font-size:30px;'
                'font-weight:700;letter-spacing:4.6px">QUADRATUS</span>'
                "</div>"
            )
        gr.Markdown(
            "Claude, ChatGPT, and Gemini collaborate — one drafts, the others "
            "review and refine, and a synthesizer merges the best ideas into a "
            "single answer."
        )
        gr.Markdown(status_md)

        chatbot = gr.Chatbot(height=520, label="Conversation")

        with gr.Row():
            msg = gr.Textbox(
                placeholder="Describe the coding task...",
                label="Your message",
                scale=4,
                lines=2,
            )
            file_upload = gr.File(
                label="Attach a file (optional)",
                file_types=[".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".json", ".csv", ".yaml", ".yml"],
                scale=1,
            )

        with gr.Row():
            submit_btn = gr.Button("Send", variant="primary")
            clear_btn = gr.Button("Clear")

        def respond(message, history):
            history = history or []
            if not message or not message.strip():
                return history, ""
            if not orchestrator.available:
                history = history + [
                    {"role": "user", "content": message},
                    {
                        "role": "assistant",
                        "content": "⚠️ No providers are configured. Set an API key in `.env`.",
                    },
                ]
                return history, ""

            turns = _history_to_turns(history, settings)
            try:
                result = orchestrator.run(message, history=turns)
                reply = _format_response(result)
            except ProviderError as exc:
                reply = f"⚠️ {exc}"
            history = history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": reply},
            ]
            return history, ""

        def attach_and_respond(message, history, file_obj):
            file_content = read_file(file_obj, settings)
            if file_content:
                message = f"{message}\n\n=== Attached File ===\n{file_content}"
            return respond(message, history)

        submit_btn.click(
            attach_and_respond,
            inputs=[msg, chatbot, file_upload],
            outputs=[chatbot, msg],
        )
        msg.submit(
            attach_and_respond,
            inputs=[msg, chatbot, file_upload],
            outputs=[chatbot, msg],
        )
        clear_btn.click(lambda: ([], ""), outputs=[chatbot, msg])

    return demo


def resolve_share(settings) -> bool:
    """Decide whether Gradio's public share tunnel may be enabled.

    Subscription (CLI) transport authenticates as *you*. A public share link
    would route strangers' prompts through your personal credential, which the
    consumer terms of Anthropic, OpenAI and Google all prohibit -- and which
    all three enforce server-side. Sharing is therefore refused outright
    whenever any provider is on CLI transport, regardless of the opt-in
    variable; on pure API transport the usage is billed to your key and the
    opt-in is honoured.
    """
    raw = env_with_legacy("QUADRATUS_ALLOW_SHARE", "MULTI_LLM_ALLOW_SHARE")
    wants_share = raw.strip().lower() in ("1", "true", "yes")
    if not wants_share:
        return False
    if settings.uses_cli():
        print(
            "Refusing to enable Gradio sharing: one or more providers use "
            "subscription (CLI) transport, and exposing that publicly would "
            "route other people's prompts through your personal subscription. "
            "Set every provider to the 'api' backend to share.",
            file=sys.stderr,
        )
        return False
    return True


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
