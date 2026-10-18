"""Plain-language, read-only view of recorded activity and public team replies.

No model calls, transcript reads, or interpretation of private reasoning.
Activity is a last-recorded observation, not proof that a process is alive.
"""

import html
import json
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
    'revision': 'Making corrections', 'recheck': 'Checking the corrections', 'design-review': 'Reviewing screenshots',
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
        replies.append(dict(artifact=artifact, at=datetime.fromtimestamp(at, timezone.utc).isoformat(),
                            author=meta.get('author'), kind=kind, text=text,
                            truncated=truncated,
                            task=meta.get('task') if isinstance(meta.get('task'), str) else None))
        if len(replies) == 8:
            break
    return replies


def _plain_work(text, limit=180):
    """Shorten saved public task text; never infer an agent's private work."""
    lines = [line.strip() for line in str(text).splitlines()
             if line.strip() and not re.match(r'^(?:COVERS|RESOLVES|HYPOTHESIS|CHANGED|FETCH|COMPLETE|NOT RUN|SUMMARY):', line.strip())]
    text = lines[0] if lines else ''
    text = re.sub(r'`[^`]*browser[^`]*\.(?:js|py)`', 'the browser test', text)
    text = re.sub(r'`[^`]*[/\\][^`]*`', 'the test file', text)
    for old, new in [('deterministic assertion mismatch', 'wrong expected result'),
                     ('deterministic', 'repeatable'), ('assertion', 'expected result'),
                     ('harness gate', 'test runner'), ('prescribed validation', 'required tests')]:
        text = text.replace(old, new)
    text = text.replace('`', '')
    text = text.replace('test harness', 'test setup')
    text = text.replace('Reproduce and fix the hang in the browser test.', 'Find and fix why the browser test gets stuck.')
    if text.startswith('Changed the browser readiness wait to count exactly three'):
        text = 'Updated the browser test to wait until all three project cards are ready.'
    elif text.startswith('Fixed the browser timeout by replacing') and 'exactly three project cards' in text:
        text = 'Updated the browser test to wait until all three project cards are ready.'
    elif text.startswith('The ascending-state check now expects the status text the app actually shows'):
        text = 'Updated the browser test to expect the message the app displays.'
    text = ' '.join(text.split())
    text = re.split(r'(?<=[.!?])\s', text, maxsplit=1)[0]
    return text[:limit - 1] + '…' if len(text) > limit else text


def _task_work(events, result):
    work = {}
    for event in events:
        match = re.match(r'^task (\d+):\s*(.*)', str(event.get('message', '')), re.DOTALL)
        if match:
            detail = _plain_work(match[2])
            if detail:
                work['t' + match[1]] = detail
    current = (result or {}).get('in_flight')
    if isinstance(current, dict) and isinstance(current.get('task'), str):
        detail = _plain_work(current.get('description', ''))
        if detail:
            work[current['task']] = detail
    return work


def _step(task):
    match = re.fullmatch(r't(\d+)', str(task))
    return f'Step {match[1]}' if match else (str(task) if task and task != 'run' else '')


def _activity_line(event, work=None, reply_text=None, check_text=None):
    kind = event.get('kind')
    actor = _model(event.get('model'))
    action = ROLE_LABELS.get(str(event.get('role', '')), 'Working on the task')
    task = str(event.get('task', ''))
    detail = (work or {}).get(task)
    suffix = f' · {_step(task)}' if _step(task) else ''
    subject = f' — {detail}' if detail else suffix
    if kind == 'call-started':
        return f'{actor}: {action.lower()}{subject}'
    if kind == 'call-ended':
        return f'{actor}: finished {action.lower()}{subject}'
    if kind == 'check-started':
        return f'Testing the changes{subject}'
    if kind == 'check-ended':
        return f'Tests finished: {check_text or "waiting for the saved result"}{suffix}'
    if kind == 'reply':
        return f'{actor} reported: {_excerpt(reply_text)}' if reply_text else f'{actor}: shared an update{subject}'
    if kind == 'stage':
        label = {'draft': 'Preparing the change', 'review': 'Reviewing the work',
                 'gate': 'Testing the changes', 'capture': 'Taking screenshots',
                 'close-out': 'Preparing the summary', 'closeout': 'Preparing the summary'}.get(str(event.get('stage', '')))
        return label + subject if label else None
    if kind == 'run-started':
        goal = _plain_work(event.get('goal', ''))
        return 'Started: ' + goal if goal else 'Run started'
    if kind == 'note':
        message = str(event.get('message', ''))
        missing = re.match(r'^task (t\d+): lead reported NOT RUN:', message)
        if missing:
            return f'{_step(missing[1])}: a requested check could not be run'
        failed = re.match(r'^task (t\d+): failed check recorded', message)
        if failed:
            return f'{_step(failed[1])}: tests failed; planning a correction'
        if 'RunBudgetExceeded' in message and message.startswith('call ended:'):
            return 'Usage limit reached; the run is stopping'
        if 'FinishReserved' in message and message.startswith('call ended:'):
            return 'Ordinary work stopped to keep room for the goal check and audit'
    return None


def _check_summary(check):
    receipts = check.get('receipts') if isinstance(check, dict) else None
    totals = dict(passed=0, failed=0, errors=0, skipped=0)
    counted = False
    if isinstance(receipts, list):
        for receipt in receipts:
            report = receipt.get('report') if isinstance(receipt, dict) else None
            counts = report.get('counts') if isinstance(report, dict) else None
            if isinstance(counts, dict):
                for name in totals:
                    value = counts.get(name)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        totals[name] += value
                        counted = True
    if counted:
        failed = totals['failed'] + totals['errors']
        return f'{totals["passed"]} passed · {failed} failed · {totals["skipped"]} skipped'
    return None


def _saved_checks(run_dir):
    """Only public check output; retain bounded, regular-file-only reads."""
    store = run_dir / 'artifacts'
    if store.is_symlink():
        return []
    candidates = []
    try:
        with os.scandir(store) as entries:
            for i, entry in enumerate(entries):
                if i >= MAX_ARTIFACT_ENTRIES:
                    break
                if re.fullmatch(r'[0-9a-f]{12}\.json', entry.name) and entry.is_file(follow_symlinks=False):
                    candidates.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
    except OSError:
        return []
    checks = []
    for at, path in sorted(candidates, reverse=True)[:200]:
        meta, problem = _read_json(path, max_bytes=16 * 1024)
        if problem or not meta or meta.get('kind') != 'check-output' or meta.get('author') != 'harness':
            continue
        try:
            body = store / (path.stem + '.txt')
            if body.is_symlink():
                continue
            raw, _ = _read_regular(body, 32 * 1024 + 1)
            if len(raw) > 32 * 1024:
                continue
            text = raw.decode('utf-8', errors='replace')
        except OSError:
            continue
        # Count each runner independently: Node succeeding cannot hide pytest failing.
        clean = re.sub(r'\x1b\[[0-9;]*m', '', text)
        node_pass = re.findall(r'(?:# pass |ℹ pass )(\d+)', clean)
        node_fail = re.findall(r'(?:# fail |ℹ fail )(\d+)', clean)
        totals = dict(passed=0, failed=0, skipped=0)
        counted = False
        if node_pass and node_fail:
            totals['passed'] += sum(map(int, node_pass))
            totals['failed'] += sum(map(int, node_fail))
            counted = True
        outcome = r'\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|rerun|warnings?)'
        for line in clean.splitlines():
            if not re.fullmatch(r'\s*(?:=+\s*)?' + outcome + r'(?:, ' + outcome +
                                r')*(?: in [\d.]+s(?: \([^)]*\))?)?\s*=*\s*', line):
                continue
            for count, name in re.findall(r'(\d+) (passed|failed|errors?|skipped)\b', line):
                totals['failed' if name in ('error', 'errors') else name] += int(count)
                counted = True
        if counted:
            summary = f'{totals["passed"]} passed · {totals["failed"]} failed'
            if totals['skipped']:
                summary += f' · {totals["skipped"]} skipped'
            if ': failed:' in text and totals['failed'] == 0:
                summary += ' · test command did not pass'
        else:
            summary = 'did not pass' if ': failed:' in text else 'result details unavailable'
        checks.append(dict(at=at, summary=summary))
    return sorted(checks, key=lambda c: c['at'])


def _elapsed(budget):
    seconds = budget.get('elapsed_seconds')
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 0 <= seconds < 1e9:
        minutes, seconds = divmod(int(seconds), 60)
        return f'{minutes} min {seconds:02d} sec'
    return None


def _preserved_reply(run_dir, budget):
    """A returned public reply saved when a usage limit interrupted acceptance.

    Only the budget's named reply files, never a prompt or native transcript.
    A reply remains a team report; it cannot replace independent test receipts.
    """
    names = budget.get('preserved_responses')
    folder = run_dir / 'budget-responses'
    if not isinstance(names, list) or folder.is_symlink():
        return None
    for name in reversed(names[-20:]):
        if not isinstance(name, str) or not re.fullmatch(r'budget-responses/attempt-[1-9]\d{0,8}\.txt', name):
            continue
        path = run_dir / name
        if path.is_symlink():
            continue
        try:
            raw, _ = _read_regular(path, 6001)
            return dict(text=raw[:6000].decode('utf-8', errors='replace'),
                        truncated=len(raw) > 6000, kind='preserved-response',
                        author=None, artifact=name,
                        at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
        except OSError:
            continue
    return None


def _reported_results(reply):
    """Short, attributed test statements, never promoted to verified results."""
    rows = []
    for line in (reply or {}).get('text', '').splitlines():
        if not re.match(r'^\s*[-*]\s', line) or not re.search(r'\b\d+ passed\b', line):
            continue
        label = line.lstrip(' -*').split(':', 1)[0]
        if 'browser' in label.lower():
            label = 'Browser checks'
        elif 'node' in label.lower():
            label = 'Full JavaScript tests' if 'full' in label.lower() else 'JavaScript tests'
        elif 'pytest' in label.lower():
            label = 'Python tests'
        elif 'cleanup' in label.lower():
            label = 'Startup and cleanup tests'
        else:
            label = _plain_work(label, 80)
        counts = re.findall(r'\b\d+ (?:passed|failed|skipped)\b', line)
        rows.append(label + ': ' + ' · '.join(counts))
        if len(rows) == 4:
            break
    return rows


def _reported_changes(reply):
    match = re.search(r'^CHANGED:\s*(.+)$', reply['text'], re.MULTILINE)
    if not match:
        return None
    try:
        paths = json.loads(match[1])
    except (ValueError, TypeError):
        return None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return None
    labels = []
    for path in paths[:3]:
        if 'test' in path.lower() and 'browser' in path.lower():
            label = 'Browser tests'
        elif 'test' in path.lower():
            label = 'Tests'
        else:
            label = Path(path).name[:80]
        if label not in labels:
            labels.append(label)
    return ', '.join(labels) or None


def _reported_work(reply):
    # Some providers concatenate progress narration and the final answer.
    # Prefer a saved statement of a completed change to their opening intention.
    before_checks = re.split(r'^Checks that ran:', reply['text'], maxsplit=1, flags=re.MULTILINE)[0]
    statements = re.findall(r'(?:^|(?<=[.!?]))\s*([^.!?\n]+\bnow\b[^.!?\n]*[.!?])', before_checks)
    return _plain_work(statements[-1], 250) if statements else _reported_changes(reply)


def _run_outcome(status, budget, last):
    """Name what actually stopped the workflow, separately from test results."""
    terminal = status.get('terminal_status')
    if status.get('finished') is not True:
        return 'Run in progress', 'running', None
    if terminal == 'complete':
        return 'Completed — goal recorded as met', 'confirmed', 'Quadratus recorded the overall goal as complete.'
    if terminal == 'listed tasks complete':
        return 'Planned tasks finished — overall review not run', 'idle', 'The listed tasks finished. This run did not review the overall goal.'
    reason = str(status.get('stop_reason', ''))
    match = re.search(r'timed out after (\d+)s', reason)
    if match:
        return 'Stopped — a test took too long', 'stopped', f'A test did not finish after {int(match[1]) // 60} minutes.'
    stop = budget.get('stop_reason')
    reasons = {
        'reported_token_threshold': ('Stopped at token limit', 'The run used its allowed tokens and stopped before final review finished.'),
        'reported_token_reserve': ('Stopped at token limit', 'There were not enough allowed tokens left to start the next request.'),
        'wall_deadline': ('Stopped at time limit', 'The run reached its time limit.'),
        'call_limit': ('Stopped at request limit', 'The run reached its allowed number of requests.'),
        'api_cost_threshold': ('Stopped at spending limit', 'The run reached its spending limit.'),
    }
    if isinstance(stop, str) and stop in reasons:
        title, why = reasons[stop]
        return title, 'stopped', why
    if last is not None and last.get('passed') is False:
        return 'Stopped — tests did not pass', 'stopped', 'The last independent test check did not pass. The work was saved.'
    if last is not None and last.get('passed') is True:
        return 'Tests passed — final review unfinished', 'idle', 'The tests passed, but Quadratus did not record a completed overall review.'
    return 'Stopped before final review finished', 'idle', 'No completed overall review was saved. The detailed stopping reason is in Run records and full report.'


def _number(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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
.qm ol.qm-timeline{margin:0 !important;padding:0 !important;list-style:none !important}
.qm ol.qm-timeline>li{margin:3px 0 !important;padding:0 !important;list-style:none !important}
.qm-now{margin-top:4px}.qm-now span{display:block;font-size:.85em;opacity:.8}
.qm time{font-variant-numeric:tabular-nums;opacity:.8;margin-right:6px}
.qm-replies{display:grid;gap:10px}
.qm-reply header{font-weight:600;display:flex;flex-wrap:wrap;gap:6px 10px}
.qm-reply header span{font-weight:400;opacity:.8}
.qm pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:6px 0 0;font-size:.9em}
.qm-excerpt{margin:6px 0 0;overflow-wrap:anywhere}
.qm-summary{border:1px solid var(--border-color-primary,#d0d5dd);border-radius:8px;padding:12px 16px}
.qm-summary p{margin:6px 0;overflow-wrap:anywhere}
.qm details>summary{cursor:pointer;margin-top:6px}
.qm details>summary:focus-visible{outline:2px solid currentColor;outline-offset:2px}
@media (max-width:600px){.qm{font-size:14px}.qm-cards{grid-template-columns:1fr}}
</style>"""


def _excerpt(text, limit=200):
    """The first plain paragraph of a reply, short; the full text stays one
    click away. Never generated: cut from the saved reply itself."""
    text = str(text)
    if '**NOT RUN:**' in text and ('no shell tool' in text or 'no Bash' in text):
        return 'Could not run the tests because this reviewer could not run commands.'
    if re.fullmatch(r'FETCH:\s*[0-9a-f]{12}', text.strip()):
        return 'Asked to read saved results before choosing the next step.'
    if text.strip() == 'RESOLVED':
        return 'Reported that its earlier review findings are resolved.'
    summary = re.search(r'^SUMMARY:\s*(.+)$', text, re.MULTILINE)
    public = '\n'.join(line for line in text.splitlines() if not re.match(
        r'^[A-Z][A-Z _-]*:|^\[artifact |^#{1,6} ', line.strip()))
    excerpt = summary[1] if summary else public.split('```', 1)[0].strip()
    excerpt = (excerpt or 'The reply contains code or test output. Open it to read.').split('\n\n', 1)[0]
    excerpt = _plain_work(excerpt, limit=10000)
    if excerpt.startswith('the browser test was changed to discover Playwright'):
        excerpt = 'Updated how the browser test finds its tools, waits for the project list, and checks the result.'
    return excerpt[:limit - 1] + '…' if len(excerpt) > limit else excerpt


def _active_calls(events):
    """Calls and test commands recorded as started and not yet recorded as
    ended, in order. A record, not proof that a process is alive."""
    active = {}
    for event in events:
        if event.get('kind') == 'check-started':
            active['check:' + str(event.get('task', ''))] = event
            continue
        if event.get('kind') == 'check-ended':
            active.pop('check:' + str(event.get('task', '')), None)
            continue
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
    string is escaped. Test results and overall completion stay separate;
    returned team claims never replace independent test receipts."""
    root = status.get('run_dir')
    run_dir = Path(root) if root and root != UNKNOWN else None
    result, events, replies = None, [], []
    if run_dir:
        result, _ = _read_json(run_dir / 'result.json')
        if result is None and not (run_dir / 'result.json').exists():
            from .monitor import _terminal_result
            result, _ = _terminal_result(run_dir)
        events, _ = _tail_jsonl(run_dir / 'activity.jsonl')
        replies = _read_replies(run_dir)
    work = _task_work(events, result)
    from .jobstore import read_budget
    budget, _ = read_budget(run_dir) if run_dir else (None, None)
    budget = budget or {}
    preserved = _preserved_reply(run_dir, budget) if run_dir and status.get('finished') is True else None
    if preserved:
        replies = [preserved] + replies[:7]
    checks = (result or {}).get('checks')
    last = checks[-1] if isinstance(checks, list) and checks and isinstance(checks[-1], dict) else None
    summary = _check_summary(last)
    saved_checks = _saved_checks(run_dir) if run_dir else []
    reply_text = {r['artifact']: r['text'] for r in replies}
    def activity(event):
        summary = None
        if event.get('kind') == 'check-ended':
            try:
                at = datetime.fromisoformat(str(event.get('at')).replace('Z', '+00:00')).timestamp()
                match = next((c for c in saved_checks if 0 <= c['at'] - at <= 10), None)
                summary = match['summary'] if match else None
            except (ValueError, TypeError, OverflowError):
                pass
        return _activity_line(event, work, reply_text.get(event.get('artifact')), summary)
    finished = status.get('finished') is True
    terminal = status.get('terminal_status')
    title, tone, why = _run_outcome(status, budget, last)
    if not run_dir:
        title, tone = 'Ready to watch — no saved run yet', 'idle'
    elif not finished:
        title = 'Run has not finished — showing recorded activity'
    goal = (result or {}).get('goal') or next((e.get('goal') for e in events if e.get('kind') == 'run-started'), None)
    banner = [f'<h2>{_safe(title)}</h2>']
    if goal:
        banner.append(f'<p><strong>Task:</strong> {_safe(str(goal).splitlines()[0].partition(". ")[0][:200])}</p>')
    out = [_STYLE, '<div class="qm">',
           f'<section class="qm-banner qm-{tone}" role="status" aria-live="polite">{"".join(banner)}</section>']

    active = _active_calls(events) if run_dir and not finished else []
    if active:
        now = ''.join(f'<div class="qm-now">{_safe(activity(e))}<span>Started {_safe(_time(e.get("at")))}</span></div>'
                      for e in active[-3:])
        checking = any(e.get('kind') == 'check-started' for e in active[-3:])
        progress = f'<div class="qm-big">{"Testing the changes" if checking else "In progress"}</div>{now}'
    elif finished:
        progress = '<div class="qm-big">' + ('Finished' if terminal == 'complete' else 'Stopped') + '</div>'
        last_task = next((e for e in reversed(events) if e.get('kind') == 'call-started' and work.get(e.get('task'))), None)
        if last_task:
            progress += f'<p class="qm-note">Last work: {_safe(work[last_task["task"]])}</p>'
        elif _number((result or {}).get('tasks')):
            progress += f'<p class="qm-note">{result["tasks"]} steps closed</p>'
    elif run_dir and events:
        latest = next((e for e in reversed(events) if activity(e)), None)
        progress = '<div class="qm-big">Waiting for the next update</div>'
        if latest:
            progress += f'<p class="qm-note">{_safe(activity(latest))}</p>'
    else:
        progress = '<div class="qm-big">No activity yet</div>'

    testing = any(e.get('kind') == 'check-started' for e in active)
    if testing:
        check = '<div class="qm-big">Tests are running</div>'
    elif last is not None:
        check = '<div class="qm-big">' + ('Passed' if last.get('passed') is True else 'Did not pass') + '</div>'
        if preserved:
            check = '<div class="qm-big">Earlier independent check</div>'
    elif saved_checks:
        check = '<div class="qm-big">Latest test results</div>'
    else:
        check = '<div class="qm-big">Not tested yet</div>'
    if summary or saved_checks:
        check += f'<p class="qm-note">Last checked: {_safe(summary or saved_checks[-1]["summary"])}</p>'
    if last is not None:
        failures = []
        for receipt in last.get('receipts', []):
            report = receipt.get('report') if isinstance(receipt, dict) else None
            for failure in (report or {}).get('failures', []):
                name = str(failure.get('nodeid', '')).split('::')[-1]
                name = re.sub(r'at 1280x800', '(desktop)', name)
                name = re.sub(r'at 390x844', '(phone)', name)
                name = name.replace('restore-order browser review', 'Restore library order')
                if name:
                    failures.append(name)
        if failures:
            check += f'<p class="qm-note">{_safe(" · ".join(failures[:2]))}</p>'
    calls, tokens = status.get('calls'), status.get('tokens_reported')
    limits = budget.get('limits') if isinstance(budget.get('limits'), dict) else {}
    cap = limits.get('max_reported_tokens') or status.get('max_reported_tokens')
    if _number(tokens):
        usage = f'<div class="qm-big">{tokens:,} tokens</div>'
        if _number(cap) and cap > 0:
            allowance = f'{tokens - cap:,} over the {cap:,} token limit' if tokens > cap else f'{round(100 * tokens / cap)}% of {cap:,} token limit'
            usage += f'<p class="qm-note">{allowance}</p>'
    else:
        usage = '<div class="qm-big">Usage not reported yet</div>'
    details = []
    if _number(calls):
        max_calls = limits.get('max_calls')
        details.append(f'{calls} of {max_calls} requests' if _number(max_calls) else f'{calls} requests')
    elapsed = _elapsed(budget)
    if elapsed:
        details.append(elapsed)
    if details:
        usage += f'<p class="qm-note">{_safe(" · ".join(details))}</p>'
    out += ['<div class="qm-cards">',
            f'<section class="qm-card" aria-label="Progress"><h3>Current work</h3>{progress}</section>',
            f'<section class="qm-card" aria-label="Checks"><h3>Tests</h3>{check}</section>',
            f'<section class="qm-card" aria-label="Usage"><h3>Usage</h3>{usage}</section>', '</div>']

    if finished:
        out.append('<section class="qm-summary" aria-label="Run summary"><h3>Run summary</h3>')
        changed = (result or {}).get('source_changed')
        if changed is True:
            out.append('<p><strong>Work saved:</strong> Changes were saved in the project.</p>')
        elif changed is False:
            out.append('<p><strong>Work saved:</strong> No project changes were recorded.</p>')
        if preserved:
            # CHANGED is a team statement too; do not assume the file exists or a fix works.
            changes = _reported_work(preserved)
            if changes:
                out.append(f'<p><strong>Team reported changes:</strong> {_safe(changes)}</p>')
            reported = _reported_results(preserved)
            if reported:
                out.append('<p><strong>Final team test report:</strong> ' + '<br>'.join(map(_safe, reported)) + '</p>')
            else:
                out.append(f'<p><strong>Final team update:</strong> {_safe(_excerpt(preserved["text"]))}</p>')
        if last is not None:
            test_result = summary or ('Passed' if last.get('passed') is True else 'Did not pass')
            out.append(f'<p><strong>{"Earlier" if preserved else "Last"} independent check:</strong> {_safe(test_result)}</p>')
        elif saved_checks:
            out.append(f'<p><strong>Last saved test output:</strong> {_safe(saved_checks[-1]["summary"])}</p>')
        else:
            out.append('<p><strong>Tests:</strong> No independent test result was saved.</p>')
        out.append(f'<p><strong>{"Result" if terminal in ("complete", "listed tasks complete") else "Why it stopped"}:</strong> {_safe(why)}</p>')
        if preserved and terminal not in ('complete', 'listed tasks complete'):
            out.append('<p><strong>Still unfinished:</strong> The final team reply was saved when the run stopped. '
                       'Quadratus did not finish independently checking and accepting that reply. '
                       'Earlier checks do not verify changes described in that reply.</p>')
        out.append('</section>')

    out.append('<section aria-label="Activity"><h3>Activity</h3>')
    answered = {e.get('call_id') for e in events if e.get('kind') == 'reply'}
    visible = []
    for e in events:
        if e.get('kind') == 'call-ended' and e.get('call_id') in answered:
            continue
        if e.get('kind') == 'stage' and e.get('stage') in ('draft', 'review', 'gate', 'closeout', 'close-out'):
            continue
        text = activity(e)
        if text and (not visible or visible[-1][1] != text):
            visible.append((e, text))
    if visible:
        out.append('<ol class="qm-timeline">' + ''.join(f'<li><time>{_safe(_time(e.get("at")))}</time>{_safe(text)}</li>'
                                    for e, text in visible[-6:]) + '</ol>')
        if len(visible) > 6:
            out.append('<details><summary>Earlier activity</summary><ol class="qm-timeline">' + ''.join(
                f'<li><time>{_safe(_time(e.get("at")))}</time>{_safe(text)}</li>' for e, text in visible[:-6])
                + '</ol></details>')
    else:
        out.append('<p class="qm-note">No live activity journal was saved for this run.</p>')
    out.append('</section>')

    out.append('<section aria-label="Team discussion"><h3>Team discussion</h3>'
               '<div class="qm-replies">')
    if not replies:
        out.append('<p>No team reply has been saved yet. Replies appear when each call returns.</p>')
    for reply in replies:
        label = {'draft': 'Reported change', 'revision': 'Reported corrections',
                 'preserved-response': 'Final team update — saved at the limit'}.get(reply['kind'], 'Team update')
        if reply['kind'].startswith('review:'):
            label = 'Review update'
        task = reply.get('task')
        excerpt = (_reported_work(reply) or _excerpt(reply['text'])) if reply['kind'] == 'preserved-response' else _excerpt(reply['text'])
        out.append('<article class="qm-reply"><header>' + _safe('Team' if reply['kind'] == 'preserved-response' else _model(reply['author']))
                   + f'<span>{_safe(label)}</span>' + (f'<span>{_safe(_step(task))}</span>' if task else '')
                   + f'<span>{_safe(_time(reply["at"]))}</span></header>'
                   + f'<p class="qm-excerpt">{_safe(excerpt)}</p>'
                   + '<details><summary>' + ('Read saved reply' if reply['truncated'] else 'Read the full reply')
                   + '</summary><pre>' + _safe(reply['text']) + '</pre>'
                   + ('<p class="qm-note">Shown up to 6,000 characters; the whole reply is in the saved run '
                      'files.</p>' if reply['truncated'] else '') + '</details></article>')
    out.append('</div></section>')
    out.append('<details><summary>Run records and full report</summary>'
               '<p class="qm-note">Activity shows the last saved update. Team replies describe what agents reported; '
               'the Tests box shows results saved by the test runner. Tokens include cached input; actual cost is not shown.</p><pre>'
               + _safe(render_markdown(status)) + '</pre></details>')
    out.append('</div>')
    return '\n'.join(out)
