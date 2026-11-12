"""How the session engine reads what a model or the operator wrote: capture
declarations, fixture blocks, the directive lines of a task (COVERS,
RESOLVES, TIER, CONTINUES, HYPOTHESIS, DISCHARGES), requirements, labels
and metadata, FETCH, EVIDENCE and role packet requests, PROVES and RUNS
lines, typed findings and scenario corrections, review rows and verdict
lines, what a requirement says about errors and boundaries, and the
close-out.

Moved out of session.py unchanged on 2026-11-11; quadratus.session imports
every name here again. Verdicts are read in verdicts.py. The CONSULT
readers stay in session.py, because a test replaces ``_member_matches`` on
that module and they must see the replacement.
The move was redone on 2026-11-12, after the development line was merged
into this file, and took that line's code of the same kind with it.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple

from . import judgment
from . import verdicts as _verdicts
from .session_types import Complexity, _needs_lines, _scope_path
from .task_kinds import ROUTING, TaskKind
from .taskmeta import AmbiguousMetadata, TaskMetadata, lead_request, parse_metadata
from .verdicts import REVIEW_COMPLETE as _REVIEW_COMPLETE
from .verdicts import has_blocking_finding as _has_blocking_finding
from .verdicts import is_empty_note as _is_empty_note
from .verdicts import review_complete as _review_complete
from .verdicts import unstyled as _unstyled

#: The no-policy role packet's byte limit (``Session._role_packet``).
_FALLBACK_PACKET_BYTES = 20_000

#: A task's role packet may pass the policy's default only by an allowance
#: the orchestrator granted on its lead's justified request (Davis,
#: 2026-11-09: "the orchestrator should have latitude to extend that if the
#: lead can show justification"). The ceiling no grant can pass is the run's
#: own resource bound, not another fixed figure: the packet rides every round
#: of every call that carries it, so it may take at most this share of the
#: context one round of a capped call is planned to spend (the per-call token
#: threshold times ``run_budget.TURN_CAP_MARGIN`` over the lead turn cap, or
#: the measured ``run_budget.TURN_CONTEXT_TOKENS`` when the run sets neither),
#: at the meter's ``usage.CHARS_PER_TOKEN``. The other half is the round's own
#: reading, writing and tool output (``Session._packet_ceiling``).
_PACKET_ROUND_SHARE = 0.5
#: The calls whose every round re-sends a task's packet, for the budget
#: estimate of an extension: the lead's draft, one review and one recheck.
#: An estimate for the refusal only; what is charged is what the calls report.
_PACKET_CARRYING_CALLS = 3
#: The lead's request (at intake, or as its whole draft reply) and its two
#: justification lines, read by their markers only.
_PACKET_REQUEST_LINE = re.compile(r"^[ \t]*PACKET[ \t]+EXTENSION:[ \t]*([0-9][0-9,_]*)[ \t]*(?:bytes)?[ \t]*$",
                                  re.IGNORECASE | re.MULTILINE)
_PACKET_NECESSARY = re.compile(r"^[ \t]*NECESSARY:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)
_PACKET_WHY_NOT_SHORTER = re.compile(r"^[ \t]*WHY[ \t]+NOT[ \t]+SHORTER:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)
_PACKET_NARROW = re.compile(r"^[ \t]*PACKET[ \t]+NARROW:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)
#: The orchestrator's decision, a line of its own reply naming the request id
#: the harness issued: ``PACKET GRANT: <id> <bytes> | because: <reason>`` or
#: ``PACKET REFUSE: <id> | because: <reason>``.
_PACKET_DECISION = re.compile(r"^[ \t]*PACKET[ \t]+(GRANT|REFUSE):[ \t]*(\S*)[ \t]*(.*)$",
                              re.IGNORECASE | re.MULTILINE)
_PACKET_GRANT_TAIL = re.compile(r"^([0-9][0-9,_]*)[ \t]*(?:bytes)?[ \t]*\|[ \t]*because:[ \t]*(.*)$", re.IGNORECASE)
_PACKET_REFUSE_TAIL = re.compile(r"^\|?[ \t]*because:[ \t]*(.*)$", re.IGNORECASE)
#: A justification or a decision's reason shorter than this says nothing a
#: reader can check ("needed", "n/a"): form only, never a judgment of merit,
#: which is the orchestrator's.
_PACKET_REASON_MIN = 20
#: Intake requests per task whose send-back spends no correction.
_PACKET_FREE_REQUESTS = 2
_PACKET_EMPTY = ("none", "n/a", "na", "-", "needed", "required", "necessary")


def _packet_bytes(text: str) -> Optional[int]:
    try:
        return int(str(text).replace(",", "").replace("_", ""))
    except (TypeError, ValueError):
        return None


def _packet_reason_problem(text: str, what: str) -> str:
    text = (text or "").strip()
    if not text or text.lower().strip(" .") in _PACKET_EMPTY:
        return f"{what} is empty"
    if len(text) < _PACKET_REASON_MIN:
        return f"{what} says too little to be checked ({len(text)} characters; at least {_PACKET_REASON_MIN})"
    return ""


def _parse_packet_request(reply: str) -> dict:
    """A lead's answer to a packet question, read by its markers only:
    ``kind`` is "request" (``bytes``, ``necessary``, ``why``), "narrow"
    (``advice``) or "malformed" (``problem``). Prose without the markers is
    no request, and a reply carrying both forms, or a marker twice with
    different text, is malformed: nothing is guessed."""
    text = reply or ""
    sizes = list(dict.fromkeys(_PACKET_REQUEST_LINE.findall(text)))
    narrow = list(dict.fromkeys(m.strip() for m in _PACKET_NARROW.findall(text)))
    if sizes and narrow:
        return dict(kind="malformed", problem="the reply carries both a PACKET EXTENSION and a PACKET NARROW line")
    if narrow:
        if len(narrow) > 1:
            return dict(kind="malformed", problem="the reply carries PACKET NARROW twice with different text")
        problem = _packet_reason_problem(narrow[0], "the PACKET NARROW advice")
        return dict(kind="malformed", problem=problem) if problem else dict(kind="narrow", advice=narrow[0][:1200])
    if not sizes:
        return dict(kind="malformed", problem="the reply has no PACKET EXTENSION line")
    if len(sizes) > 1:
        return dict(kind="malformed", problem="the reply names more than one PACKET EXTENSION size")
    size = _packet_bytes(sizes[0])
    fields = {}
    for name, pattern, label in (("necessary", _PACKET_NECESSARY, "NECESSARY"),
                                 ("why", _PACKET_WHY_NOT_SHORTER, "WHY NOT SHORTER")):
        found = list(dict.fromkeys(m.strip() for m in pattern.findall(text)))
        if len(found) != 1:
            return dict(kind="malformed", problem=(f"the reply has no {label} line" if not found else
                                                   f"the reply carries {label} twice with different text"))
        problem = _packet_reason_problem(found[0], f"the {label} line")
        if problem:
            return dict(kind="malformed", problem=problem)
        fields[name] = found[0][:1200]
    if not size:
        return dict(kind="malformed", problem="the PACKET EXTENSION size is not a positive number of bytes")
    return dict(kind="request", bytes=size, **fields)


def _packet_allowance_record(grant: dict) -> dict:
    """A granted request as a task's record carries it."""
    return dict(request=grant.get("id"), origin=grant.get("origin"), granted=grant.get("granted"),
                default=grant.get("default"), needed=grant.get("needed"), ceiling=grant.get("ceiling"),
                lead=grant.get("lead"), necessary=grant.get("necessary"), why_not_shorter=grant.get("why_not_shorter"),
                decided_by=grant.get("decided_by"), reason=grant.get("grant_reason"),
                decision_artifact=grant.get("decision_artifact"), reply_artifact=grant.get("reply_artifact"))


def _read_packet_decisions(spec):
    """``(decisions, spec)``: the orchestrator's PACKET GRANT and PACKET
    REFUSE lines, taken out of the task text it named (they decide a request;
    they are not task text for its lead). Read here only, from the
    orchestrator's own reply: a lead's justification shown to it is never
    read as a decision."""
    text = getattr(spec, "description", "") or ""
    found = [dict(verb=m.group(1).upper(), id=m.group(2).strip(), rest=m.group(3).strip(), line=m.group(0).strip())
             for m in _PACKET_DECISION.finditer(text)]
    if not found:
        return [], spec
    kept = "\n".join(line for line in _PACKET_DECISION.sub("", text).splitlines() if line.strip())
    return found, replace(spec, description=kept)


#: How many facts one decomposition may add, and how long each may be. The map
#: is re-sent on every orchestrator and lead call for the rest of the run, so
#: an unbounded one would replace a one-off exploration cost with a permanent
#: per-call one -- which is the trade this whole change exists to avoid.
_MAX_ORIENT_NOTES = 4
_MAX_ORIENT_NOTE_CHARS = 180


_ROW_LINE = re.compile(r"^\s*O\d+\s*:\s*(?:SUFFICIENT|INSUFFICIENT)\b", re.IGNORECASE)


def _row_verdict(line: str) -> bool:
    """A row line with a verdict on it, as verdicts.row_line reads it."""
    return bool((_verdicts.row_line(line) or ("", ""))[1])


def _design_approved(verdict: str) -> bool:
    """APPROVED as the only verdict line: alone, or followed by nothing but
    per-obligation sufficiency lines (a design review judging the capture
    rows it was shown). Any other text is no approval."""
    lines = [line.strip() for line in (verdict or "").splitlines() if line.strip()]
    # A row line counts only as verdicts.py reads it: "O1: SUFFICIENT? No"
    # is other text, so the reply is no approval.
    return bool(lines) and lines[0] == "APPROVED" and all(_row_verdict(line) for line in lines[1:])


_CAPTURE_LINE = re.compile(r"^\s*CAPTURE\s*:\s*(.*\S)\s*$", re.IGNORECASE)
#: The marker wherever it sits on a line: series rule-3572b72 f5's lead wrote
#: prose and then ``load.CAPTURE: {"path": "/", ...}`` on the same line, and a
#: line-anchored read saw no declaration at all. Any case (the old contract),
#: and a marker is a candidate only when something declarable follows it.
_CAPTURE_MARKER = re.compile(r"CAPTURE\s*:", re.IGNORECASE)


def _capture_candidates(reply: str):
    """``(declarations, conflicts)`` across the whole reply. A declaration
    is marker-backed: a ``CAPTURE:`` marker (any case) followed by ``{`` or
    ``none``. Everything else that looks like an attempt is a conflict and
    never a declaration: a marker with any other tail or no tail at all
    (``Correction.capture: invalid``, ``Correction.CAPTURE:``), and a line
    outside the marker lines that starts with ``{`` (a second object, an
    unmarked example, an incomplete ``{"path":``). The harness reads exactly
    one declaration and only when nothing conflicts with it (Codex reviews
    of 351d3ba, 6844b97 and ada4c75)."""
    text = (reply or "").replace("`", "")
    declarations: List[str] = []
    conflicts: List[str] = []
    lines = text.splitlines()
    marker_lines = set()
    for index, line in enumerate(lines):
        for match in _CAPTURE_MARKER.finditer(line):
            marker_lines.add(index)
            tail = line[match.end():].strip()
            if tail.startswith("{") or re.match(r"none\b", tail, re.IGNORECASE):
                declarations.append(tail)
            else:
                conflicts.append(f"marker with no declaration after it: {line.strip()[:60]}")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index not in marker_lines and stripped.startswith("{"):
            conflicts.append(f"an object outside any CAPTURE: marker: {stripped[:60]}")
    return declarations, conflicts


def _capture_declaration(reply: str):
    """``(tail, "")`` for the one declaration candidate in ``reply``, or
    ``(None, why)``."""
    declarations, conflicts = _capture_candidates(reply)
    if not declarations and not conflicts:
        return None, "no CAPTURE: line in the reply"
    if len(declarations) + len(conflicts) > 1:
        return None, (f"{len(declarations)} capture declaration(s) and {len(conflicts)} conflicting line(s) "
                      "in the reply; one declaration is read, never a choice")
    if not declarations:
        return None, f"no usable CAPTURE: declaration ({conflicts[0]})"
    return declarations[0], ""


def _parse_capture_line(reply: str, task_id: str):
    """``(capture, "")`` from a CAPTURE: line, or ``(None, why)``.

    The same syntax as a SCOPE capture (``preview.validate_capture``), and a
    capture-only fixture must belong to this task. ``CAPTURE: none`` and a
    missing or malformed line both return None with the reason; the caller
    keeps the reviewer's verdict in that case.
    """
    line, why = _capture_declaration(reply)
    if line is None:
        return None, why
    if re.match(r"none\b", line, re.IGNORECASE):
        return None, "the lead declared that no page and steps reach the changed state"
    try:
        from .preview import validate_capture
        obj, end = json.JSONDecoder().raw_decode(line)
        trailing = line[end:].strip()
        if trailing and ("{" in trailing or "}" in trailing or "[" in trailing):
            return None, "more than one object after the CAPTURE: marker"
        capture = validate_capture(obj)
    except (ValueError, TypeError) as exc:
        return None, f"the CAPTURE line did not parse ({str(exc)[:120]})"
    for step in capture["steps"]:
        if step.get("action") == "file":
            parts = PurePosixPath(step["path"]).parts
            if parts[:2] == (".quadratus", "capture-fixtures") and (len(parts) < 3 or parts[2] != task_id):
                return None, f"a capture-only fixture must live under .quadratus/capture-fixtures/{task_id}/"
    return capture, ""


#: A well-formed header: the path alone, with or without its colon.
_FIXTURE_HEAD = re.compile(r"[ \t]*FIXTURE[ \t]+(\S+?):?[ \t]*")
#: A fence line: a run of three or more backticks, then anything (the
#: opener may name a language). A closing fence is the run alone.
_FIXTURE_FENCE = re.compile(r"[ \t]*(`{3,})([^\n]*)")


#: Any line that starts like a header, well-formed or not (Codex review of
#: ada4c75: ``FIXTURE a.csv: corrected content follows`` after a valid block
#: escaped the duplicate accounting). The path is the first token; an
#: empty one is a malformed header.
_FIXTURE_HEADER = re.compile(r"^[ \t]*FIXTURE\b[ \t]*(\S*)", re.MULTILINE)


def _fixture_body(lines):
    """``(content, "")`` for the one fenced block opening ``lines`` (what
    follows a header, up to the next header or the end of the reply), else
    ``(None, why)``; ``why`` is "unclosed" when a fence opened and never
    closed.

    The block must end at the last fence in that stretch. A closing fence is
    a line of backticks alone, at least as long as the opening run, so a
    sample that itself holds a line of three backticks is fenced with four.
    When more than one line could close the block the content has two
    readings; the first used to be taken in silence, cutting such a sample
    short and writing the stump as if it were whole."""
    opener = _FIXTURE_FENCE.fullmatch(lines[0]) if lines else None
    if opener is None:
        return None, "no fence"
    run = len(opener.group(1))
    closers = [index for index, line in enumerate(lines[1:], 1)
               if (fence := _FIXTURE_FENCE.fullmatch(line)) and len(fence.group(1)) >= run
               and not fence.group(2).strip()]
    if not closers:
        return None, "unclosed"
    if len(closers) > 1:
        return None, "ambiguous"
    return "\n".join(lines[1:closers[0]]), ""


def _parse_fixture_blocks(reply: str):
    """``({path: content}, problems)`` for every ``FIXTURE <path>:`` line
    followed by one fenced block. The path is taken as written; the caller
    matches it against the declared fixtures, so an unexpected path is
    ignored, never written. Every header is accounted for, complete or not:
    a path with more than one header, or a header with no complete block
    (an unclosed or missing fence), is a problem for that path and nothing
    is kept for it; so is a block that more than one fence line could close
    (see :func:`_fixture_body`). A block whose content holds another header
    (an unclosed fence swallowed the next block) makes the whole reply
    malformed (Codex reviews of 351d3ba and 6844b97)."""
    lines = (reply or "").splitlines()
    starts = [index for index, line in enumerate(lines) if _FIXTURE_HEADER.match(line)]
    headers: Dict[str, int] = {}
    blocks: Dict[str, List[str]] = {}
    unclear: set = set()
    for position, start in enumerate(starts):
        path = _FIXTURE_HEADER.match(lines[start]).group(1).strip().rstrip(":").strip("`'\"")
        if not path:
            return {}, ["malformed reply: a FIXTURE header names no path"]
        headers[path] = headers.get(path, 0) + 1
        last = position == len(starts) - 1
        body, why = _fixture_body(lines[start + 1:len(lines) if last else starts[position + 1]])
        if not _FIXTURE_HEAD.fullmatch(lines[start]):
            continue    # a header with words after its path opens no block
        if why == "unclosed" and not last:
            return {}, ["malformed reply: a fenced block holds another FIXTURE header (an unclosed fence)"]
        if why == "ambiguous":
            unclear.add(path)
        elif body is not None:
            blocks.setdefault(path, []).append(body)
    found: Dict[str, str] = {}
    problems: List[str] = []
    for path in sorted(headers):
        count, bodies = headers[path], blocks.get(path, [])
        if count > 1:
            problems.append(f"{path}: more than one FIXTURE header for the same path")
        elif path in unclear:
            problems.append(f"{path}: more than one fence line could close the block, so its content has two "
                            "readings and nothing is written (fence a sample that holds a line of backticks "
                            "with a longer run)")
        elif len(bodies) == 1:
            found[path] = bodies[0]
        else:
            problems.append(f"{path}: a FIXTURE header with no complete fenced block")
    return found, problems


#: A lead's typed claim that a blocking finding or a failed case of its own
#: task needs files outside its grant (cycle04, PR73 6713487831: completion-
#: cycle03's t5 said so in prose and still received a second revision, its
#: recheck and a gate-fix under the same README/tests grant). Bounded: at most
#: ``_MAX_SCOPE_LINES`` lines read per reply and ``_MAX_SCOPE_PATHS`` paths per
#: id, each path at most ``_MAX_SCOPE_PATH_CHARS``.
_OUTSIDE_SCOPE = re.compile(r"^[ \t]*OUTSIDE SCOPE:[ \t]*(\S+?)[ \t]+-[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_MAX_SCOPE_LINES = 8
_MAX_SCOPE_PATHS = 5
_MAX_SCOPE_PATH_CHARS = 300
_DECIDE = re.compile(r"^\s*DECIDE:\s*(R\d+)\s*[-:—]\s*(.+?)\s*$", re.MULTILINE)
_REQ_BLOCK = re.compile(r"^\s*REQUIREMENTS:\s*\n((?:\s*R\d+\s*[:.)-].*\n?)+)", re.MULTILINE)
_REQ_LINE = re.compile(r"^\s*(R\d+)\s*[:.)-]\s*(.+?)\s*$", re.MULTILINE)
_COVERS = re.compile(r"^\s*COVERS:\s*(.+?)\s*$", re.MULTILINE)
#: An editing call's own account of acceptance it could not run (runtime
#: check guidance). Read only to lower an audit verdict, never to raise one.
_NOT_RUN_LINE = re.compile(r"^\s*(?:NOT RUN|BLOCKED):\s*(.+?)\s*$", re.MULTILINE)
#: An optional association on a NOT RUN reason: ``... - <why> | for: R2, R5``.
_NOT_RUN_FOR = re.compile(r"\s*\|\s*for:\s*(R\d+(?:\s*,\s*R\d+)*)\s*$", re.IGNORECASE)
#: A NOT RUN item that only names reading files (turn-tracking-01 O10:
#: "Read tickets/store.py tests/test_dependencies_store.py"), never a run.
_INSPECTION_ITEM = re.compile(r"^\s*(?:read|reading|inspect|inspecting|open|view|examine|look\s+at|review)\b\s*:?",
                              re.IGNORECASE)
#: A reviewer's judgment on an inspection gap it was handed.
#: One line each: the fields are separated by spaces or tabs only, so an
#: empty judgment never borrows the next line (root review of 508a9a8).
_INSPECTION_VERDICT = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?(READ|UNREAD)[ \t]+(IG-[0-9a-f]{10})\b[ \t]*[-:]?[ \t]*([^\n]*)$",
                                 re.IGNORECASE | re.MULTILINE)


def _inspection_attested(gap) -> bool:
    """Whether a gap's attestation carries the authority the engine grants
    one: a named seat, a review or recheck stage, a non-empty READ judgment,
    digests for exactly the gap's files, and a consumed hand-off of exactly
    those digests and source to that seat and stage. Missing, legacy or
    malformed authority is no attestation. ``tools/truth_check.py`` applies
    the same rule to the saved record."""
    if not isinstance(gap, dict):
        return False
    attested, files = gap.get("attested"), [str(f) for f in gap.get("files") or []]
    if not isinstance(attested, dict) or not files:
        return False
    digests = attested.get("files")
    if (not isinstance(digests, dict) or sorted(digests) != sorted(files)
            or any(not isinstance(v, str) or not v for v in digests.values())):
        return False
    by, stage, judgment = attested.get("by"), attested.get("stage"), attested.get("judgment")
    if not (isinstance(by, str) and by and stage in ("review", "recheck")
            and isinstance(judgment, str) and judgment.strip()):
        return False
    return any(isinstance(h, dict) and h.get("peer") == by and h.get("stage") == stage and h.get("consumed") is True
               and h.get("files") == digests and h.get("source") == attested.get("source")
               and (attested.get("task") is None or h.get("task") == attested.get("task"))
               for h in gap.get("handed") or [])


#: Words that join file names in a pure reading request.
_READING_JOINERS = frozenset({"and", "&", ",", "+"})


def _pure_reading(item: str) -> List[str]:
    """The files of a NOT RUN item that asks only to read files: a reading
    verb, then file paths and joiners, nothing else. Anything more (a
    command, a run, prose) is not a reading, and its execution obligation
    stands (root review of a3de6d6, item 1). Empty when not pure."""
    m = _INSPECTION_ITEM.match(item or "")
    if not m:
        return []
    import shlex
    rest = item[m.end():]
    try:
        tokens = shlex.split(rest)
    except ValueError:
        return []
    paths = []
    for token in tokens:
        bare = token.strip(",;")
        if not bare or bare.lower() in _READING_JOINERS:
            continue
        if "::" in bare or not ("/" in bare or re.search(r"\.[A-Za-z]\w*$", bare)):
            return []
        paths.append(bare[2:] if bare.startswith("./") else bare)
    return paths


#: Documentation files a reading scenario may name (never a source file).
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_PROVES_ANY = re.compile(r"^\s*PROVES:(.*)$", re.MULTILINE)
_PROVES_BODY = re.compile(r"(P\d+)\s*-\s*(\S+)")
_PROVES_PID = re.compile(r"\s*(P\d+)\b")
#: The same line read loosely, only to say what is wrong with it.
_PROVES_LOOSE = re.compile(r"(P\d+)\s*-\s*(.+)")


def _proves_problem(rest: str) -> str:
    """Why the text after 'P<n> -' is not one test case, measured from its
    shape and said in the terms the correction needs (one exact
    file::[Class::]test_case per line, repeated per case). The grammar
    itself is unchanged: this names the fault, it never repairs it."""
    rest = rest.strip().strip("`")
    shown = rest[:80]
    if "," in rest:
        parts = [p.strip() for p in rest.split(",") if p.strip()]
        return (f"'{shown}' lists {len(parts)} items on one line (comma-separated): one test case per "
                f"PROVES line, one line per case, repeated with the same P<n>")
    if any(ch.isspace() for ch in rest):
        return f"'{shown}' names no single test case: one exact file::[Class::]test_case per PROVES line, nothing else on it"
    if "::" not in rest:
        return f"'{shown}' names a file, not a test case: one exact file::[Class::]test_case per PROVES line"
    last = rest.rsplit("::", 1)[-1]
    if not re.fullmatch(r"test\w*(?:\[[^\]\s]*\])?", last):
        return (f"'{shown}' names a test class or object, not a test case: one exact "
                f"file::Class::test_case per PROVES line, one line per case")
    return f"'{shown}' is not a file::[Class::]test_case identity in a test file"


def _folds_binding(plan: dict, editor: bool) -> bool:
    """Whether a correction bundle's binding declaration rides in the editor
    revision the same bundle already buys (cycle22; run21 t8's bundle needed
    5 calls with 4 left, and its separate declaration settled on the
    pre-edit source the revision then changed). The one decision both the
    admission (``_fix_cycle_stages``) and the serve read."""
    return bool(plan.get("binding")) and bool(editor)


def _split_item_reason(text: str) -> Tuple[str, str]:
    """``(item, why)``: the item and its reason are separated by the first
    `` - `` that lies outside a bracketed parameter, so ``test_value[a - b]
    - no socket`` keeps its whole identity (root's addendum on 3b99f5f: a
    plain partition cut the parameter at ``[a``)."""
    depth = 0
    for i, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif depth == 0 and text.startswith(" - ", i):
            return text[:i], text[i + 3:]
    return text, ""


#: Kept because one of the development line's tests still names it. Nothing
#: in the engine reads it: audit lines go through quadratus.verdicts.audit_lines
#: and a reviewer's requirement lines through verdicts.requirement_line.
_AUDIT_LINE = re.compile(r"^\s*(R\d+)\s*:\s*(MET|NOT MET)\b[\s:—-]*(.*)$", re.MULTILINE | re.IGNORECASE)
#: The id a reply line answers: its leading '<id>:' (a bullet, a number and
#: backticks allowed), for the audit's required-line list (cycle05).
_ANSWER_ID = re.compile(r"\s*(?:(?:[-*+]|\d+[.)])\s+)?`?([A-Za-z][A-Za-z0-9]*-?\d+)`?\s*:")
#: The separator a reviewer or planner writes between a scenario id and
#: the text after it: a hyphen, an en dash, an em dash, a minus sign or a
#: colon (close family, 2026-10-26: Sol's SCENARIO claim on
#: recovery-handoff-01 t3 used an em dash, did not type, and became the
#: code finding that ended the run). Punctuation is not semantics: the
#: fields after it are still required in full (``_typed_finding``).
_TYPED_SEP = r"(?:-|\u2013|\u2014|\u2212|:)"
#: Scenario ids are ASCII ``P`` plus ASCII digits only: ``\d`` would read
#: other scripts' digits, and an id the task never declared is unbound.
#: An earlier task's scenario is named with its task, ``t<k>:P<n>`` (cycle22),
#: so a later reviewer's t7:P1 can never be read as its own task's P1.
_SCENARIO_CLAIM = re.compile(r"^\s*SCENARIO\s+(?:(t[0-9]+):)?(P[0-9]+)\s*" + _TYPED_SEP
                             + r"\s*(.*?)\s*\|\s*proposal\s*:\s*"
                             r"(operation|state|expect)\s*:\s*(.*?)\s*$", re.IGNORECASE | re.DOTALL)
_EVIDENCE_CLAIM = re.compile(r"^\s*EVIDENCE\s+(P[0-9]+)\s*" + _TYPED_SEP + r"\s*(.*?)\s*$", re.IGNORECASE | re.DOTALL)
#: Line-bounded on purpose: ``\s*`` would cross the newline after a blank
#: field and read the next line as the corrected text (root's control,
#: 2026-10-26: ``SCENARIO P1: state:`` followed by a P2 line or by prose
#: was recorded as corrected with that next line as P1's state).
_SCENARIO_REPLY = re.compile(r"^[ \t]*SCENARIO[ \t]+((?:t[0-9]+:)?P[0-9]+)[ \t]*:[ \t]*"
                             r"(operation|state|expect|KEEP|REQUIREMENT)\b"
                             r"[ \t]*" + _TYPED_SEP + r"?[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
#: A claim that opens like a typed one. Read only to say why it did not
#: type, never to route it.
_TYPED_HEAD = re.compile(r"^\s*(SCENARIO|EVIDENCE)\s+((?:t[0-9]+:)?P\S*)", re.IGNORECASE)
#: An inclusive scenario range in a NOT RUN item, ASCII hyphen or en dash,
#: with a third endpoint captured so a chain is refused (cycle22).
_SCENARIO_RANGE = re.compile(r"(?<![\w./:-])P([0-9]+)[ \t]*[-\u2013][ \t]*P([0-9]+)"
                             r"(?:[ \t]*[-\u2013][ \t]*P([0-9]+))?(?![\w./:-])")
#: A claim no passing run establishes, beside scenarios in a NOT RUN item.
_UNRUNNABLE_CLAIM = re.compile(r"\b(?:mutation|mutations|mutant|mutants)\b", re.IGNORECASE)

#: Why a receipt of the exact required command is not a whole-command run,
#: by ``_check_runs``'s command reasons (cycle17): each says which fact holds.
_COMMAND_SELECTION_REASONS = {
    "argv": "its arguments carry an option the harness reads as a test selection",
    "runner": "the harness cannot establish what this command form selects",
    "external": "a test selection from the environment or configuration applies to it",
    "deselected": "the runner reported deselected cases",
}

_IMPACT_RANK = ("other", "contract", "correctness", "irreversible", "security", "integrity", "data-loss")


def _worst_impact(records) -> Optional[str]:
    """The gravest impact among finding records, for the recheck's effort."""
    impacts = [str(r.get("impact") or "") for r in records if r.get("impact") in _IMPACT_RANK]
    return max(impacts, key=_IMPACT_RANK.index) if impacts else None


def _typed_finding(claim: str):
    """``("scenario", fields)``, ``("evidence", fields)`` or ``(None, None)``
    for one registered finding's claim (the line before its basis and
    impact segments). Strict: a SCENARIO claim needs the scenario id, the
    contradiction and a one-field proposal with text; an EVIDENCE claim
    needs the id and what is not shown. A blank field is no field (root's
    old-source control, 2026-10-26: ``SCENARIO P1 -   | proposal: state:
    nothing stored`` typed with an empty reason), so anything looser is
    ``(None, None)``: an ordinary finding, never a planner or harness route
    without its semantics. Whether such a claim is a near miss that must
    not reach the editor either is ``_typed_near_miss``."""
    match = _SCENARIO_CLAIM.match(claim or "")
    if match:
        fields = dict(proof_id=match.group(2).upper(), reason=match.group(3).strip(),
                      field=match.group(4).lower(), proposal=match.group(5).strip())
        if match.group(1):
            fields["task"] = match.group(1).lower()
        if fields["reason"] and fields["proposal"]:
            return "scenario", fields
        return None, None
    match = _EVIDENCE_CLAIM.match(claim or "")
    if match:
        fields = dict(proof_id=match.group(1).upper(), reason=match.group(2).strip())
        if fields["reason"]:
            return "evidence", fields
        return None, None
    return None, None


def _typed_near_miss(claim: str) -> str:
    """Why a claim that opens as a typed one (``SCENARIO P<n>`` or
    ``EVIDENCE P<n>``) did not type, or "" for a claim that is complete or
    not typed at all. Such a claim is about the plan or the evidence, not
    the code: it is held for its reviewer to restate, and it is never the
    editor's instruction (Davis, 2026-10-26)."""
    head = _TYPED_HEAD.match(claim or "")
    if not head or _typed_finding(claim)[0] is not None:
        return ""
    kind = head.group(1).upper()
    if not re.fullmatch(r"(?i)(?:t[0-9]+:)?P[0-9]+", head.group(2).rstrip(":.,;")):
        return f"a {kind} claim whose scenario id ({head.group(2)[:20]}) is not P followed by digits"
    if kind == "SCENARIO":
        if "|" not in (claim or "") or not re.search(r"(?i)\|\s*proposal\s*:", claim or ""):
            return "a SCENARIO claim without its '| proposal: <operation|state|expect>: <text>' segment"
        if not re.search(r"(?i)\|\s*proposal\s*:\s*(operation|state|expect)\s*:", claim or ""):
            return "a SCENARIO claim whose proposal names no scenario field (operation, state or expect)"
        return "a SCENARIO claim with a blank contradiction or a blank proposed text"
    return "an EVIDENCE claim without saying what is not shown"


#: The segment that separates the operation (the corrected text) from the
#: planner's explanation on a correction line.
_CORRECTION_EXPLANATION = re.compile(r"\s*\|\s*(?:because|why|explanation|reason|note)\s*:\s*", re.IGNORECASE)


def _split_explanation(text: str) -> Tuple[str, str]:
    """``(operation, explanation)``: the corrected text (or a KEEP or
    REQUIREMENT reason) before the first ``| because:`` segment, and the
    explanation after it. A line with no such segment is all operation."""
    parts = _CORRECTION_EXPLANATION.split(text or "", maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return (text or "").strip(), ""


def _scenario_text_problem(text: str, current: str) -> str:
    """Why a planner's corrected scenario text cannot stand on its own
    shape, or "": empty, more than one line, or the declared text again.
    There is no character count (root on 6c1b922: a 683-character state
    the declaration accepts was refused only for being over 400); the
    size bound a corrected field meets is the one a declared field meets,
    the task's role packet limit, checked by ``_correction_packet_problem``."""
    text = (text or "").strip()
    if not text or text.lower() in ("none", "n/a", "-"):
        return "the corrected text is empty"
    if "\n" in text:
        return "the corrected text spans more than one line"
    if text == (current or "").strip():
        return "the corrected text equals the declared text"
    return ""


#: The one size an evidence file may have: a test, not a payload, and small
#: enough that the reassessment is always shown the whole file.
_MAX_EVIDENCE_TEST_BYTES = 16_000

_EVIDENCE_HEAD = re.compile(r"\s*EVIDENCE\s*:\s*(\S+?)\s*(?:[-:—]\s*(.*))?$", re.IGNORECASE)
_EVIDENCE_FILE = re.compile(r"\s*FILE\s*:\s*`?([^`\s]+)`?\s*$", re.IGNORECASE)
_EVIDENCE_BODY = re.compile(r"^```[^\n]*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


def _parse_evidence(reply: str) -> Optional[dict]:
    """An orchestrator evidence request, or None when the reply is not one.

    The reply's first non-blank line is ``EVIDENCE: <R<n>|U<n>|none> - <claim>``,
    then ``FILE: <new test path>``, then one fenced block holding the whole
    file. A reply that starts with EVIDENCE: but is otherwise malformed comes
    back with ``error`` set, so it is answered and never read as a task."""
    lines = (reply or "").strip().splitlines()
    if not lines:
        return None
    head = _EVIDENCE_HEAD.match(lines[0])
    if head is None:
        return None
    target = head.group(1).strip(" .,:").upper()
    request = dict(target=target, claim=(head.group(2) or "").strip()[:300], path="", text="", error="")
    if not re.fullmatch(r"[RU]\d+|NONE", target):
        return dict(request, error="the first line must name one finding id (R<n> or U<n>) or none")
    path_line = next((m for m in map(_EVIDENCE_FILE.match, lines[1:4]) if m), None)
    body = _EVIDENCE_BODY.search("\n".join(lines[1:]) + "\n")
    if path_line is None or body is None:
        return dict(request, error="it needs a FILE: line and one fenced block holding the whole test file")
    return dict(request, path=path_line.group(1).strip(), text=body.group(1))


def _evidence_path_problem(path: str) -> str:
    """Why ``path`` cannot be an evidence test file, or ''."""
    from .integration import _test_family
    parts = PurePosixPath(path).parts
    if not path or path.startswith(("/", "\\")) or "\\" in path or ".." in parts or any(
            p.startswith(".") for p in parts):
        return "the path must be project-relative, with no '..' or hidden component"
    if not _test_family(path):
        return "the file must be a test file, named as the project's test runner collects it"
    return ""


def _read_requirements(reply: str):
    """Split a REQUIREMENTS block off an orchestrator reply."""
    match = _REQ_BLOCK.search(reply or "")
    if not match:
        return {}, reply
    found = {rid: text for rid, text in _REQ_LINE.findall(match.group(1))}
    rest = (reply[:match.start()] + reply[match.end():]).strip()
    return found, rest


def _covers_ids(text) -> List[str]:
    """The requirement ids a task text claims: every id on every ``COVERS:``
    line, in the order written, each once.

    The one reading of a COVERS declaration. The task's own record
    (``_read_covers``) and an operator list's claims (``Session.run``) both
    come from here: read apart, the record said a task covered R2 while the
    run said nobody claimed it, and the run completed with R2 never audited
    (review 1110, engine 2)."""
    return list(dict.fromkeys(rid for line in _COVERS.findall(text or "") for rid in re.findall(r"R\d+", line)))


def _read_covers(spec):
    """Split the ``COVERS: R1, R3`` lines off a task.

    More than one line is one declaration: every id, in the order written,
    each once. The first line used to be read and all of them deleted, so
    "COVERS: R1" and a later "COVERS: R4" dropped R4 without a word."""
    if not _COVERS.search(spec.description or ""):
        return [], spec
    ids = _covers_ids(spec.description)
    description = _COVERS.sub("", spec.description).strip()
    return ids, replace(spec, description=description or spec.description)


_RESOLVES = re.compile(r"^\s*RESOLVES:(.*)$", re.MULTILINE)
#: A planned task taking over standing NOT RUN items (cycle02).
_DISCHARGES = re.compile(r"^\s*DISCHARGES:(.*)$", re.MULTILINE)
#: An owning lead's restatement of a NOT RUN item as one exact case
#: (cycle02): 'RUNS: O<n> - file::[Class::]test_case'.
_RUNS_ANY = re.compile(r"^\s*RUNS:(.*)$", re.MULTILINE)
_RUNS_BODY = re.compile(r"\s*(O\d+)\s*-\s*(\S+)\s*")
#: A reviewer's reading of one deferred row at the post-merge recheck.
_DEFERRED_ROW_LINE = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?(P[0-9]+)[ \t]*:[ \t]*(SHOWN|NOT[ \t]+SHOWN)\b[ \t]*"
                                r"(?:-|\u2013|\u2014|:|,)?[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)


def _read_discharges(spec):
    """Split a ``DISCHARGES: O3, O7`` line off a task, as ``_read_resolves``."""
    match = _DISCHARGES.search(spec.description or "")
    if not match:
        return None, spec
    tokens = [t for t in re.split(r"[\s,]+", match.group(1).strip()) if t]
    description = _DISCHARGES.sub("", spec.description).strip()
    return tokens, replace(spec, description=description or spec.description)


def _resolves_tokens(line: str) -> tuple:
    return tuple(t for t in re.split(r"[\s,]+", line.strip()) if t)


def _read_resolves(spec):
    """Split a ``RESOLVES: F1, F2`` line off a task. None when absent; the
    raw tokens otherwise, so an empty or malformed line can be refused.

    A line repeated word for word is one line. Lines that differ are all
    returned, never only the first: the dispatch loop refuses a RESOLVES on
    more than one line, and nothing is dropped unread before it does."""
    found = _RESOLVES.findall(spec.description or "")
    if not found:
        return None, spec
    tokens = [t for line in dict.fromkeys(_resolves_tokens(line) for line in found) for t in line]
    description = _RESOLVES.sub("", spec.description).strip()
    return tokens, replace(spec, description=description or spec.description)


_PARALLEL_HEAD = re.compile(r"^\s*PARALLEL\s*$", re.MULTILINE)


def _parallel_blocks(reply: str):
    """The task blocks of a 'PARALLEL' reply, or None for a single task."""
    match = _PARALLEL_HEAD.search(reply or "")
    if not match:
        return None
    blocks = [b.strip() for b in re.split(r"^\s*---\s*$", reply[match.end():], flags=re.MULTILINE)]
    blocks = [b for b in blocks if b]
    return blocks if len(blocks) >= 2 else ([blocks[0]] if blocks else None)


def _cited_path(token) -> str:
    """A path as an auditor wrote it, in the form project paths are compared
    in: a leading "./" or "/" dropped as a prefix, then ``_scope_path``. ""
    for one that names nothing inside the project (empty, or a ".." part).

    ``lstrip("./")`` strips characters, not a prefix, so a citation of
    ".storybook/main.js" was looked up as "storybook/main.js": never found,
    or found as another file that carried that name (review 1110, engine 4)."""
    text = str(token)
    while text.startswith(("./", "/")):
        text = text[1:] if text.startswith("/") else text[2:]
    path = _scope_path(text)
    return "" if path in ("", ".") or ".." in path.split("/") else path


_TIER = re.compile(r"^\s*TIER:\s*(direct|normal)\s*$", re.MULTILINE | re.IGNORECASE)


def _read_tier(spec):
    """Split a ``TIER: direct`` line off a task. Returns (tier, spec).

    Two TIER lines that disagree are sent back before a task is built (see
    :func:`_read_metadata`). Read here anyway, they ask for the normal tier:
    the label that drops the review never wins by being written first."""
    values = list(dict.fromkeys(v.lower() for v in _TIER.findall(spec.description or "")))
    if not values:
        return "normal", spec
    description = _TIER.sub("", spec.description).strip()
    return (values[0] if len(values) == 1 else "normal"), replace(spec, description=description or spec.description)


_CONTINUES = re.compile(r"^\s*CONTINUES:\s*(\S+)\s*$", re.MULTILINE)


def _read_continues(spec):
    """Split a ``CONTINUES: <task id>`` line off a task, naming the capped
    task it finishes. Returns (task id or None, spec without the line).

    Two CONTINUES lines naming different tasks are sent back before a task
    is built (see :func:`_read_metadata`). Read here anyway, they name no
    task: neither capped task's debt is discharged by a guess."""
    values = list(dict.fromkeys(_CONTINUES.findall(spec.description or "")))
    if not values:
        return None, spec
    description = _CONTINUES.sub("", spec.description).strip()
    return (values[0] if len(values) == 1 else None), replace(spec, description=description or spec.description)


_HYPOTHESIS = re.compile(r"^\s*HYPOTHESIS:\s*(.+?)\s*$", re.MULTILINE)


def _read_hypothesis(spec):
    """Split a ``HYPOTHESIS: <text>`` line off a task (survey runs): what
    the orchestrator inferred from the last failure and what it changes.
    Returns (text or None, spec without the line)."""
    found = list(dict.fromkeys(_HYPOTHESIS.findall(spec.description or "")))
    if not found:
        return None, spec
    description = _HYPOTHESIS.sub("", spec.description).strip()
    # Diagnostic text: more than one line is kept whole, never only the first.
    return " / ".join(found), replace(spec, description=description or spec.description)


def _read_task_orientation(description: str):
    """Split ``MAP NOTES:`` lines out of a task description.

    Returns ``(notes, remaining_description)``. The notes are removed from the
    description because they are not part of the task: leaving them in would
    put them through the scope lint and into the task text the lead is told to
    satisfy verbatim.

    Malformed notes are dropped rather than guessed at, exactly as in a
    close-out: the map is long-lived and re-sent on every call, so a wrong note
    there is worse than no note. Bounded in count and length for the same
    reason -- this exists to remove a cost, not to relocate it.
    """
    lines, notes, collecting = [], [], False
    for line in description.splitlines():
        match = re.match(r"\s*MAP\s+NOTES\s*:(.*)$", line, re.IGNORECASE)
        if match is not None:
            collecting = True
            rest = match.group(1).strip()
            if rest:
                notes.append(rest)
            continue
        stripped = line.strip()
        if collecting:
            # The section runs until a blank line or a line that is plainly
            # not one of its entries, so a description continuing underneath
            # is not swallowed.
            if stripped.startswith(("-", "•", "*")) or (stripped and ":" in stripped
                                                        and len(stripped) <= 300
                                                        and not stripped.endswith(".")):
                notes.append(stripped)
                continue
            collecting = False
        lines.append(line)
    parsed = []
    for raw in notes:
        raw = raw.lstrip("-•* ").strip()
        topic, _, note = raw.partition(":")
        if topic.strip() and note.strip():
            parsed.append((topic.strip()[:40], note.strip()[:_MAX_ORIENT_NOTE_CHARS]))
    return parsed[:_MAX_ORIENT_NOTES], "\n".join(lines).strip()


def _settle_directives(description: str):
    """``(description, problem)``: a task's directive lines given one reading
    before any reader sees them.

    A RESOLVES, CONTINUES or TIER line repeated word for word is one line,
    and the repeat is dropped. Two TIER lines, or two CONTINUES lines, that
    disagree have no single reading, and a NEEDS line that cannot be read
    used to end the run: each is a problem for the caller to send back. A
    RESOLVES on two lines that differ is left for the dispatch loop, which
    has always refused it. Two NEEDS lines that disagree are not a problem
    here: the prompt says they are refused with no correction."""
    kept, seen, dropped = [], {}, False
    for line in (description or "").splitlines():
        repeat = False
        for name, pattern, key in (("TIER", _TIER, str.lower), ("CONTINUES", _CONTINUES, str),
                                   ("RESOLVES", _RESOLVES, _resolves_tokens)):
            match = pattern.match(line)
            if match is None:
                continue
            value = key(match.group(1))
            values = seen.setdefault(name, [])
            if value in values:
                repeat = True
            else:
                values.append(value)
        if repeat:
            dropped = True
        else:
            kept.append(line)
    if len(seen.get("TIER", ())) > 1:
        return description, ("the task carries more than one TIER line and they disagree ("
                             + ", ".join(seen["TIER"]) + "); state one")
    if len(seen.get("CONTINUES", ())) > 1:
        return description, ("the task carries more than one CONTINUES line naming different tasks ("
                             + ", ".join(seen["CONTINUES"]) + "); a task continues one task, so name "
                             "the other in a task of its own")
    problem = _needs_lines(description)[2]
    if problem:
        return description, f"invalid task requirements: {problem}"
    return ("\n".join(kept).strip() if dropped else description), ""


#: A line opening with either marker, in any case: before, UNRESOLVED stopped
#: in any case too (Codex review 5859031079), and must keep doing so. The
#: verdict readers live in quadratus.verdicts; this pattern and the next are
#: what the finding registry itemises a review with
#: (Session._register_review_findings, Session._record_review_notes).
_BLOCKING_LINE = re.compile(r"\s*(?:(?:[-*+]|\d+[.)])\s+)?(?:BLOCKING|UNRESOLVED)\s*:\s*(.*)$", re.IGNORECASE)
#: "BLOCKING: none found." is a note too (series rule-b1ff751 f6, 2026-10-06:
#: a verifier that accepted the code closed the task with its own report
#: filed as the open finding). The same words verdicts.is_empty_note reads.
_EMPTY_FINDING = re.compile(r"(?:none|n/?a|nothing)(?:\s+(?:found|identified|observed|noted|detected|recorded|"
                            r"remain(?:s|ing)?|to\s+report))?[\s.!]*$", re.IGNORECASE)


_REQUIREMENT_VERDICT = re.compile(r"^\s*(?:[-*+]\s+)?\**(R\d+)\**\s*:\s*(NOT\s+MET|MET)\b", re.IGNORECASE | re.MULTILINE)


_ERROR_CLAUSE = re.compile(
    r"\b(?:raises?|raised|raising|rejects?|rejected|rejecting|rejection|refus(?:e|es|ed|ing|al)|invalid|"
    r"malformed|non-?zero|exceptions?|denied|forbidden|conflicts?|not found|(?:HTTP|status)\s*[45]\d\d|"
    # A returned client or server status by its number alone, and the usual
    # words for an absent resource (cycle05 trial: an unknown-ID 404
    # requirement never reached the 2**63 boundary candidate).
    r"(?:returns?|responds?(?:\s+with)?|answers?(?:\s+with)?)\s+(?:an?\s+|with\s+)?(?:HTTP\s*)?[45]\d\d|"
    r"nonexistent|non-existent|(?:does|do)\s+not\s+exist|"
    r"errors?\s+(?:response|member|message|code|status|object|output)s?|(?:an|the|JSON)\s+error)\b",
    re.IGNORECASE)


def _error_clause(text: str) -> bool:
    return bool(_ERROR_CLAUSE.search(text or ""))


#: A documentation assertion about behaviour is not the behaviour (cycle07
#: run 20261108T091024Z-75154efd: R23, "README documents the new Store
#: methods, HTTP endpoints and CLI commands, including the refusal rules",
#: was refused reading evidence for the word "refusal", the last task was
#: never admitted, and its audit would have lowered the MET for the same
#: word). A clause whose subject is documentation the work writes (a README,
#: the docs or documentation, a changelog, a guide or manual, a named .md,
#: .rst or .adoc file) and whose verb documents something, or that starts with
#: the imperative Document, Describe or Explain, asserts what that text says;
#: the words it documents are its object, not claims about the program. A
#: behaviour coordinated after it ("Document rejection and reject invalid
#: input") is a claim of its own and is kept. Program output (help or usage
#: text, messages) is never documentation here, and a passive "is documented
#: in" is not recognised: such text stays behaviour, the conservative side.
_DOC_SUBJECT = (r"(?:readme(?:\.\w+)?|docs?|documentation|changelog|(?:user\s+)?guide|manual|"
                r"[\w./-]+\.(?:md|rst|adoc))")
_DOC_ACTIVE = re.compile(
    r"(?i)\b" + _DOC_SUBJECT + r"\s+(?:(?:must|should|shall)\s+)?(?:also\s+)?"
    r"(?:documents?|describes?|explains?|lists?|states?|mentions?|notes?|covers?)\b")
_DOC_IMPERATIVE = re.compile(r"(?i)^\s*(?:document|describe|explain)\b")
#: Where a documentation clause's object may end and another claim begin: a
#: comma or semicolon, a coordinator, a subordinator, or "ensure" (root review
#: of 06c708d, PR73 6720550674). The word that opens a segment stays with it.
_SEGMENT_BOUNDARY = re.compile(
    r"\s*[,;]\s*|\s+(?=(?:and|or|but|while|whereas|then|so\s+that|ensur(?:e|es|ing)|which|whose)\b)",
    re.IGNORECASE)
_COORDINATOR = re.compile(r"(?i)^(?:and|or)\b\s*")
#: A segment opened by one of these always starts a claim of its own.
_SUBORDINATOR = re.compile(r"(?i)^(?:but|while|whereas|then|so\s+that|ensur(?:e|es|ing)|which|whose)\b")
#: A segment that continues the documented object: it opens with one of these.
_OBJECT_CONTINUATION = re.compile(
    r"(?i)^(?:including|such\s+as|for\s+example|e\.g\.|like|covering|listing|with|without|for|of|in|as|"
    r"plus|alongside)\b")
#: A predicate in a segment: an auxiliary or modal, or a finite or
#: participle form of a verb that asserts what the program does. Explicit and
#: lexical on purpose (root review of 621df07, PR73 6720918371): a coordinated
#: noun list ("refusal rules and invalid ID responses") carries none and stays
#: the documented object; "and the API rejects invalid IDs" carries one and is
#: a claim. Nouns that share a stem ("rejection", "refusal", "responses",
#: "errors", "conflicts") are not verb forms here.
_PREDICATE = re.compile(
    r"(?i)\b(?:is|are|was|were|be|been|being|do|does|did|must|should|shall|will|would|can|cannot|could|may|"
    r"might|has|have|had|"
    r"reject(?:s|ed|ing)?|refus(?:e|es|ed|ing)|rais(?:e|es|ed|ing)|return(?:s|ed|ing)?|respond(?:s|ed|ing)?|"
    r"answer(?:s|ed|ing)?|exit(?:s|ed|ing)?|fail(?:s|ed|ing)?|den(?:y|ies|ied|ying)|validat(?:e|es|ed|ing)|"
    r"abort(?:s|ed|ing)?|leav(?:e|es|ing)|left|stor(?:e|es|ed|ing)|creat(?:e|es|ed|ing)|updat(?:e|es|ed|ing)|"
    r"delet(?:e|es|ed|ing)|writ(?:e|es|ing)|wrote|written|print(?:s|ed|ing)?|accept(?:s|ed|ing)?|"
    r"emit(?:s|ted|ting)?|sends?|sent|throw(?:s|n|ing)?|threw|crash(?:es|ed|ing)?|keeps?|kept|"
    r"preserv(?:e|es|ed|ing)|mutat(?:e|es|ed|ing)|produc(?:e|es|ed|ing))\b")


def _behaviour_text(text: str) -> str:
    """The requirement with each documentation clause's documented object
    removed: what it asserts about the program itself (``_DOC_ACTIVE``).
    Clauses are split at a sentence end or a semicolon; text before the
    documentation subject stays. After the documenting verb, the object runs
    through its first segment and on through every later segment that opens
    with an ``_OBJECT_CONTINUATION`` word, or that is a coordinated item
    (and, or, a comma) carrying no ``_PREDICATE``: a noun list. A segment
    opened by a subordinator (but, while, then, ensure, ...) or carrying a
    predicate is a claim of its own and stays, and so does everything after
    it. Any segment whose form this cannot place stays: uncertain is
    behaviour."""
    kept = []
    for clause in re.split(r"(?<=[.;])\s+|;\s*", text or ""):
        hit = _DOC_ACTIVE.search(clause) or _DOC_IMPERATIVE.match(clause)
        if hit is None:
            kept.append(clause)
            continue
        kept.append(clause[:hit.start()])
        segments = [seg.strip() for seg in _SEGMENT_BOUNDARY.split(clause[hit.end():]) if seg and seg.strip()]
        claim = False
        for seg in segments[1:]:
            body = _COORDINATOR.sub("", seg)
            if not claim and body and (_OBJECT_CONTINUATION.match(body)
                                      or (not _SUBORDINATOR.match(body) and not _PREDICATE.search(body))):
                continue
            claim = True
            kept.append(body)
    return " ".join(part for part in kept if part.strip())


def _behavioural_error(text: str) -> bool:
    """Whether a requirement asserts error, refusal or invalid-input
    behaviour of the program (``_error_clause`` over ``_behaviour_text``):
    the claim that needs executed negative-path evidence. Documentation that
    describes such behaviour is not that claim."""
    return _error_clause(_behaviour_text(text))


#: An error requirement whose input is an integer or an identifier (Davis,
#: 2026-11-04, R19 of grok-worker-01: the 5000-digit ID). General
#: vocabulary only, never a project's field names.
_INTEGER_INPUT = re.compile(
    r"\b(?:ids?|identifiers?|integers?|ints?|numbers?|numeric|counts?|index(?:es)?|indices|ports?|"
    r"quantit(?:y|ies)|amounts?|limits?|offsets?|sizes?)\b", re.IGNORECASE)


def _int_conversion_limit():
    """The running interpreter's own limit on digits in an int conversion
    from text, or None where the runtime has none. Read, never set: the
    harness neither weakens Python's protection nor imposes a goal limit."""
    import sys as _sys
    get = getattr(_sys, "get_int_max_str_digits", None)
    try:
        limit = get() if get else 0
    except Exception:  # noqa: BLE001 -- an interpreter without the limit has none to name
        return None
    return limit if isinstance(limit, int) and limit > 0 else None


def boundary_classes(texts) -> list:
    """(rid, classes) for each covered error or refusal requirement whose
    text names an integer or identifier input: candidate input classes at
    common representation edges, each with the condition under which it
    applies, derived from the runtime, never from a candidate. They are
    questions to answer from the requirement's own contract, which may be
    successful handling; never new requirements (root review of a3de6d6,
    item 7: an opaque or arbitrary-precision ID must not be held to a
    refusal the goal never asked for)."""
    limit = _int_conversion_limit()
    classes = ["non-numeric text, where the ID is numeric", "an empty or missing value",
               "a negative value and zero, where the ID is a positive integer",
               "2**63 (9223372036854775808), where values reach a signed 64-bit or SQLite INTEGER store",
               # The same store's lower edge (cycle06 run: a negative ID past
               # it reached the binding unchecked while the upper was bounded).
               "-(2**63)-1 (-9223372036854775809), where a negative value is accepted and reaches the same store"]
    if limit:
        classes.append(f"a decimal of {limit + 1} digits, where text is converted with this Python's int() "
                       f"(its limit is {limit} digits, sys.get_int_max_str_digits())")
    return [(rid, list(classes)) for rid, text in texts
            if _behavioural_error(text) and _INTEGER_INPUT.search(_behaviour_text(text))]


def boundary_note(texts) -> str:
    """The candidate boundary classes for the lead and the reviewer,
    labelled as conditional, untested risks: a class nobody exercised is a
    gap to test, not an observed defect, and what the code must do there is
    the requirement's own contract, never a refusal this list implies."""
    rows = boundary_classes(texts)
    if not rows:
        return ""
    by_classes = {}
    for rid, classes in rows:
        by_classes.setdefault(tuple(classes), []).append(rid)
    lines = [f"- {', '.join(rids)}: " + "; ".join(classes) for classes, rids in by_classes.items()]
    return ("\n\n## Input boundaries these requirements may reach (candidates to check, not new requirements)\n\n"
            + "\n".join(lines)
            + "\nA class applies only where its condition holds for the input the requirement declares; skip "
              "the rest. Where one applies, the requirement's own words decide the expected behaviour, which "
              "may be successful handling: a value the contract accepts (an opaque ID, an arbitrary-precision "
              "integer) must stay accepted, and nothing here asks for a refusal the goal does not state. Do not "
              "lower Python's limit or add a limit the goal does not state. Where the contract requires a "
              "refusal, the code refuses as it says, without a traceback or a crash, and leaves no change "
              "behind. A test case per applicable class is the cheap proof; an approved check runs it. A "
              "class nobody exercised is an untested risk, never a defect anyone observed: report a failure "
              "only when a run or a read of the code shows it, and name which.")


_BOUNDARY_NOTE = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?BOUNDARY[ \t]+(R\d+)[ \t]*:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
#: A quoted span in an auditor's stated basis (cycle20): a straight or curly
#: double-quoted span, or a single-quoted one whose quote marks sit at word
#: boundaries, with apostrophes between letters (goal's, isn't) allowed inside.
#: Opening and closing marks must pair; an apostrophe inside a word never
#: opens or closes a span (the pattern before read "the goal's rule" as the
#: opening of a quote and reported spans no text contains).
_QUOTED_SPAN = re.compile(
    r'"([^"\n]{3,200}?)"'
    r"|\u201c([^\u201c\u201d\n]{3,200}?)\u201d"
    r"|(?<!\w)'((?:[^'\n]|(?<=\w)'(?=\w)){3,200}?)'(?!\w)")
_NOT_MET_SEGMENT = re.compile(r";\s*(basis|evidence|at|example)\s*:\s*", re.IGNORECASE)


#: A token that is a source or test file by its extension (a path operand
#: in a NOT RUN item); anything else shaped like a case is an identity claim.
_SOURCE_FILE = re.compile(r"\.(?:py|pyi|js|mjs|cjs|ts|tsx|jsx|rb|go|rs|java|kt|cs|php|sh|feature)$")
#: An explicit case id, read whole: the bracketed parameter is kept entire
#: (spaces included) and the id must end at a boundary (end, whitespace, a
#: separator, or sentence punctuation), so ``test_complete[not a real
#: parameter]`` is that instance and never its base, and ``test_complete
#: .extra`` is no id at all (root's control on d88d8c0: a prefix match let
#: a run of the base clear a claim about an instance that never existed).
#: ...or a closing quote or backtick around the id, or a colon before prose
#: (root's addendum on 3b99f5f: a backticked or double-quoted id followed by
#: prose returned nothing, which also starved PROVES and error citations).
_ID_END = r"(?=$|[\s,;)\]\"'`]|[.:](?:\s|$))"
_CASE_CITATION = re.compile(r"([\w./-]+\.[A-Za-z]\w*)::((?:[A-Za-z_]\w*::)*test\w*(?:\[[^\]]*\])?)" + _ID_END)
#: A case named without its file in a NOT RUN item: ``Class.test``,
#: ``Class::test`` or a bare ``test_*``, with any parameter kept whole;
#: never a word inside a path or an explicit id (the lookarounds refuse
#: ``/``, ``.``, ``:`` and ``-`` neighbours).
_CASE_NAME = re.compile(r"(?<![\w/.:-])((?:[A-Za-z_]\w*(?:\.|::))?test\w*(?:\[[^\]]*\])?)(?![\w/\[:-])"
                        r"(?!\.(?!\s|$))")


def _cited_cases(text: str) -> List[str]:
    """Explicit ``file::[Class::]name[param]`` case citations, as written:
    the file and class a seat named stay bound to the case."""
    # A leading "./" goes as a prefix (_scope_path): lstrip("./") strips
    # characters, so "./.storybook/a.test.js" lost the dot of its folder.
    return [f"{_scope_path(m.group(1))}::{m.group(2)}" for m in _CASE_CITATION.finditer(text or "")]


def _proof_cases(proof: dict) -> set:
    """The ``file::case`` identities a harness-run scratch proof shows
    passed (its rows, ``integration.scratch_receipt``), with a parametrized
    case's base name when the proof passed in full."""
    out = set()
    for row in (proof or {}).get("cases") or ():
        label, _, outcome = str(row).rpartition(": ")
        if outcome.strip() != "passed" or "::" not in label:
            continue
        out.add(label)
        out.add(label.split("[", 1)[0])
    return out


def _met_lines(text: str) -> Dict[str, str]:
    """rid -> the rest of its MET line, for lines that say MET (not NOT MET).
    Read by the reader that reads the verdict (verdicts.requirement_line),
    so a bulleted or bold MET line is seen here too."""
    out: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        read = _verdicts.requirement_line(raw)
        if read and read[1] is True:
            out.setdefault(read[0], read[2])
    return out


def _review_judgments(text: str) -> Dict[str, "judgment.Judgment"]:
    """A reviewer's per-requirement lines, combined by the shared strict rule
    (quadratus.judgment): NOT MET wins in either order (root review of
    5cfc841), and duplicate MET lines approve nothing. Each line is read by
    verdicts.requirement_line: a MET with a condition or other text after it
    ("R1: MET, but only partly") is no verdict for that requirement, held
    here like a duplicate so it is missing wherever a verdict is required."""
    read = [r for r in map(_verdicts.requirement_line, (text or "").splitlines()) if r]
    out = judgment.combine((rid, met, "") for rid, met, _ in read if met is not None)
    for rid, met, why in read:
        if met is None and (rid not in out or out[rid].state != judgment.NOT_MET):
            out[rid] = judgment.Judgment(judgment.DUPLICATE, why, out[rid].lines + 1 if rid in out else 1)
    return out


def _requirement_verdicts(text: str) -> Dict[str, bool]:
    """rid -> met for each requirement the reviewer judged: a duplicate is no
    verdict, so it is absent here and counts as missing wherever a verdict
    is required."""
    return {rid: j.state == judgment.MET for rid, j in _review_judgments(text).items()
            if j.state != judgment.DUPLICATE}


def _clean_goal_review(text: str, rids) -> bool:
    """A goal-bearing review with nothing to change: NO FINDINGS on the first
    line, every covered requirement MET on its own line, no BLOCKING finding
    and no other text beyond a REVIEW: COMPLETE line."""
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    if not lines or lines[0].upper().rstrip(".") != "NO FINDINGS" or _has_blocking_finding(text):
        return False
    verdicts = _requirement_verdicts(text)
    if not rids or any(verdicts.get(rid) is not True for rid in rids):
        return False
    # An optional 'BOUNDARY R<n>:' note is an observation the prompt offers,
    # never text that makes a clean review unfinished (cycle12).
    # A requirement or row line passes only as the strict reader read it,
    # never by its prefix: "R9: MET, but only partly" is other text.
    return all((_verdicts.requirement_line(line) or (None, None))[1] is not None
               or _INSPECTION_VERDICT.match(line) or _row_verdict(line)
               or _BOUNDARY_NOTE.match(line)
               or line.replace("**", "").rstrip(" .!").upper() == _REVIEW_COMPLETE for line in lines[1:])


def _approving_review(text: str, rids) -> bool:
    """A finished review that approves the work: no BLOCKING finding, a
    verdict form (NO FINDINGS, or a REVIEW: COMPLETE line), and, when it
    covers requirements, every one MET on its own line with no NOT MET line
    for it in either order. Non-blocking notes may sit beside the verdict
    (r07 on 820bed6: the parent review of a capped final child gave R8-R10
    MET, REVIEW: COMPLETE and six low-severity notes, and was held because
    only a bare NO FINDINGS counted). Used where no revision can follow, so
    the notes are kept on record rather than answered."""
    body = (text or "").strip()
    if not body or _has_blocking_finding(body):
        return False
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not (_review_complete(body) or lines[0].upper().rstrip(".") == "NO FINDINGS"):
        return False
    # Root review of 6a1ff8a: a contrary marker anywhere is never outvoted by
    # a later COMPLETE. Any UNFINISHED line, any REVIEW: line other than
    # COMPLETE, and any NOT MET line, covered or not, holds the work.
    for line in lines:
        bare = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line.replace("**", "").replace("`", "")).strip()
        if re.match(r"UNFINISHED\s*:", bare, re.IGNORECASE):
            return False
        verdict = re.match(r"REVIEW\s*:\s*(.*)$", bare, re.IGNORECASE)
        if verdict and verdict.group(1).rstrip(" .!").upper() != "COMPLETE":
            return False
    verdicts = _requirement_verdicts(body)
    if any(met is False for met in verdicts.values()):
        return False
    return all(verdicts.get(rid) is True for rid in rids or ())


_FINDING_ID = re.compile(r"\s*(RF[A-Z0-9]*-\d+)\s*:")


def _finding_id(text: str) -> Optional[str]:
    """The review finding id a recorded unresolved text starts with."""
    match = _FINDING_ID.match(text or "")
    return match.group(1) if match else None


#: The recovery class of a required judgment that did not finish, by the
#: judgment's kind and whether its call reached the round cap (True) or an
#: arrived reply carried no valid verdict (False). quadratus.recovery.
_NO_VERDICT_CLASSES = {
    ("review", True): "review_capped", ("review", False): "review_unfinished",
    ("recheck", True): "recheck_capped", ("recheck", False): "recheck_unfinished",
    ("design review", True): "design_review_capped", ("verification", True): "verification_capped",
    ("test comparison", True): "review_capped", ("test comparison", False): "review_unfinished",
}


def _review_disposition(note: str, capped: bool, rids) -> str:
    """How one review ended, in the record's words, for a factual close-out."""
    if capped:
        return "review capped at its turn limit; no verdict"
    clean = (note.strip().upper().rstrip(".") == "NO FINDINGS" or _clean_goal_review(note, list(rids)))
    if clean:
        return "reviewed: no findings"
    if _has_blocking_finding(note):
        return "reviewed: blocking findings"
    if not _review_complete(note):
        return "review ended without a verdict"
    return "reviewed: non-blocking notes"


_FETCH_REQUEST = re.compile(r"^\s*FETCH\s*:\s*(?P<id>\S+?)(?:\s+FROM\s+(?P<offset>[0-9]+))?\s*$", re.IGNORECASE)


def _parse_fetch(reply: str) -> Optional[str]:
    """An artifact request: the whole reply is ``FETCH: <artifact-id>`` or
    ``FETCH: <artifact-id> FROM <character offset>`` (the next window of a
    long artifact, exact retrieval, 2026-10-29). Returns the artifact id;
    ``_parse_fetch_window`` reads the offset.

    Deliberately strict -- only a reply that *is* a fetch request counts, so a
    draft that merely mentions the word FETCH in code or prose is never
    mistaken for one.
    """
    window = _parse_fetch_window(reply)
    return window[0] if window else None


def _parse_fetch_window(reply: str) -> Optional[Tuple[str, int]]:
    """``(artifact id, character offset)`` of a fetch request, or None."""
    stripped = _parse_kind(reply)[2].strip()
    stripped = lead_request(stripped) or stripped
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) != 1 or not lines[0].upper().startswith("FETCH:"):
        return None
    match = _FETCH_REQUEST.match(lines[0])
    if match:
        return match.group("id"), int(match.group("offset") or 0)
    wanted = lines[0].split(":", 1)[1].strip()
    return (wanted, 0) if wanted else None


def _read_labels(reply: str) -> TaskMetadata:
    """Kind, difficulty and description as :mod:`quadratus.taskmeta` reads
    them, with nothing checked beyond the labels."""
    return parse_metadata(
        reply,
        known_kinds=set(ROUTING),
        known_difficulties=set(Complexity._COLLABORATORS),
        default_kind=TaskKind.GENERAL,
        default_difficulty=Complexity.SIMPLE,
    )


def _read_metadata(reply: str) -> TaskMetadata:
    """Recover kind, difficulty and description, tolerating a bounded preface.

    Delegates to :mod:`quadratus.taskmeta`, which scans a few lines rather
    than only the first. The old first-line-only rule is what let a prefaced
    ``KIND: test rote`` fall through to general/simple, dropping the testing
    pin without a word in the record; see that module for the full account.

    The task's directive lines are settled here too (:func:`_settle_directives`),
    so every path that names a task sends the same problems back the same
    way: TIER or CONTINUES lines that disagree, and a NEEDS line that cannot
    be read.

    Raises:
        AmbiguousMetadata: on contradictory or unknown labels, or directive
            lines with no single reading. The caller re-asks once with a
            correction rather than defaulting, because the default is
            precisely the silent misroute being fixed.
    """
    meta = _read_labels(reply)
    description, problem = _settle_directives(meta.description)
    if problem:
        raise AmbiguousMetadata(problem)
    return meta if description == meta.description else replace(meta, description=description)


def _parse_kind(reply: str) -> tuple:
    """(kind, difficulty, description), degrading rather than raising.

    The tolerant face of :func:`_read_metadata`, kept for the callers that
    only want the description and have no way to re-ask -- an ambiguous label
    there is still better handled as the documented default than as a crash.
    Callers that *can* re-ask use :func:`_read_metadata` directly.
    """
    try:
        # The labels only: a lead's draft is read through here, and its lines
        # are not a task's directives.
        meta = _read_labels(reply)
    except AmbiguousMetadata:
        return TaskKind.GENERAL, Complexity.SIMPLE, (reply or "").strip()
    return meta.kind, meta.difficulty, meta.description


#: A close-out section header: the name, then a colon and the first of its
#: text, or the name alone where markdown marks it as a heading.
_CLOSEOUT_HEADER = re.compile(r"(SUMMARY|DECISIONS|REASONING|DEAD\s+ENDS|MAP\s+NOTES)\s*(?::(.*))?", re.IGNORECASE)
#: What markdown puts around such a header: stars, backticks, a heading mark.
#: A list bullet is not removed: a bulleted line is an entry, as it always was.
_CLOSEOUT_STYLING = re.compile(r"[*`]+|^#{1,6}\s+")
#: "No dead ends." and the like: the section's way of saying it is empty.
_NO_DEAD_ENDS = re.compile(r"no\s+dead\s+ends?(?:\s+(?:were\s+|was\s+)?(?:recorded|found|noted|hit|encountered))?"
                           r"[\s.!]*", re.IGNORECASE)


def _map_entry(line: str) -> Optional[tuple]:
    """``(topic, text)`` for a 'topic: text' line, bulleted or not; None for
    anything else."""
    topic, colon, note = line.lstrip("-\u2022* ").partition(":")
    topic, note = topic.strip(" *_`"), note.strip()
    return (topic, note) if colon and topic and note else None


def _parse_closeout(reply: str):
    """Split a close-out into summary, reasoning, dead ends and map notes.

    Tolerant by design: a missing section degrades to a usable record rather
    than failing the task, since the raw work is stored either way. Reasoning
    falls back to the summary because the ledger refuses an empty one, and a
    weak reason recorded honestly beats a lost task. Map notes that do not
    parse as 'topic: fact' are dropped rather than guessed at -- the map is
    long-lived, so a malformed note is worse there than nowhere.
    """
    sections: Dict[str, List[str]] = {
        "SUMMARY": [], "DECISIONS": [], "REASONING": [], "DEAD ENDS": [], "MAP NOTES": [],
    }
    current = "SUMMARY"
    notes_open = True
    for line in (reply or "").splitlines():
        stripped = line.strip()
        # A header is read as written, or failing that without its markdown:
        # "**SUMMARY:**" and "## Dead ends" used to be body text, which put
        # the whole reply in the summary and the reasoning and dropped its
        # map notes.
        plain = _CLOSEOUT_STYLING.sub("", stripped).strip()
        header = _CLOSEOUT_HEADER.fullmatch(stripped) or _CLOSEOUT_HEADER.fullmatch(plain)
        if header and header.group(2) is None and plain == stripped:
            # The bare word with no colon and no markdown is text, as it
            # always was: a wrapped sentence can leave "summary" or "dead
            # ends" alone on a line, and the lines after it would be filed
            # under that section (as dead-end lessons, at worst).
            header = None
        if header:
            current = " ".join(header.group(1).upper().split())
            notes_open = True
            stripped = (header.group(2) or "").strip()      # what follows the colon opens the section
        if not stripped:
            continue
        if current == "MAP NOTES":
            # The section is its entries and nothing after them: it ends at
            # the first line that is not 'topic: text', so a sentence that
            # follows the notes (and happens to hold a colon further on) is
            # never stored as one.
            notes_open = notes_open and _map_entry(stripped) is not None
            if not notes_open:
                continue
        sections[current].append(stripped)

    summary = " ".join(sections["SUMMARY"]).strip() or (reply or "").strip() or "(no summary)"
    # DECISIONS replaced REASONING in the prompt; an older-style reply still parses.
    reasoning = " ".join(sections["DECISIONS"] + sections["REASONING"]).strip() or summary
    # The prompt says "or none": that answer is not a lesson. It was stored
    # as the dead end "none" and rendered in every later orchestrator prompt.
    dead_ends = [d for d in (d.lstrip("-•* ").strip() for d in sections["DEAD ENDS"])
                 if d and not _NO_DEAD_ENDS.fullmatch(_unstyled(d)) and not _is_empty_note(d)]
    map_notes = [entry for entry in map(_map_entry, sections["MAP NOTES"]) if entry is not None]
    return summary, reasoning, dead_ends, map_notes
