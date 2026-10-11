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
                            truncated=truncated,
                            task=meta.get('task') if isinstance(meta.get('task'), str) else None))
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


#: Scoped styles for the Monitor panel. Colours come from the host theme's
#: variables where it has them, so light and dark both keep their contrast.
_STYLE = """<style>
.qm{--qm-ok:#1f7a3f;--qm-warn:#a15c00;--qm-bad:#b42318;--qm-idle:#5b6472;
  font-size:15px;line-height:1.5;color:var(--body-text-color,inherit);display:grid;gap:14px;max-width:960px}
.qm *{box-sizing:border-box}
.qm-banner{border-left:6px solid var(--qm-idle);border-radius:8px;padding:12px 16px;
  background:var(--block-background-fill,transparent);border-top:1px solid var(--border-color-primary,#d0d5dd);
  border-right:1px solid var(--border-color-primary,#d0d5dd);border-bottom:1px solid var(--border-color-primary,#d0d5dd)}
.qm-banner h2{margin:0;font-size:1.15em}
.qm-banner p{margin:4px 0 0}
.qm-running{border-left-color:var(--qm-warn)}.qm-stopped{border-left-color:var(--qm-bad)}
.qm-confirmed{border-left-color:var(--qm-ok)}
.qm-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.qm-card,.qm-reply{border:1px solid var(--border-color-primary,#d0d5dd);border-radius:8px;padding:10px 14px;
  background:var(--block-background-fill,transparent)}
.qm-card h3,.qm h3{margin:0 0 4px;font-size:.8em;letter-spacing:.04em;text-transform:uppercase;opacity:.8}
.qm-card .qm-big{font-size:1.1em;font-weight:600}
.qm-note{font-size:.88em;opacity:.85;margin:0}
.qm ol{margin:0;padding:0;list-style:none}.qm li{margin:3px 0}
.qm-now{margin-top:4px}.qm-now span{display:block;font-size:.85em;opacity:.8}
.qm time{font-variant-numeric:tabular-nums;opacity:.8;margin-right:6px}
.qm-replies{display:grid;gap:10px}
.qm-reply header{font-weight:600;display:flex;flex-wrap:wrap;gap:6px 10px}
.qm-reply header span{font-weight:400;opacity:.8}
.qm pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:6px 0 0;font-size:.9em}
.qm-excerpt{margin:6px 0 0;overflow-wrap:anywhere}
.qm details>summary{cursor:pointer;margin-top:6px}
.qm details>summary:focus-visible{outline:2px solid currentColor;outline-offset:2px}
@media (max-width:600px){.qm{font-size:14px}.qm-cards{grid-template-columns:1fr}}
</style>"""


def _excerpt(text, limit=200):
    """The first plain paragraph of a reply, short; the full text stays one
    click away. Never generated: cut from the saved reply itself."""
    summary = re.search(r'^SUMMARY:\s*(.+)$', text, re.MULTILINE)
    excerpt = summary[1] if summary else text.split('```', 1)[0].strip()
    excerpt = (excerpt or 'The reply contains code or test output. Open it to read.').split('\n\n', 1)[0]
    excerpt = ' '.join(excerpt.split())
    return excerpt[:limit - 1] + '…' if len(excerpt) > limit else excerpt


def _active_calls(events):
    active = {}
    for event in events:
        call_id = event.get('call_id')
        if not isinstance(call_id, str):
            continue
        if event.get('kind') == 'call-started':
            active[call_id] = event
        elif event.get('kind') == 'call-ended':
            active.pop(call_id, None)
    return list(active.values())


def render_story(status):
    """The Monitor panel as HTML: one status banner, three cards (progress,
    checks, usage), the latest activity, the saved team replies as short
    cards each opening to the full text, then every technical fact
    collapsed. Read-only: no model call, no generated summary; every saved
    string is escaped. The banner never says more than the saved result
    does: complete only when the engine recorded it, otherwise not
    confirmed."""
    root = status.get('run_dir')
    run_dir = Path(root) if root and root != UNKNOWN else None
    result, events, replies = None, [], []
    if run_dir:
        result, _ = _read_json(run_dir / 'result.json')
        events, _ = _tail_jsonl(run_dir / 'activity.jsonl')
        replies = _read_replies(run_dir)
    finished = status.get('finished') is True
    terminal = status.get('terminal_status')
    tone = 'idle'
    if finished:
        title, tone = 'Run ended — goal not confirmed', 'stopped'
        if terminal == 'complete':
            title, tone = 'Run ended — engine reports the goal complete', 'confirmed'
        elif terminal == 'listed tasks complete':
            title = 'Listed tasks finished — overall goal not checked'
    elif not run_dir:
        title = 'Ready to watch — no saved run yet'
    else:
        title, tone = 'Run has not finished — showing recorded activity', 'running'
    goal = (result or {}).get('goal') or next((e.get('goal') for e in events if e.get('kind') == 'run-started'), None)
    banner = [f'<h2>{_safe(title)}</h2>']
    if goal:
        banner.append(f'<p><strong>Task:</strong> {_safe(str(goal).splitlines()[0].partition(". ")[0][:200])}</p>')
    if finished:
        reason = str(status.get('stop_reason', ''))
        match = re.search(r'timed out after (\d+)s', reason)
        if match:
            banner.append(f'<p><strong>What stopped it:</strong> A test did not finish after '
                          f'{int(match[1]) // 60} minutes. The run stopped without confirming the goal. '
                          'Changes and evidence were saved.</p>')
        elif terminal not in ('complete', 'listed tasks complete'):
            banner.append('<p><strong>What stopped it:</strong> The run could not confirm completion. '
                          'The recorded reason is in Technical details.</p>')
    out = [_STYLE, '<div class="qm">',
           f'<section class="qm-banner qm-{tone}" role="status" aria-live="polite">{"".join(banner)}</section>']

    # Progress: what is in flight now, or what was saved.
    active = _active_calls(events) if run_dir and not finished else []
    if active:
        now = ''.join(f'<div class="qm-now">{_safe(_activity_line(e))}<span>since {_safe(_time(e.get("at")))}</span></div>'
                      for e in active[-3:])
        progress = f'<div class="qm-big">In progress</div>{now}'
    elif finished and isinstance((result or {}).get('tasks'), int):
        progress = (f'<div class="qm-big">{result["tasks"]} tasks closed</div>'
                    '<p class="qm-note">Closed tasks do not prove the whole goal is met.</p>')
    elif run_dir and events:
        latest = next((e for e in reversed(events) if _activity_line(e)), None)
        progress = ('<div class="qm-big">No call in progress</div>' + (
            f'<p class="qm-note">Last recorded: {_safe(_activity_line(latest))} · {_safe(_time(latest.get("at")))}</p>'
            if latest else ''))
    elif run_dir:
        progress = ('<div class="qm-big">Unknown</div><p class="qm-note">This run predates the live activity '
                    'feed; a call in progress is not visible until it returns.</p>')
    else:
        progress = '<div class="qm-big">No run yet</div>'
    checks = (result or {}).get('checks')
    last = checks[-1] if isinstance(checks, list) and checks and isinstance(checks[-1], dict) else None
    if last is not None:
        check = ('<div class="qm-big">' + ('Passed' if last.get('passed') is True else 'Did not pass') + '</div>'
                 '<p class="qm-note">Latest test result. Earlier passes do not replace it.</p>')
    else:
        check = '<div class="qm-big">No saved result yet</div><p class="qm-note">Test results are saved when the run ends.</p>'
    calls, tokens = status.get('calls'), status.get('tokens_reported')
    if isinstance(tokens, int):
        usage = (f'<div class="qm-big">{tokens:,} tokens</div><p class="qm-note">'
                 + (f'{calls} recorded calls. ' if isinstance(calls, int) else '')
                 + 'Updates after each call returns.</p>')
    else:
        usage = '<div class="qm-big">Unknown</div><p class="qm-note">No usage recorded yet.</p>'
    out += ['<div class="qm-cards">',
            f'<section class="qm-card" aria-label="Progress"><h3>Progress</h3>{progress}</section>',
            f'<section class="qm-card" aria-label="Checks"><h3>Checks</h3>{check}</section>',
            f'<section class="qm-card" aria-label="Usage"><h3>Usage</h3>{usage}</section>', '</div>']

    out.append('<section aria-label="Activity"><h3>Activity</h3>')
    visible = [(e, _activity_line(e)) for e in events if _activity_line(e)]
    if visible:
        out.append('<ol>' + ''.join(f'<li><time>{_safe(_time(e.get("at")))}</time>{_safe(text)}</li>'
                                    for e, text in visible[-6:]) + '</ol>')
        if len(visible) > 6:
            out.append('<details><summary>Earlier activity</summary><ol>' + ''.join(
                f'<li><time>{_safe(_time(e.get("at")))}</time>{_safe(text)}</li>' for e, text in visible[:-6])
                + '</ol></details>')
    else:
        out.append('<p class="qm-note">No live activity journal was saved for this run.</p>')
    if run_dir and not finished:
        out.append('<p class="qm-note">Replies appear when each call finishes. Activity records are not a '
                   'heartbeat; completion comes only from the saved result.</p>')
    out.append('</section>')

    out.append('<section aria-label="Team discussion"><h3>Team discussion</h3>'
               '<p class="qm-note">Saved replies, newest first. They are the team’s own claims, separate from '
               'the run result. Watching uses no model calls.</p><div class="qm-replies">')
    if not replies:
        out.append('<p>No team reply has been saved yet. Replies appear when each call returns.</p>')
    for reply in replies:
        label = {'draft': 'Build update', 'revision': 'Corrections'}.get(reply['kind'], 'Reply')
        if reply['kind'].startswith('review:'):
            label = 'Review'
        task = reply.get('task')
        out.append('<article class="qm-reply"><header>' + _safe(_model(reply['author']))
                   + f'<span>{_safe(label)}</span>' + (f'<span>{_safe(task)}</span>' if task else '')
                   + f'<span>{_safe(_time(reply["at"]))}</span></header>'
                   + f'<p class="qm-excerpt">{_safe(_excerpt(reply["text"]))}</p>'
                   + '<details><summary>Read the full reply</summary><pre>' + _safe(reply['text']) + '</pre>'
                   + ('<p class="qm-note">Shown up to 6,000 characters; the whole reply is in the saved run '
                      'files.</p>' if reply['truncated'] else '') + '</details></article>')
    out.append('</div></section>')
    out.append('<details><summary>Technical details, recorded errors and full report</summary><pre>'
               + _safe(render_markdown(status)) + '</pre></details>')
    out.append('</div>')
    return '\n'.join(out)
