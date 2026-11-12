"""Contests between two drafts of one task, and the scoreboard.

The session engine had collaboration and no competition: one lead drafted,
the others critiqued, and nothing recorded whose work held up (whole-repo
review, 2026-11-10, design sections 1 and 2). A contest is the first place
two models attempt the same task. It is opt-in (``SessionConfig.compete``),
and off is the old run.

What a contest is, in the engine's own terms:

* two senior seats from different vendors draft the same task blind, each in
  its own private copy of the project;
* the winner is decided by measured facts in a fixed order (:data:`RULES`);
  a judge from a third vendor is asked only when the measurements tie, or
  when one draft changed no file and so cannot be measured against the other
  (:func:`one_unchanged`). The judge comes from the brain trust, which has
  two vendors since Grok left it on 2026-11-01, and both are drafting: no
  judge can be seated today, the record says "no verdict", and the tie goes
  to the smaller diff, then the routed lead;
* only the winner's measured changes are merged into the project, and the
  task then continues through the ordinary review, checks and close-out.

This module holds the parts that need no session: the draft's measured
facts, the winner decision, the judge's prompt and the reading of its reply,
the record for ``result.json``, and the scoreboard file. Running a contest
is ``Session._contest_draft``.

A contest result is never evidence that work is verified. The check that
counts is still the task's own gate on the merged project.

The scoreboard (``.quadratus/scoreboard.jsonl``) is a record only. Nothing
in the engine reads it to route, decide or prompt: it is written once when a
run ends and read by ``tools/scoreboard.py`` and by people. A table that fed
routing would be a model roster by another name, and those are seeds here,
never truth.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import List, Optional, Tuple

from .verdicts import unstyled

__all__ = [
    "COMPETE_MODES",
    "JUDGE_ROLE",
    "ROUTED_STOP",
    "RULES",
    "append_scoreboard",
    "decide_measured",
    "decide_tied",
    "draft_record",
    "gate_facts",
    "judge_prompt",
    "labels_for",
    "ledger_line",
    "one_unchanged",
    "read_winner",
    "scoreboard_rows",
    "usable",
]

#: ``SessionConfig.compete``. "complex" contests a task routed at COMPLEX
#: difficulty; "off" is the run made before contests existed.
COMPETE_MODES = ("off", "complex")

#: The invocation role of the judge's one call.
JUDGE_ROLE = "judge"

#: What can decide a contest, in the order each is tried. The first that
#: separates the two drafts decides and is recorded as ``decided_by``.
RULES = ("completed", "scope", "gate", "judge", "size", "default")

#: ``decided_by`` when no rule was tried: the routed lead's own draft call
#: ended in a policy refusal, a denied capability or a permission block. Those stop a single-lead
#: run and no re-plan repairs them, so the run stops the same way here,
#: whatever the challenger delivered. No draft is merged in the routed
#: lead's place; the challenger's stays on record as an artifact.
ROUTED_STOP = "routed lead's stop"

#: Gate outcomes on a draft's copy, best first.
_GATE_RANK = {"passed": 2, "failed": 1, "not_run": 0}

_ROLES = ("routed", "challenger")


def labels_for(task_id: str) -> dict:
    """Which draft is shown as Draft A: ``{"routed": "A" or "B", ...}``.

    Decided from a hash of the task id, so the order tells a judge nothing
    about which draft the router picked, and a test can know it."""
    first = int(hashlib.sha256(str(task_id).encode("utf-8")).hexdigest(), 16) % 2 == 0
    return {"routed": "A" if first else "B", "challenger": "B" if first else "A"}


def gate_facts(result, *, reason: str = "") -> dict:
    """A gate run on a draft's copy as the facts the decision compares.

    ``status`` is passed, failed or not_run. A run that established no
    verdict (nothing configured, a runner that could not start, a timeout, a
    tree that moved under it) is not_run with the reason: it says nothing
    for the draft and nothing against it. ``failing_commands`` counts the
    required commands that did not pass; ``failed_cases`` is the structured
    reports' failed and errored cases, or None when a failing command gave
    no parsed report."""
    if result is None:
        return dict(status="not_run", failing_commands=None, failed_cases=None,
                    reason=reason or "no check ran on this copy")
    receipts = [r for r in (getattr(result, "receipts", None) or ()) if getattr(r, "required", True)]
    if getattr(result, "passed", False):
        return dict(status="passed", failing_commands=0, failed_cases=0, reason="")
    if receipts:
        failing = [r for r in receipts if r.status not in ("passed", "skipped")]
        judged = [r for r in failing if r.status == "failed"]
        reports = [getattr(r, "report", None) for r in judged]
        why = "; ".join(f"{r.id}: {r.status}: {r.reason}" for r in failing)[:300]
    else:
        # A single-command gate: no return code means it never finished.
        judged = failing = [result] if getattr(result, "returncode", None) is not None else []
        reports = [getattr(result, "report", None)] if judged else []
        why = "" if judged else str(getattr(result, "output", "") or "")[:300]
    if not judged:
        return dict(status="not_run", failing_commands=None, failed_cases=None,
                    reason=why or reason or "no command reached a verdict")
    counted = [(r or {}).get("counts") for r in reports if (r or {}).get("state") == "parsed"]
    cases = (sum(int(c.get("failed", 0)) + int(c.get("errors", 0)) for c in counted)
             if len(counted) == len(reports) and counted else None)
    return dict(status="failed", failing_commands=len(failing), failed_cases=cases, reason=why)


def usable(draft: dict) -> bool:
    """A draft that could become the task's draft: its call returned a
    delivery and its measured changes are inside the task's scope."""
    return bool(draft.get("completed")) and draft.get("in_scope") is True


def one_unchanged(routed: dict, challenger: dict) -> bool:
    """Whether exactly one of two drafts was measured as changing no file.

    Such a pair is not compared on the gate or on size. A check that passes
    on unchanged source is the starting source's result, and a diff of no
    lines is not the smaller attempt: on either rule, doing nothing would
    beat real work without anyone having judged it. Whether the task needed
    a change at all is a judgement, so the judge is asked, and with no
    verdict the routed lead's draft stands, as it would have alone."""
    return (routed.get("changed") == []) != (challenger.get("changed") == [])


def decide_measured(routed: dict, challenger: dict) -> Tuple[Optional[str], str]:
    """Rules 1 to 3: completed, scope, gate.

    Returns ``(winner, decided_by)``. ``winner`` is "routed" or
    "challenger"; ``(None, "")`` means the two are equal on all three and
    both usable (or the gate does not apply: :func:`one_unchanged`), so the
    judge is next; ``(None, "none usable")`` means neither draft can become
    the task's draft, and the task takes the path the routed lead's own
    result takes.

    A draft that is not usable never wins, whatever it measures: a contest
    never ends worse than the routed lead alone would have."""
    a, b = usable(routed), usable(challenger)
    if not a and not b:
        return None, "none usable"
    if a != b:
        other = challenger if a else routed
        return ("routed" if a else "challenger"), ("completed" if not other.get("completed") else "scope")
    if one_unchanged(routed, challenger):
        return None, ""
    ga, gb = routed.get("gate") or {}, challenger.get("gate") or {}
    ranks = _GATE_RANK.get(ga.get("status"), 0), _GATE_RANK.get(gb.get("status"), 0)
    if ranks[0] != ranks[1]:
        return ("routed" if ranks[0] > ranks[1] else "challenger"), "gate"
    if ga.get("status") == "failed":
        for key in ("failing_commands", "failed_cases"):
            left, right = ga.get(key), gb.get(key)
            if left is not None and right is not None and left != right:
                return ("routed" if left < right else "challenger"), "gate"
    return None, ""


def decide_tied(routed: dict, challenger: dict, judged: Optional[str]) -> Tuple[str, str]:
    """Rules 4 to 6 for two usable drafts equal on the measurements: the
    judge's verdict when it gave one (``judged`` is "routed", "challenger"
    or None), then the smaller measured diff, then the routed lead. Size is
    a rule between two drafts that both changed something
    (:func:`one_unchanged`)."""
    if judged in _ROLES:
        return judged, "judge"
    left, right = routed.get("changed_lines"), challenger.get("changed_lines")
    if (isinstance(left, int) and isinstance(right, int) and left != right
            and not one_unchanged(routed, challenger)):
        return ("routed" if left < right else "challenger"), "size"
    return "routed", "default"


#: What the judge's reply is held to, stated in its prompt and read by
#: :func:`read_winner`. One sentence, so the rule cannot drift from the reader.
JUDGE_RULE = (
    "End your reply with exactly one line, 'WINNER: A' or 'WINNER: B', and before it one line "
    "of reason. That closing line is all that is read. A reply with no such line, with both, "
    "with a condition or anything else after the letter (WINNER: A if the tests pass), or "
    "with two WINNER lines is no verdict: the choice is then made without you, by the smaller "
    "diff when both attempts changed something. You are asked once; there is no second round."
)

#: What the judge is told the measurements showed. Stated from the facts of
#: the pair in front of it, so the prompt never claims a comparison that was
#: not made.
_MEASURED_EQUAL = ("Both stayed inside the task's scope and measured the same on the project's checks, so "
                   "the checks do not separate them.")
_MEASURED_ONE_UNCHANGED = (
    "Both stayed inside the task's scope. One of them changed no file, so the project's checks were "
    "not used to separate them: a check that passes on unchanged source says nothing for that "
    "attempt. First decide whether the task needed a change at all, from the task and the source in "
    "your working directory.")

_WINNER = re.compile(r"WINNER\s*:\s*(.*)$", re.IGNORECASE)


def judge_prompt(*, packet: str, task_text: str, draft_a: str, draft_b: str, budget_note: str = "",
                 unchanged: bool = False) -> str:
    """The judge's whole prompt. ``draft_a`` and ``draft_b`` are measured
    diff blocks already bounded and fenced by the caller; ``packet`` is the
    reviewer's role packet, which carries the task's scope. ``unchanged``
    says one of the two changed no file (:func:`one_unchanged`)."""
    return (
        packet + "\n\n"
        f"Task: {task_text}\n\n"
        "Two attempts at this task were made separately, each from the same starting source, "
        "by authors who did not see each other's work. The harness measured each as a diff. "
        + (_MEASURED_ONE_UNCHANGED if unchanged else _MEASURED_EQUAL)
        + " Choose the one that better does the task as written: "
        "correct first, then complete against the task's acceptance, then simpler. Judge only "
        "what the diffs show. Your working directory holds the project as it was before either "
        "attempt; neither is applied there, so do not run the project's checks and do not edit "
        "anything.\n\n"
        "## Draft A" + draft_a + "\n\n"
        "## Draft B" + draft_b + "\n\n"
        + JUDGE_RULE + budget_note
    )


def read_winner(reply: str) -> Optional[Tuple[str, str]]:
    """``(letter, reason)`` from a judge's reply, or None for no verdict.

    A verdict is the reply's last line that says something, reading exactly
    WINNER: A or WINNER: B under any formatting or case, with no other
    WINNER line anywhere above it. The reason is the line before it. A
    condition after the letter, both letters, or a second WINNER line is no
    verdict: an acceptance counts only when it is said plainly."""
    lines = [unstyled(line) for line in str(reply or "").splitlines()]
    lines = [line for line in lines if re.search(r"\w", line)]
    if not lines:
        return None
    marked = [i for i, line in enumerate(lines) if _WINNER.match(line)]
    if marked != [len(lines) - 1]:
        return None
    said = _WINNER.match(lines[-1]).group(1).strip().rstrip(" .!").upper()
    if said not in ("A", "B"):
        return None
    reason = lines[-2] if len(lines) > 1 else ""
    return said, reason[:300]


def draft_record(role: str, label: str, entry: dict) -> dict:
    """One draft as ``result.json`` carries it: measured facts and how the
    draft call ended, never the draft's text."""
    gate = dict(entry.get("gate") or gate_facts(None, reason="not run: the draft was not usable"))
    stop = entry.get("error")
    return dict(
        label=label, role=role, model=entry.get("model"),
        completed=bool(entry.get("completed")), in_scope=entry.get("in_scope"),
        usable=usable(entry), ended=str(entry.get("ended") or ""),
        # What ended a draft that did not deliver, in the stop's own words:
        # for the challenger this record is the only place it is kept.
        error=f"{type(stop).__name__}: {str(stop)[:200]}" if isinstance(stop, BaseException) else "",
        gate=gate, changed=list(entry.get("changed") or []),
        changed_lines=entry.get("changed_lines"),
        reported_tokens=entry.get("reported_tokens"), seconds=entry.get("seconds"),
        diff_artifact=entry.get("diff_artifact"),
    )


def ledger_line(record: dict) -> str:
    """The contest in one line for the task's ledger entry: the harness's
    record of who drafted and what decided, never the lead's account."""
    if not record.get("formed"):
        return ""
    drafts = record.get("drafts") or []
    names = " and ".join(str(d.get("model")) for d in drafts)
    winner = record.get("winner_model")
    if not winner and record.get("decided_by") == ROUTED_STOP:
        return (f"contest: two blind drafts ({names}); the routed lead's own draft call ended in a "
                "refusal, a denied capability or a permission block, which stops the run as it does with one lead, so "
                "no draft was merged in its place")
    if not winner:
        return (f"contest: two blind drafts ({names}); neither was usable, so the routed "
                "lead's own result stood")
    # Said as it happened: a winning draft that changed no file merged nothing.
    took = "was merged" if record.get("merged") else "was taken and changed no file"
    return (f"contest: two blind drafts ({names}); {winner}'s {took}, decided by "
            f"{record.get('decided_by')}")


def scoreboard_rows(run_id: str, contests, reviewers) -> List[dict]:
    """The scoreboard lines one run adds: one per contest that formed and
    one per review, each carrying the run id."""
    rows = []
    for record in contests or ():
        if not isinstance(record, dict) or not record.get("formed"):
            continue
        rows.append(dict(
            type="contest", run=run_id, task=record.get("task"), kind=record.get("kind"),
            difficulty=record.get("difficulty"),
            drafts=[{k: d.get(k) for k in ("label", "role", "model", "completed", "in_scope",
                                           "changed_lines", "reported_tokens", "seconds")}
                    | {"gate": (d.get("gate") or {}).get("status")}
                    for d in record.get("drafts") or ()],
            winner=record.get("winner"), winner_model=record.get("winner_model"),
            decided_by=record.get("decided_by"),
            judge_model=(record.get("judge") or {}).get("model"),
        ))
    for record in reviewers or ():
        if isinstance(record, dict):
            rows.append(dict(type="review", run=run_id, **{
                k: record.get(k) for k in ("task", "model", "role", "complete", "blocking_raised",
                                           "blocking_resolved", "blocking_unresolved", "recheck")}))
    return rows


def append_scoreboard(path, rows) -> int:
    """Append ``rows`` to the scoreboard at ``path``, one JSON object per
    line, owner-only and in one write. Returns how many lines were added.
    Nothing is ever rewritten or removed."""
    rows = [row for row in rows or () if isinstance(row, dict)]
    if not rows:
        return 0
    from .safe_io import append_private
    append_private(path, "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows))
    return len(rows)
