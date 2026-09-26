"""What each call did inside its own session, read from the vendor's transcript.

The ledger records one row per call: who, how long, how many tokens. It cannot
say what happened *inside* the call, and in the Q9-v2 and GameTape runs that is
where the answers were: a lead that read the external grader, a lead that
wrote WORKER and FETCH requests into its reasoning where nothing serves them,
an account-level rule injected into every grok session, a codex seat reading
plugin files from the operator's home directory. All of it was on disk in the
vendors' own session stores, and none of it reached the run record.

This module copies each call's transcript into the run's private evidence and
extracts a trace per call: tool calls and their outcomes, files read and
written, commands and exit codes, paths outside the project, protocol requests
written in-loop that the harness never served, and injected rules. Nothing
here calls a model, and nothing here can fail a run: every reader degrades to
"unknown" rather than raising.

Raw transcripts contain prompts and source. They stay in
``<run>/native-private`` (owner-only) and are never published without
inspection. So does ``trace-detail.jsonl``, which keeps command text, the
reasoning around an unserved request and injected rule text. The shareable
``trace.jsonl`` and the report carry names, outcomes, paths, exit codes and
hashes only (Codex review of #25): an argument or a sentence of reasoning can
hold source or a secret, and a file path rarely does.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Dict, List, Optional

#: A harness request written somewhere other than as the whole reply.
PROTOCOL = re.compile(r"(?<![A-Za-z])(WORKER\s*\{|FETCH:\s*\S|CONSULT\s+[A-Za-z]|ASK:\s*\S)")
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{6,120}")
_ABS_PATH = re.compile(r"(?<![\w.*-])(/(?:[\w.@%+-]+/)*[\w.@%+-]+)")
_SYSTEM_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/opt/homebrew/", "/System/", "/Library/",
                    "/dev/", "/etc/", "/proc/")
_READ_TOOLS = {"Read", "Glob", "Grep", "LS", "read_file", "list_dir", "grep", "glob"}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write", "search_replace",
                "edit_file", "apply_patch"}
_SHELL_TOOLS = {"Bash", "run_terminal_command", "exec_command", "shell"}


def vendor_of(model: Optional[str]) -> Optional[str]:
    prefix = (model or "").split(":", 1)[0]
    return {"claude": "claude", "openai": "codex", "grok": "grok"}.get(prefix)


def session_roots(env=None) -> Dict[str, Path]:
    """Where each CLI keeps its session transcripts."""
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())
    return {
        "claude": Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects",
        "codex": Path(env.get("CODEX_HOME") or home / ".codex") / "sessions",
        "grok": home / ".grok" / "sessions",
    }


def locate(vendor: Optional[str], session_id: Optional[str], roots: Dict[str, Path]) -> Optional[Path]:
    """The transcript for one call, or None. Session ids are validated before
    they are used in a glob, so a hostile id cannot widen the search."""
    if not vendor or not session_id or not _SAFE_ID.fullmatch(session_id):
        return None
    root = roots.get(vendor)
    if root is None or not root.is_dir():
        return None
    try:
        if vendor == "claude":
            hits = sorted(root.glob(f"*/{session_id}.jsonl"))
        elif vendor == "codex":
            hits = sorted(root.rglob(f"rollout-*{session_id}*.jsonl"))
        else:
            hits = sorted(p for p in root.glob(f"*/{session_id}") if p.is_dir())
    except OSError:
        return None
    return hits[0] if hits else None


def _private_copy(source: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.is_dir():
        shutil.copytree(source, dest, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.lock", "terminal"))
        for path in [dest, *dest.rglob("*")]:
            path.chmod(0o700 if path.is_dir() else 0o600)
    else:
        shutil.copyfile(source, dest)
        dest.chmod(0o600)
    return dest


def _jsonl(path: Path) -> List[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        pass
    return rows


def _target(args) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args[:300]
    if not isinstance(args, dict):
        return ""
    for key in ("command", "cmd", "file_path", "target_file", "path", "pattern", "notebook_path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value[:300]
    return ""


def _empty(vendor: str) -> dict:
    return dict(vendor=vendor, cwd=None, tool_calls=[], commands=[], files_read=[], files_written=[],
                files_not_written=[], outside_project=[], protocol_attempts=[], injected_rules=[], texts=[], reasoning=[])


def _record_write(trace: dict, path: str, outcome) -> None:
    """File a write under what it did, not what it tried.

    Only a write the transcript reports as successful is a written file.
    GameTape run 9 (2026-09-26): t8's trace listed /work/csv_preview.py as
    written while every Edit on it had been denied and the project diff was
    empty. Denied, failed and unconfirmed attempts are kept apart, because
    an attempt is still evidence of what the call was trying to do.
    """
    trace["files_written" if outcome == "success" else "files_not_written"].append(path)


def _claude(path: Path) -> dict:
    trace = _empty("claude")
    results = {}
    calls = []
    for row in _jsonl(path):
        trace["cwd"] = trace["cwd"] or row.get("cwd")
        message = row.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if row.get("type") == "assistant" and block.get("type") == "text":
                trace["texts"].append(block.get("text") or "")
            elif row.get("type") == "assistant" and block.get("type") == "thinking":
                trace["reasoning"].append(block.get("thinking") or "")
            elif block.get("type") == "tool_use":
                calls.append((block.get("id"), block.get("name"), _target(block.get("input"))))
            elif block.get("type") == "tool_result":
                text = json.dumps(block.get("content"))[:400]
                denied = bool(re.search(r"(?i)permission|not allowed|denied", text)) and block.get("is_error")
                results[block.get("tool_use_id")] = "denied" if denied else (
                    "error" if block.get("is_error") else "success")
    for cid, name, target in calls:
        trace["tool_calls"].append(dict(name=name, target=target, outcome=results.get(cid, "unknown")))
    return trace


def _codex(path: Path) -> dict:
    trace = _empty("codex")
    outputs = {}
    calls = []
    for row in _jsonl(path):
        payload = row.get("payload") or {}
        kind = payload.get("type")
        if row.get("type") == "session_meta":
            trace["cwd"] = trace["cwd"] or payload.get("cwd")
        elif kind in ("custom_tool_call", "function_call"):
            calls.append((payload.get("call_id"), payload.get("name"),
                          payload.get("input") or payload.get("arguments") or ""))
        elif kind in ("custom_tool_call_output", "function_call_output"):
            outputs[payload.get("call_id")] = json.dumps(payload.get("output"))
        elif kind == "message" and payload.get("role") == "assistant":
            for part in payload.get("content") or []:
                if isinstance(part, dict) and part.get("text"):
                    trace["texts"].append(part["text"])
        elif kind == "reasoning":
            for part in payload.get("summary") or []:
                if isinstance(part, dict) and part.get("text"):
                    trace["reasoning"].append(part["text"])
    for cid, name, text in calls:
        out = outputs.get(cid, "")
        codes = [int(c) for c in re.findall(r'exit_code\\*"?:\s*(-?\d+)', out)]
        if "Rejected" in out:
            outcome = "denied"
        elif "Script failed" in out or any(c != 0 for c in codes):
            outcome = "error"
        elif out:
            outcome = "success"
        else:
            outcome = "unknown"
        inner = re.findall(r"tools\.([a-z_]+)\(", text) or [name]
        for tool in inner:
            trace["tool_calls"].append(dict(name=tool, target="", outcome=outcome))
        for cmd in re.findall(r'cmd"?\s*:\s*"((?:[^"\\]|\\.)*)"', text):
            trace["commands"].append(dict(command=cmd[:300], exit=codes[0] if codes else None,
                                          outcome=outcome))
        for patched in re.findall(r"\*\*\* (?:Update|Add|Delete) File: ([^\n\\'\"]+)", text):
            _record_write(trace, patched.strip(), outcome)
    return trace


def _grok(path: Path, origin: Optional[Path] = None) -> dict:
    trace = _empty("grok")
    # The cwd is encoded in the folder *above* the session, so read it from
    # where the transcript came from, not from the private copy.
    folder = (origin or path).parent.name
    if folder.startswith("%2F"):
        from urllib.parse import unquote
        trace["cwd"] = unquote(folder)
    outcomes, decisions = {}, {}
    for event in _jsonl(path / "events.jsonl"):
        if event.get("type") == "tool_completed":
            outcomes[event.get("tool_call_id")] = event.get("outcome") or "unknown"
        elif event.get("type") == "permission_resolved":
            decisions.setdefault(event.get("tool_name"), []).append(event.get("decision"))
    first_user = True
    for row in _jsonl(path / "chat_history.jsonl"):
        kind = row.get("type")
        content = row.get("content")
        text = "".join(c.get("text", "") for c in content if isinstance(c, dict)) if isinstance(content, list) \
            else (content if isinstance(content, str) else "")
        if kind == "user" and first_user:
            first_user = False
            match = re.search(r"<user_rules.*?</user_rules>", text, re.S)
            if match:
                body = re.sub(r"<[^>]+>", " ", match.group(0))
                first = next((line.strip() for line in body.splitlines() if line.strip()), "")
                trace["injected_rules"].append(dict(
                    source="grok account user_rules",
                    sha256=hashlib.sha256(match.group(0).encode()).hexdigest(),
                    first_line=first[:160]))
        elif kind == "assistant":
            if text:
                trace["texts"].append(text)
            for call in row.get("tool_calls") or []:
                trace["tool_calls"].append(dict(name=call.get("name"), target=_target(call.get("arguments")),
                                                outcome=outcomes.get(call.get("id"), "unknown")))
        elif kind == "reasoning":
            trace["reasoning"].append(text or json.dumps(row)[:4000])
    for name, values in decisions.items():
        denied = sum(1 for v in values if v not in ("allow", "allow_once", "allowed", "approve"))
        if denied:
            trace["tool_calls"].append(dict(name=name, target="", outcome=f"denied x{denied}"))
    return trace


def _outside(paths, cwd, project_root) -> List[str]:
    anchors = [p for p in (cwd, str(project_root) if project_root else None) if p]
    anchors += [os.path.realpath(p) for p in anchors]
    found = []
    for path in paths:
        if any(path == a or path.startswith(a.rstrip("/") + "/") for a in anchors):
            continue
        if (path.startswith(_SYSTEM_PREFIXES) or path.rstrip("/") + "/" in _SYSTEM_PREFIXES
                or path in ("/tmp", "/opt", "/dev/null") or re.search(r"/bin/[\w.+-]+$", path)):
            continue
        if path not in found:
            found.append(path)
    return found[:40]


def trace_call(vendor: str, path: Path, project_root=None, origin=None) -> dict:
    """One call's trace from its transcript. Never raises."""
    try:
        if vendor == "grok":
            trace = _grok(Path(path), Path(origin) if origin else None)
        else:
            trace = {"claude": _claude, "codex": _codex}[vendor](Path(path))
    except Exception as exc:  # noqa: BLE001 -- a trace never fails a run
        trace = _empty(vendor)
        trace["error"] = f"{type(exc).__name__}: {exc}"[:200]
    for call in trace["tool_calls"]:
        name, target = call.get("name") or "", call.get("target") or ""
        if name in _READ_TOOLS and target:
            trace["files_read"].append(target)
        elif name in _WRITE_TOOLS and target:
            _record_write(trace, target, call.get("outcome"))
        elif name in _SHELL_TOOLS and target:
            trace["commands"].append(dict(command=target, exit=None, outcome=call.get("outcome")))
    # Requests the harness serves only as a whole reply: anything in the
    # reasoning, or in any assistant text but the last, went nowhere.
    unserved = trace["reasoning"] + trace["texts"][:-1]
    for text in unserved:
        for match in PROTOCOL.finditer(text or ""):
            start = max(0, match.start() - 60)
            trace["protocol_attempts"].append(dict(
                verb=match.group(1).split(":")[0].split("{")[0].split()[0].rstrip(":"),
                context=re.sub(r"\s+", " ", text[start:match.end() + 100])[:220]))
    mentioned = [c.get("target") or "" for c in trace["tool_calls"]]
    mentioned += [c.get("command") or "" for c in trace["commands"]]
    paths = [m.group(1) for text in mentioned for m in _ABS_PATH.finditer(text)]
    trace["outside_project"] = _outside(paths, trace.get("cwd"), project_root)
    trace["files_read"] = list(dict.fromkeys(trace["files_read"]))[:80]
    trace["files_written"] = list(dict.fromkeys(trace["files_written"]))[:80]
    trace["files_not_written"] = list(dict.fromkeys(trace["files_not_written"]))[:80]
    trace.pop("texts", None)
    trace.pop("reasoning", None)
    return trace


_FILE_TOOLS = _READ_TOOLS | _WRITE_TOOLS


def _program(command: str) -> str:
    try:
        words = shlex.split(command or "")
    except ValueError:
        words = (command or "").split()
    return os.path.basename(words[0]) if words else ""


def shareable(record: dict) -> dict:
    """The record with every free-text value removed: argument values,
    reasoning snippets and rule text stay in the owner-only detail file."""
    out = {k: v for k, v in record.items()
           if k not in ("tool_calls", "commands", "protocol_attempts", "injected_rules")}
    if "tool_calls" in record:
        out["tool_calls"] = [dict(name=c.get("name"), outcome=c.get("outcome"),
                                  **({"path": c["target"]} if c.get("name") in _FILE_TOOLS
                                     and c.get("target") and _ABS_PATH.fullmatch(c["target"]) else {}))
                             for c in record.get("tool_calls") or []]
        out["commands"] = [dict(program=_program(c.get("command")), exit=c.get("exit"),
                                outcome=c.get("outcome")) for c in record.get("commands") or []]
        out["protocol_attempts"] = [dict(verb=a.get("verb")) for a in record.get("protocol_attempts") or []]
        out["injected_rules"] = [dict(source=r.get("source"), sha256=r.get("sha256"))
                                 for r in record.get("injected_rules") or []]
    return out


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def build_traces(run_dir, invocations_path, project_root=None, roots=None) -> List[dict]:
    """Copy each invoked call's transcript into the run and write ``trace.jsonl``.

    Returns the shareable record per invoked call, joined to its ledger row. A
    call whose transcript cannot be found here is listed as unavailable, never
    silently dropped, and never read as "no tools ran": the session may have
    lived in a container home that was not kept.
    """
    run_dir = Path(run_dir)
    private = run_dir / "native-private"
    try:
        run_dir.chmod(0o700)
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        private.chmod(0o700)
    except OSError:
        pass
    roots = roots or session_roots()
    records = []
    for row in _jsonl(Path(invocations_path)):
        if not row.get("invoked"):
            continue
        vendor = vendor_of(row.get("requested_model"))
        sid = row.get("session_id")
        record = dict(invocation_id=row.get("invocation_id"), task=row.get("task"), role=row.get("role"),
                      model=row.get("requested_model"), session_id=sid, outcome=row.get("outcome"),
                      seconds=row.get("seconds"), input_tokens=row.get("input_tokens"),
                      cached_input_tokens=row.get("cached_input_tokens"),
                      output_tokens=row.get("output_tokens"), model_turns=row.get("model_turns"),
                      prompt_artifact=row.get("prompt_artifact"))
        source = locate(vendor, sid, roots)
        if source is None:
            record["transcript"] = ("unavailable in this environment" if sid
                                    else "no session id recorded")
        else:
            try:
                copied = _private_copy(source, run_dir / "native-private" / vendor / source.name)
                record["transcript"] = str(copied.relative_to(run_dir))
                record.update(trace_call(vendor, copied, project_root, origin=source))
            except Exception as exc:  # noqa: BLE001 -- a trace never fails a run
                record["transcript"] = f"copy failed: {type(exc).__name__}"
        records.append(record)
    shared = [shareable(r) for r in records]
    try:
        _write_private(private / "trace-detail.jsonl", "".join(json.dumps(r) + "\n" for r in records))
        _write_private(run_dir / "trace.jsonl", "".join(json.dumps(r) + "\n" for r in shared))
    except OSError:
        pass
    return shared


def _tool_summary(calls: List[dict]) -> str:
    counts: Dict[str, List[int]] = {}
    for call in calls:
        slot = counts.setdefault(call.get("name") or "?", [0, 0, 0])
        slot[0] += 1
        outcome = str(call.get("outcome") or "")
        if outcome.startswith("denied"):
            slot[2] += 1
        elif outcome not in ("success", "unknown"):
            slot[1] += 1
    parts = []
    for name, (n, failed, denied) in sorted(counts.items(), key=lambda kv: -kv[1][0]):
        extra = ", ".join(x for x in (f"{failed} failed" if failed else "", f"{denied} denied" if denied else "") if x)
        parts.append(f"{name} {n}" + (f" ({extra})" if extra else ""))
    return ", ".join(parts) or "none"


def render_timeline(records: List[dict]) -> str:
    """The report's call-by-call view: what each call did, in order."""
    if not records:
        return "## Call timeline\n\nNo invoked calls."
    lines = ["## Call timeline", "",
             "One line per call, from the ledger and the vendor's own session transcript "
             "(copied privately to `native-private/`; per-call detail in `trace.jsonl`).", ""]
    for r in records:
        cached = r.get("cached_input_tokens")
        tokens = (f"{r.get('input_tokens') or 0:,} in"
                  + (f" ({cached:,} cached)" if isinstance(cached, int) else "")
                  + f", {r.get('output_tokens') or 0:,} out")
        head = (f"- **{r.get('task')} {r.get('role')}** {r.get('model')}: {r.get('outcome')}, "
                f"{round(r.get('seconds') or 0)} s, {tokens}"
                + (f", {r['model_turns']} turns" if isinstance(r.get("model_turns"), int) else ""))
        lines.append(head)
        if r.get("tool_calls") is None:
            lines.append(f"  - transcript: {r.get('transcript')}; what this call did is unknown here")
            continue
        lines.append(f"  - tools: {_tool_summary(r.get('tool_calls') or [])}")
        if r.get("files_written"):
            lines.append(f"  - wrote: {', '.join(r['files_written'][:12])}")
        if r.get("files_not_written"):
            lines.append("  - write attempted, not confirmed (denied, failed or no result): "
                         f"{', '.join(r['files_not_written'][:12])}")
        if r.get("outside_project"):
            lines.append(f"  - **outside the project:** {', '.join(r['outside_project'][:8])}")
        if r.get("protocol_attempts"):
            verbs = {}
            for a in r["protocol_attempts"]:
                verbs[a["verb"]] = verbs.get(a["verb"], 0) + 1
            lines.append("  - **requests written mid-session, never served:** "
                         + ", ".join(f"{v} x{n}" for v, n in verbs.items()))
        for rule in r.get("injected_rules") or []:
            lines.append(f"  - injected: {rule['source']} (sha256 {str(rule.get('sha256'))[:12]})")
    return "\n".join(lines)
