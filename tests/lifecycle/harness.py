"""A whole-run replay harness below the vendor CLIs.

``cli_providers._launch`` is the one place Quadratus executes a vendor CLI.
Replacing it, and nothing above it, keeps every harness layer real: argv
building, the vendors' envelope extractors and usage meters, Fleet's
per-call source-truth (CHANGED) check, the session lifecycle, the worker
pool, the integration gate, the design check and the close-out. A scripted
responder answers each launch as the vendor would, in that vendor's own
envelope, and may edit the working directory the way an editing call does.

Controller determinism, not live reliability: a pass says what the harness
does with a given reply, never how often a model produces it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from quadratus import cli_providers
from quadratus.config import Settings
from quadratus.delegation import invocation_context
from quadratus.project_run import run_project
from quadratus.run_budget import RunLimits

USAGE = {"input_tokens": 100, "output_tokens": 10}

#: The real integration gate, run with this test's own interpreter: a bare
#: ``python`` is not on every host (Codex review: "command not found" on the
#: Mac made four lifecycle cases stop after t1).
GATE = f"{shlex.quote(sys.executable)} -m pytest -q"

#: Render timestamps are set, never left to the write clock. The design check
#: compares a screenshot's mtime with the start of the last editing call; a
#: render written inside that call can land on either side on a coarse or
#: containerised filesystem (frozen-image run: 5 of 46 failed once, then passed).
STALE = -3600.0
FRESH = 3600.0


# -- vendor envelopes -------------------------------------------------------------

def claude_ok(text, *, num_turns=1):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "num_turns": num_turns, "result": text, "stop_reason": "end_turn",
                       "session_id": str(uuid.uuid4()), "usage": USAGE})


def claude_cap(text=None, *, num_turns=14):
    return json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": False,
                       "num_turns": num_turns, "stop_reason": None, "session_id": str(uuid.uuid4()),
                       "usage": USAGE, "errors": [], **({"result": text} if text else {})})


def claude_error(message, *, num_turns=20):
    return json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                       "num_turns": num_turns, "stop_reason": None, "session_id": str(uuid.uuid4()),
                       "usage": USAGE, "errors": [message]})


def claude_refusal(category="reasoning_extraction"):
    return json.dumps({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
                       "result": "", "stop_reason": "refusal", "stop_details": {"category": category},
                       "session_id": str(uuid.uuid4()), "usage": USAGE})


def codex_ok(text):
    thread = str(uuid.uuid4())
    events = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}},
              {"type": "turn.completed", "usage": USAGE}]
    return "\n".join(json.dumps(e) for e in events)


def grok_ok(text, *, stop="end_turn", num_turns=1, error=None):
    envelope = {"text": text, "stopReason": stop, "num_turns": num_turns,
                "sessionId": str(uuid.uuid4()), "usage": USAGE}
    if error is not None:
        envelope["error"] = error
    return json.dumps(envelope)


ENVELOPE = {"claude": claude_ok, "codex": codex_ok, "grok": grok_ok}


# -- the fake launcher ------------------------------------------------------------

@dataclass
class Call:
    vendor: str
    model: str
    role: str
    task: str
    prompt: str
    cwd: str
    argv: List[str]
    reply: str = ""


@dataclass
class Replay:
    """One scripted run: a responder, the calls it saw, what the run left."""
    responder: Callable[["Call", "Replay"], object]
    calls: List[Call] = field(default_factory=list)
    project: Optional[Path] = None
    result: object = None

    def artifacts(self, kind_prefix: str) -> List[str]:
        """Ids of stored artifacts with any recorded kind starting ``kind_prefix``."""
        return [path.stem for path in self._texts() if any(
            kind.startswith(kind_prefix) for kind in self._kinds(path))]

    def artifact_texts(self, kind: str) -> List[str]:
        return [path.read_text() for path in self._texts() if kind in self._kinds(path)]

    def _texts(self):
        return sorted((self.project / ".quadratus" / "runs").glob("*/artifacts/*.txt"))

    @staticmethod
    def _kinds(text_path: Path):
        kinds = set()
        for meta in text_path.parent.glob(text_path.stem + "*.json"):
            try:
                kinds.add(str(json.loads(meta.read_text()).get("kind", "")))
            except ValueError:
                continue
        return kinds

    def _kinds_all(self):
        return set().union(*(self._kinds(path) for path in self._texts())) if self._texts() else set()

    def of(self, role_prefix: str) -> List[Call]:
        return [c for c in self.calls if c.role.startswith(role_prefix)]


def _vendor(argv) -> str:
    return Path(argv[0]).name


def _model(argv) -> str:
    for flag in ("--model", "-m"):
        if flag in argv:
            return argv[argv.index(flag) + 1]
    return ""


def fake_launch(replay: Replay):
    def launch(argv, *, input=None, timeout=None, cwd=None, env=None):  # noqa: A002
        vendor = _vendor(argv)
        context = invocation_context.get() or {}
        prompt = input
        if prompt is None and "--prompt-file" in argv:
            prompt = Path(argv[argv.index("--prompt-file") + 1]).read_text()
        prompt = prompt if prompt is not None else " ".join(argv)
        call = Call(vendor, _model(argv), context.get("role", "direct"), context.get("task", "run"),
                    prompt, str(cwd or ""), list(argv))
        replay.calls.append(call)
        answer = replay.responder(call, replay)
        stdout = answer if isinstance(answer, str) and answer.lstrip().startswith("{") else \
            ENVELOPE[vendor](answer if isinstance(answer, str) else str(answer))
        call.reply = stdout
        return subprocess.CompletedProcess(argv, 0, stdout, "")
    return launch


def write(call: Call, files: Dict[str, str]) -> None:
    """Edit the calling seat's working directory, as an editing CLI would."""
    for name, text in files.items():
        target = Path(call.cwd) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def run(tmp_path, monkeypatch, responder, *, files, check=GATE, max_tasks=1,
        limits=None, settings=None, extra_checks=()) -> Replay:
    project = tmp_path / "project"
    project.mkdir()
    for name, text in files.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text)
    replay = Replay(responder=responder, project=project)
    monkeypatch.setattr("shutil.which", lambda name: f"/fake/bin/{name}")
    for vendor in ("OPENAI", "CLAUDE", "GROK"):
        monkeypatch.delenv(f"QUADRATUS_CLI_ARGS_{vendor}", raising=False)
    monkeypatch.delenv("QUADRATUS_CONTAINED", raising=False)
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    monkeypatch.setattr(cli_providers, "_launch", fake_launch(replay))
    monkeypatch.setattr(cli_providers.CLIProvider, "available", lambda self: True)
    settings = settings or Settings(backend="cli")
    settings.backend_overrides = {}
    settings.openai_api_key = settings.anthropic_api_key = settings.xai_api_key = None
    replay.result = run_project("Build the feature.", project, settings, allow_writes=True,
                                check=check, max_tasks=max_tasks, extra_checks=extra_checks,
                                run_limits=limits or RunLimits(max_calls=120, max_reported_tokens=6_000_000,
                                                               wall_seconds=600, max_concurrent_workers=2))
    return replay


def evidence(root: Path, task_id: str, *, age: float, target="http://127.0.0.1:5000/import") -> None:
    """Clean desktop and mobile renders, as ``quadratus.design_evidence`` writes them.

    ``age`` is the screenshots' mtime offset from now: ``STALE`` predates any
    later editing call, ``FRESH`` postdates the current one.
    """
    import struct
    import zlib

    from quadratus.design_evidence import evidence_dir

    def png(width):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, 1, 8, 0, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"\x00" * (width + 1))) + chunk(b"IEND", b""))
    views = {}
    for name, width in (("desktop", 1280), ("mobile", 390)):
        folder = evidence_dir(root, task_id) / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "page.png").write_bytes(png(width))
        stamp = time.time() + age
        os.utime(folder / "page.png", (stamp, stamp))
        views[name] = dict(clean=True, console_errors=[], failed_requests=[])
    (evidence_dir(root, task_id) / "summary.json").write_text(json.dumps(dict(target=target, views=views)))


def gate_results(replay: Replay) -> List[str]:
    """PASSED/FAILED for each integration check the run recorded, in order."""
    return re.findall(r"^Check (PASSED|FAILED): ", replay.result.report, re.MULTILINE)


def result_json(replay: Replay) -> dict:
    return json.loads((Path(replay.result.run_dir) / "result.json").read_text())


def declared(prompt: str, marker: str) -> bool:
    return bool(re.search(marker, prompt))
