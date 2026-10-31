"""Recovery decisions and the improvement loop (2026-10-31).

A live run stopped after two of eight task slots because one reviewer
reached its round cap: the no-verdict review was filed as a bare open
string nothing could hold, recheck or settle (PR #73, 6544825528). This
module is the deterministic half of the common recovery lifecycle that
replaced that route; the session decides and acts, this module only
classifies, measures and describes.

* **An occurrence record** (``RV<scope>-<n>``) is written by a production
  path when an operation does not give the outcome its caller needed: the
  operation and seat, the source it was bound to, the outcome class, what
  was already done (the failed call's invocation identity and measured
  cost, the observed tool facts that passed the allowlist below), the prior
  attempts on the same identity, the unresolved question, the eligible next
  actions with the allowance actually left, and the decision (the action
  taken, or the stop and why). Its result is filled in when the action
  settles. It records a decision; it holds no debt. The debt stays where the
  rest of the engine reads it (the review registry, the held entries, the
  typed facts), so this list can never say "recovered" while an owned
  obligation is open: ``result`` is written from the registry after the
  action, not from the action's return.
* **The current job's memory** is the same list: an identity already tried
  on the same source is not tried again.
* **Proposals** aggregate occurrences by failure signature into a
  human-readable recommendation with its evidence, measured cost (unknown
  stays null), a falsifiable regression scenario and a status. They are
  proposals: correlated outcomes are not a confirmed cause, nothing here is
  adopted into the engine, and no model call writes them.

Nothing from a model's text is promoted into a prompt here. Observed tool
facts pass an explicit allowlist (``safe_observations``): known tool names,
known outcomes, validated project-relative file paths that exist in the
project, counts. Search patterns, command text, out-of-project paths,
narration and reasoning never pass (root's context audit on 1dbbf9f, L2:
``trace.shareable`` keeps grep patterns in ``files_read``).
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional

#: Outcome classes, with what each means. ``protective`` classes are stops
#: that protect the operator (a write whose effect is unknown, a denied
#: permission, an integrity fault, a cancellation): they are recorded and
#: never retried. ``unknown`` keeps its evidence and names a reconciliation.
OUTCOME_CLASSES: Dict[str, str] = {
    "review_capped": "a required review stopped at its round cap before giving a verdict",
    "review_unfinished": "a required review reply arrived without a valid verdict",
    "recheck_capped": "a recheck stopped at its round cap before giving a verdict",
    "recheck_unfinished": "a recheck reply arrived without a valid verdict",
    "design_review_capped": "a design review stopped at its round cap before giving a verdict",
    "verification_capped": "a security verification stopped at its round cap before giving a verdict",
    "judgment_refused_finish": "a required judgment call was refused to keep the held finish",
    "reviewer_unavailable": "the seat that owes a judgment was unavailable",
    "provider_timeout": "a call timed out; its usage and effect are unknown",
    "provider_quota": "a seat's subscription window was exhausted",
    "provider_error": "a call failed in transport",
    "unknown_usage": "a call's usage could not be measured; the budget latched",
    "uncertain_write": "an editing call stopped and its writes are not known",
    "permission": "a capability or permission was denied",
    "integrity": "an integrity check failed (source, dependency tree, evidence or record)",
    "cancelled": "the run was cancelled",
    "capacity_exhausted": "the allowance left could not pay the next required step",
    "operator_input": "only the operator can answer what the run needs next",
    "stalled": "the planning loop did not converge",
    "stop_rule": "one of the run's own stop rules ended it (a failed or unattributable check, unverified "
                 "evidence, open findings, unmet requirements, a breaker)",
    "task_failed": "a task broke its own rules (scope, reply, channel, environment); its work was kept and "
                   "it was handed back",
    "unknown": "an outcome the harness could not classify",
}

PROTECTIVE = frozenset({"uncertain_write", "permission", "integrity", "cancelled"})

#: The actions a recovery decision can name. Each is an existing engine
#: route; none is a free retry.
ACTIONS: Dict[str, str] = {
    "complete_judgment": "the same seat completes its judgment once, with the observed context carried "
                         "(the job's judgment-completion allowance, claimed before the call, never refunded)",
    "hold_owned_debt": "the judgment is held as owned review debt on the registry, with its owner, its source "
                       "and a settlement path; nothing completes over it",
    "owner_settlement": "the owner judges the debt on the current source (a RESOLVES or CONTINUES task's "
                        "settlement, the post-merge recheck, or the one attempt at a list's end or the cap)",
    "stop": "the run stops; the record keeps the evidence",
    "reconcile": "the outcome is uncertain: an operator reconciles it from the retained evidence before any "
                 "resume",
    "hand_back": "the task fails; its work is kept and the planner names the remaining work (CONTINUES); "
                 "nothing is retried",
}

#: Vendor CLI tool names the allowlist knows, by what they do. A read is a
#: tool that opened one file; a search takes a pattern, which never passes.
_READ_FILE = frozenset({"Read", "read_file", "view_file", "open_file", "NotebookRead"})
_SEARCH = frozenset({"Grep", "Glob", "LS", "grep", "glob", "list_dir", "search_files", "find_files"})
_SHELL = frozenset({"Bash", "run_terminal_command", "exec_command", "shell"})
_WRITE = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "write", "search_replace", "edit_file",
                    "apply_patch"})
_OUTCOMES = {"success": "success", "completed": "success", "ok": "success", "error": "error",
             "failed": "error", "failure": "error", "denied": "denied", "unknown": "unknown"}
_SAFE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}")


def _outcome(value) -> str:
    text = str(value or "unknown").strip().lower()
    if text.startswith("denied"):
        return "denied"
    return _OUTCOMES.get(text, "other")


def _project_path(target: str, cwd: Optional[str], root: Optional[Path]) -> Optional[str]:
    """``target`` as a project-relative path when it names an existing file
    in ``root``: absolute under the call's own working directory, or
    relative; nothing with ``..``, no character outside the safe set, never
    a pattern that happens not to be a file."""
    if root is None or not isinstance(target, str) or not target.strip():
        return None
    text = target.strip()
    if text.startswith("/"):
        if not cwd or not text.startswith(cwd.rstrip("/") + "/"):
            return None
        text = text[len(cwd.rstrip("/")) + 1:]
    if text.startswith("./"):
        text = text[2:]
    if not _SAFE_PATH.fullmatch(text) or ".." in PurePosixPath(text).parts:
        return None
    try:
        return text if (Path(root) / text).is_file() else None
    except OSError:
        return None


def safe_observations(trace: Optional[dict], *, project_root=None, unavailable: str = "") -> dict:
    """The allowlisted facts of one call's observed tool use, or an explicit
    ``unavailable`` reason. ``files_read`` lists only project files a file
    reader opened; searches and commands are counted, never quoted. A read
    is an observation of an attempt: it does not show the file was
    reviewed."""
    if not trace or unavailable:
        return dict(status="unavailable", reason=(unavailable or "no tool record was found for the call")[:200])
    if trace.get("error"):
        # The parser itself failed: its message is not repeated.
        kind = str(trace.get("error")).split(":", 1)[0][:60]
        return dict(status="unavailable", reason=f"the call's transcript could not be parsed ({kind})")
    integrity = trace.get("integrity") if isinstance(trace.get("integrity"), dict) else None
    if integrity is not None and not integrity.get("lines"):
        return dict(status="unavailable", reason="the call's transcript held no readable record")
    root = Path(project_root) if project_root else None
    cwd = trace.get("cwd") if isinstance(trace.get("cwd"), str) else None
    tools: Dict[str, Dict[str, int]] = {}
    read: List[str] = []
    searches = commands = writes = 0
    command_outcomes: Dict[str, int] = {}
    for call in trace.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        name = call.get("name") if isinstance(call.get("name"), str) else ""
        outcome = _outcome(call.get("outcome"))
        known = name if name in (_READ_FILE | _SEARCH | _SHELL | _WRITE) else "other"
        tools.setdefault(known, {}).setdefault(outcome, 0)
        tools[known][outcome] += 1
        if name in _READ_FILE and outcome == "success":
            path = _project_path(call.get("target"), cwd, root)
            if path and path not in read:
                read.append(path)
        elif name in _SEARCH:
            searches += 1
        elif name in _SHELL:
            commands += 1
            command_outcomes[outcome] = command_outcomes.get(outcome, 0) + 1
        elif name in _WRITE:
            writes += 1
    # ``observed`` only for a transcript read whole; anything less is
    # ``partial`` with the reason, and its counts are lower bounds. A
    # transcript whose completeness was never measured is not called whole.
    if integrity is None:
        status, reason = "partial", "the transcript's completeness was not measured"
    elif not integrity.get("complete"):
        parts = [f"{int(integrity.get('invalid') or 0)} of {int(integrity.get('lines') or 0)} line(s) unreadable"
                 if integrity.get("invalid") else "",
                 f"{int(integrity.get('missing_files') or 0)} expected file(s) missing"
                 if integrity.get("missing_files") else "",
                 "read stopped at its size bound" if integrity.get("truncated") else ""]
        status, reason = "partial", "; ".join(p for p in parts if p) or "the transcript was not read whole"
    else:
        status, reason = "observed", ""
    extra = {}
    progress = trace.get("progress") if isinstance(trace.get("progress"), dict) else None
    if progress is not None:
        extra["progress"] = _safe_progress(progress)
    return dict(status=status, **({"reason": reason} if reason else {}), **extra,
                tool_calls=sum(sum(v.values()) for v in tools.values()),
                tools={k: dict(v) for k, v in sorted(tools.items())}, files_read=read[:60],
                searches=searches, commands=commands, command_outcomes=dict(sorted(command_outcomes.items())),
                writes_attempted=writes)


_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,47}")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.+-]{8,32}")


def _safe_progress(progress: dict) -> dict:
    """A call's progress summary, re-validated at the record boundary:
    counts, short identifier labels and ISO times only."""
    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def seconds(value):
        return round(float(value), 3) if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and value >= 0 else None
    labels = {k: count(v) for k, v in (progress.get("labels") or {}).items()
              if isinstance(k, str) and _LABEL.fullmatch(k) and count(v) is not None} \
        if isinstance(progress.get("labels"), dict) else {}
    status = progress.get("status") if progress.get("status") in ("observed", "partial", "unavailable") else "unavailable"
    activity = progress.get("activity") if progress.get("activity") in (
        "tool_use", "reasoning", "other_events", "no_events") else None
    return dict(status=status, events=count(progress.get("events")), invalid=count(progress.get("invalid")),
                labels=dict(list(labels.items())[:12]), tool_events=count(progress.get("tool_events")),
                reasoning_events=count(progress.get("reasoning_events")),
                first_at=progress.get("first_at") if isinstance(progress.get("first_at"), str)
                and _ISO.fullmatch(progress["first_at"]) else None,
                last_at=progress.get("last_at") if isinstance(progress.get("last_at"), str)
                and _ISO.fullmatch(progress["last_at"]) else None,
                span_seconds=seconds(progress.get("span_seconds")),
                longest_gap_seconds=seconds(progress.get("longest_gap_seconds")), activity=activity)


def call_cost(event) -> Optional[dict]:
    """One outer invocation's identity and measured cost, from its
    ``InvocationEvent`` (the delegation ledger). Inner tool cycles are the
    call's ``model_turns``; a selection-only row is no attempt. Unknown
    usage or duration stays None, never zero."""
    if event is None or not getattr(event, "invoked", False):
        return None
    inp, out = getattr(event, "input_tokens", None), getattr(event, "output_tokens", None)
    known = isinstance(inp, int) and isinstance(out, int)
    return dict(invocation_id=getattr(event, "invocation_id", None), task=getattr(event, "task", None),
                role=getattr(event, "role", None), model=getattr(event, "requested_model", None),
                outcome=getattr(event, "outcome", None), turn_limited=bool(getattr(event, "turn_limited", False)),
                model_turns=getattr(event, "model_turns", None), max_turns=getattr(event, "max_turns", None),
                input_tokens=inp if isinstance(inp, int) else None,
                output_tokens=out if isinstance(out, int) else None,
                tokens=(inp + out) if known else None,
                seconds=getattr(event, "seconds", None) if isinstance(getattr(event, "seconds", None),
                                                                       (int, float)) else None,
                # Whether the CLI wrote to stderr, as a count: never its text.
                stderr_chars=len(getattr(event, "stderr_tail", "") or "") if isinstance(
                    getattr(event, "stderr_tail", ""), str) else None,
                session_known=bool(getattr(event, "session_id", None)),
                operation=getattr(event, "operation", None) if isinstance(getattr(event, "operation", None), str)
                else None)


def costs(records: Iterable[dict]) -> dict:
    """Measured cost of the calls the records name, each invocation counted
    once however many records name it; unknown usage is counted, not
    zeroed."""
    seen: Dict[str, dict] = {}
    unidentified = 0
    # A cause the budget named whose ledger row was not found is still a call
    # that happened: counted once per operation id, unidentified, never
    # absent and never zero, unless another record names its row.
    orphan_ops = set()
    records = [r for r in records if isinstance(r, dict)]
    for record in records:
        causes = [c for c in record.get("causes") or [] if isinstance(c, dict)]
        for cause in causes:
            if not isinstance(cause.get("call"), dict):
                if cause.get("operation"):
                    orphan_ops.add(str(cause["operation"]))
                else:
                    unidentified += 1
        for call in ([record.get("failed_call")] + [c.get("call") for c in causes]
                     + [a.get("call") for a in record.get("attempts") or []]):
            if not isinstance(call, dict):
                continue
            key = call.get("invocation_id")
            if not key:
                unidentified += 1
                continue
            seen.setdefault(key, call)
    named_ops = {str(c.get("operation")) for c in seen.values() if c.get("operation")}
    unidentified += len(orphan_ops - named_ops)
    known = [c for c in seen.values() if isinstance(c.get("tokens"), int)]
    timed = [c for c in seen.values() if isinstance(c.get("seconds"), (int, float))]
    return dict(calls=len(seen), tokens_known=sum(c["tokens"] for c in known) if known else None,
                calls_with_unknown_usage=len(seen) - len(known), seconds_known=round(sum(c["seconds"] for c in timed), 3)
                if timed else None, calls_with_unknown_duration=len(seen) - len(timed),
                unidentified_calls=unidentified)


def signature(record: dict) -> str:
    """A family key: outcome class, the seat's role and its vendor. Two
    occurrences with the same key are the same family; anything else is
    kept apart."""
    vendor = str(record.get("seat") or "").split(":", 1)[0] or "-"
    return f"{record.get('outcome_class')}|{record.get('role') or '-'}|{vendor}"


#: The durable correction each family proposes, and the regression scenario
#: that would show it held. Proposals, not findings: a recurring outcome
#: says where to look, never what caused it.
_PROPOSALS: Dict[str, tuple] = {
    "review_capped": (
        "Reviews of this kind spend their round budget before the verdict. Candidates to evaluate: lead the "
        "review prompt with the harness's measured receipts and the changed-file list so the seat need not "
        "re-derive them; review the effort and tool settings of this seat for review roles; keep the owned-debt "
        "route as the backstop.",
        "A review that reaches its round cap with task slots left: the run continues, the same seat completes "
        "its judgment once with its observed reads carried, and either the review settles or an owned "
        "unreviewed record is held that DONE and the cap never complete over."),
    "review_unfinished": (
        "Review replies of this kind arrive without the required verdict form. Candidate: restate the closing "
        "marker and the NO FINDINGS form at the end of the review prompt as well as in its body.",
        "An arrived review reply with no verdict: one completion; failing that, owned review debt, never "
        "a clean review and never a bare open string."),
    "recheck_capped": (
        "Rechecks of this kind reach the round cap. Candidate: carry the finding's own evidence and the "
        "revision's changed lines into the recheck so it judges the named finding without re-investigating.",
        "A recheck that caps: the finding stays open under its own id, held, settled only by its author."),
    "recheck_unfinished": (
        "Recheck replies of this kind give no per-id verdict. Candidate: show the expected id lines as a "
        "template at the end of the recheck prompt.",
        "A recheck reply without per-id verdicts: one completion; the finding stays open and held."),
    "design_review_capped": (
        "Design reviews of this kind reach the round cap. Candidate: hand the renders and the declared state "
        "first and ask for the verdict before any further inspection.",
        "A capped design review: owned review debt on the design judgment, never a design defect or approval."),
    "verification_capped": (
        "Security verifications of this kind reach the round cap. Candidate: bound the verifier's "
        "investigation to the excursion's own report and diff.",
        "A capped verification: the verification edge stays unset and owned debt is held."),
    "judgment_refused_finish": (
        "A required judgment could not be paid while the finish was held. Candidate: size tasks so their "
        "reviews fit before the final slot, or raise the operator's limits for this job size.",
        "With the finish held and no room for a judgment, the judgment is owed debt and the run reaches its "
        "finish incomplete, never a completed run."),
    "provider_timeout": (
        "Calls of this kind time out. Candidate: compare the timeout with this seat's measured durations; "
        "reconcile the timed-out call's effect before any resume.",
        "A timed-out call: the run stops protectively, the record names the call and what must be reconciled."),
    "provider_quota": (
        "This seat's subscription window ran out. Candidate: spread the seat's load or schedule the job when "
        "the window has room; the engine does not substitute another vendor.",
        "An exhausted window: the stop names the seat and the window; no silent fallback."),
    "unknown_usage": (
        "A call ended with usage the harness could not measure. Candidate: find why the envelope carried no "
        "usage for this vendor and role.",
        "Unknown usage latches the budget; the record keeps the call for reconciliation."),
}


def proposals(records: Iterable[dict]) -> List[dict]:
    """Occurrences aggregated by family into reviewable proposals. Each says
    what was observed, what was tried and with what result, the measured
    cost with its unknowns, the suggested durable change and the scenario
    that would show it held. Status is always ``proposed``: adoption is a
    reviewed engine change, never this record."""
    families: Dict[str, List[dict]] = {}
    for record in records:
        if not isinstance(record, dict) or not record.get("outcome_class"):
            continue
        families.setdefault(signature(record), []).append(record)
    out = []
    for key, group in sorted(families.items()):
        cls = group[0]["outcome_class"]
        results = [str(r.get("result") or "pending") for r in group]
        recovered = sum(1 for r in results if r.startswith("recovered"))
        held = sum(1 for r in results if r.startswith("held"))
        stopped = sum(1 for r in results if r.startswith(("stopped", "stop")))
        change, scenario = _PROPOSALS.get(cls, (
            "No durable correction is proposed for this class yet; the occurrences are kept as evidence.",
            "The outcome is recorded with its evidence and routed as the class's route says."))
        evidence = (f"{len(group)} occurrence(s): {recovered} recovered, {held} held as owned debt, "
                    f"{stopped} stopped, {len(group) - recovered - held - stopped} other")
        out.append(dict(
            family=key, outcome_class=cls, meaning=OUTCOME_CLASSES.get(cls, ""),
            occurrences=[r.get("id") for r in group], tasks=sorted({str(r.get("task")) for r in group}),
            # Only what was actually tried: a refused or unavailable action is
            # an attempt record, not an attempt.
            attempted=sorted({str(a.get("action")) for r in group for a in r.get("attempts") or []
                              if not str(a.get("outcome") or "").startswith("not attempted")}),
            results=results, evidence=evidence, cost=costs(group),
            recommendation=change, regression_scenario=scenario,
            confidence=("observed once; the cause is not confirmed" if len(group) == 1 else
                        f"observed {len(group)} times in this run; recurrence, not a confirmed cause"),
            status="proposed: not adopted (a reviewed engine change adopts it, never this record)"))
    return out


def render(records: List[dict], props: List[dict]) -> str:
    """The report section: each occurrence in one line, then each proposal."""
    if not records:
        return ""
    lines = ["## Recovery and improvement record", "",
             "Occurrences (harness record; costs from the invocation ledger, unknown stays unknown):", ""]
    for r in records:
        call = r.get("failed_call") or {}
        tokens = call.get("tokens")
        lines.append(f"- {r.get('id')}: {r.get('task')} {r.get('role')} by {r.get('seat') or '-'}: "
                     f"{r.get('outcome_class')}; decided {r.get('decision') or '-'}; result "
                     f"{r.get('result') or 'pending'}"
                     + (f"; failed call {tokens:,} tokens" if isinstance(tokens, int) else
                        ("; failed call usage unknown" if call else "")))
    lines += ["", "Proposals (not adopted; recurrence is not a confirmed cause):", ""]
    for p in props:
        cost = p["cost"]
        lines.append(f"- {p['family']}: {p['evidence']}. {p['recommendation']} Regression scenario: "
                     f"{p['regression_scenario']} Cost: {cost['calls']} call(s), "
                     + (f"{cost['tokens_known']:,} tokens known" if cost["tokens_known"] is not None
                        else "tokens unknown")
                     + (f", {cost['calls_with_unknown_usage']} with unknown usage" if cost["calls_with_unknown_usage"]
                        else "") + f". {p['confidence']}. Status: {p['status']}.")
    return "\n".join(lines) + "\n"
