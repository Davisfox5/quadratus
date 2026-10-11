"""Plain-language, read-only view of recorded activity and public team replies.

No model calls, transcript reads, or interpretation of private reasoning.
Activity is a last-recorded observation, not proof that a process is alive.
"""

import html
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .monitor import (
    MAX_ARTIFACT_ENTRIES,
    UNKNOWN,
    _read_json,
    _read_regular,
    _tail_jsonl,
    render_markdown,
)

ROLE_LABELS = {
    'orchestrator': 'Planning the next step', 'requirements-review': 'Reviewing the plan',
    'lead': 'Building the change', 'direct': 'Building the change',
    'collaborator': 'Reviewing the change', 'verifier': 'Checking the fix',
    'revision': 'Making corrections', 'design-review': 'Reviewing screenshots',
    'design-fix': 'Correcting the interface', 'gate-fix': 'Correcting a failed test',
    'closeout': 'Summarizing the work', 'auditor': 'Checking the requirements',
}
PUBLIC_KINDS = {'draft', 'revision', 'monitor-reply', 'direct-closeout'}


def _safe(text):
    # Render saved model text as text, never executable HTML or Markdown links.
    return html.escape(str(text), quote=True)


def _model(key):
    key = str(key)
    vendor = key.split(':', 1)[0]
    return {'claude': 'Claude', 'openai': 'Codex', 'grok': 'Grok'}.get(vendor, 'Team member')


def _time(at):
    try:
        return datetime.fromisoformat(str(at).replace('Z', '+00:00')).astimezone().strftime('%I:%M:%S %p %Z').lstrip('0')
    except (TypeError, ValueError, OverflowError):
        return 'Time unavailable'


def _read_replies(run_dir):
    """Recent public output only. Never prompts, private transcripts, or paths from metadata."""
    store = run_dir / 'artifacts'
    if store.is_symlink():
        return []
    candidates = []
    try:
        with os.scandir(store) as entries:
            for i, entry in enumerate(entries):
                if i >= MAX_ARTIFACT_ENTRIES:
                    break
                if not re.fullmatch(r'[0-9a-f]{12}(?:\.[0-9a-f]{12})?\.json', entry.name):
                    continue
                if entry.is_file(follow_symlinks=False):
                    candidates.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
    except OSError:
        return []
    replies, seen = [], set()
    for at, path in sorted(candidates, reverse=True)[:200]:
        meta, problem = _read_json(path, max_bytes=16 * 1024)
        if problem or not meta:
            continue
        kind = meta.get('kind', '')
        if not isinstance(kind, str):
            continue
        if kind not in PUBLIC_KINDS and not (isinstance(kind, str) and kind.startswith('review:')):
            continue
        artifact = path.name[:12]
        if artifact in seen:
            continue
        seen.add(artifact)
        body = store / (artifact + '.txt')
        if body.is_symlink():
            continue
        try:
            text = _read_regular(body, 6000)[0].decode('utf-8', errors='replace')
        except OSError:
            continue
        try:
            truncated = body.stat().st_size > 6000
        except OSError:
            continue
        replies.append(dict(at=datetime.fromtimestamp(at, timezone.utc).isoformat(),
                            author=meta.get('author'), kind=kind, text=text,
                            truncated=truncated))
        if len(replies) == 8:
            break
    return replies


def _activity_line(event):
    kind = event.get('kind')
    actor = _model(event.get('model'))
    action = ROLE_LABELS.get(str(event.get('role', '')), 'Working on the task')
    task = str(event.get('task', ''))
    suffix = f' · {_safe(task)}' if task and task != 'run' else ''
    if kind == 'call-started':
        return f'{actor}: {action.lower()}{suffix}'
    if kind == 'call-ended':
        return f'{actor}: call ended{suffix} (this does not mean the task passed)'
    if kind == 'check-started':
        return f'Running the project tests{suffix}'
    if kind == 'check-ended':
        return f'Test command ended{suffix}; its result is recorded separately'
    if kind == 'reply':
        return f'{actor}: reply saved{suffix}'
    if kind == 'stage':
        return {'draft': 'Building the change', 'review': 'Reviewing the work',
                'gate': 'Running the tests', 'capture': 'Capturing the interface',
                'close-out': 'Preparing the task summary'}.get(str(event.get('stage', '')))
    if kind == 'run-started':
        return 'Run started'
    return None


def render_story(status):
    """Human summary first; original technical facts remain available below."""
    root = status.get('run_dir')
    run_dir = Path(root) if root and root != UNKNOWN else None
    result, events, replies = None, [], []
    if run_dir:
        result, _ = _read_json(run_dir / 'result.json')
        events, _ = _tail_jsonl(run_dir / 'activity.jsonl')
        replies = _read_replies(run_dir)
    finished = status.get('finished') is True
    terminal = status.get('terminal_status')
    if finished:
        title = 'Run ended — goal not confirmed'
        if terminal == 'complete':
            title = 'Run ended — engine reports the goal complete'
        elif terminal == 'listed tasks complete':
            title = 'Listed tasks finished — overall goal not checked'
    elif not run_dir:
        title = 'Ready to watch — no saved run yet'
    else:
        title = 'Run has not finished — showing recorded activity'
    lines = [f'## {title}', '']
    goal = (result or {}).get('goal') or next((e.get('goal') for e in events if e.get('kind') == 'run-started'), None)
    if goal:
        first = str(goal).splitlines()[0].partition('. ')[0][:200]
        lines += [f'**Task:** {_safe(first)}', '']
    if finished:
        reason = str(status.get('stop_reason', ''))
        if 'timed out after' in reason:
            match = re.search(r'timed out after (\d+)s', reason)
            duration = f' after {int(match[1]) // 60} minutes' if match else ''
            lines += [f'**What stopped it:** A test did not finish{duration}. The run stopped without confirming the goal. Changes and evidence were saved.', '']
        elif terminal not in ('complete', 'listed tasks complete'):
            lines += ['**What stopped it:** The run could not confirm completion. The recorded reason is in Technical details below.', '']
        if result:
            tasks = result.get('tasks')
            if isinstance(tasks, int):
                lines += [f'**Progress saved:** {tasks} tasks closed. Closed tasks do not prove the whole goal is met.', '']
            checks = result.get('checks')
            if isinstance(checks, list) and checks:
                last = checks[-1]
                if isinstance(last, dict):
                    lines += [f'**Latest test result:** {"Passed" if last.get("passed") is True else "Did not pass"}. Earlier passes do not replace the final result.', '']
    elif run_dir:
        active = {}
        for event in events:
            call_id = event.get('call_id')
            if not isinstance(call_id, str):
                continue
            if event.get('kind') == 'call-started':
                active[call_id] = event
            elif event.get('kind') == 'call-ended':
                active.pop(call_id, None)
        if active:
            for event in list(active.values())[-4:]:
                lines += [f'**Last recorded:** {_activity_line(event)} · started {_time(event.get("at"))}', '']
        elif events:
            latest = next((e for e in reversed(events) if _activity_line(e)), None)
            if latest:
                lines += [f'**Last recorded:** {_activity_line(latest)} · {_time(latest.get("at"))}', '']
        else:
            lines += ['This run predates the live activity feed. Its saved replies appear below, but a call in progress is not visible until it returns.', '']
        lines += ['Activity records are not a heartbeat: an interrupted process may leave an unfinished record. Completion comes from the saved result.', '']
    calls, tokens = status.get('calls'), status.get('tokens_reported')
    if isinstance(tokens, int):
        lines += [f'**Usage so far:** {tokens:,} reported tokens' + (f' · {calls} recorded calls' if isinstance(calls, int) else '') + '. Usage updates after calls return.', '']
    lines += ['### Activity', '']
    visible = [(e, _activity_line(e)) for e in events if _activity_line(e)]
    if visible:
        lines += [f'- {_time(e.get("at"))} — {text}' for e, text in visible[-12:]]
    else:
        lines += ['No live activity journal was saved for this older run.']
    lines += ['', '### Team discussion', '',
              'Saved team replies, newest first. These are the team’s own claims; the run result above is separate. Watching uses no extra model calls.', '']
    if not replies:
        lines += ['No team reply has been saved yet. Replies appear when each call returns.']
    for reply in replies:
        label = {'draft': 'Build update', 'revision': 'Corrections'}.get(reply['kind'], 'Reply')
        if reply['kind'].startswith('review:'):
            label = 'Review'
        summary = re.search(r'^SUMMARY:\s*(.+)$', reply['text'], re.MULTILINE)
        excerpt = summary[1] if summary else reply['text'].split('```', 1)[0].strip()
        excerpt = (excerpt or 'The reply contains code or test output. Open the saved reply to read it.').split('\n\n', 1)[0]
        excerpt = excerpt[:397] + '…' if len(excerpt) > 400 else excerpt
        lines += [f'**{_model(reply["author"])} · {label} · {_time(reply["at"])}**', '',
                  '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + _safe(excerpt) + '</pre>', '']
        lines += ['<details><summary>Read this saved reply</summary>', '',
                  '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + _safe(reply['text']) + '</pre>',
                  'Excerpt; full reply remains in the saved run files.' if reply['truncated'] else '', '</details>', '']
    lines += ['<details><summary>Technical details and recorded errors</summary>', '', render_markdown(status), '', '</details>']
    return '\n'.join(lines)
