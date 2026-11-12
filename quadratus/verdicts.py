"""One way to read a verdict.

Judging seats answer with a small set of markers: ``NO FINDINGS``,
``BLOCKING: <finding>``, ``REVIEW: COMPLETE``, ``RESOLVED``, ``UNRESOLVED:
<what remains>``, ``VERDICT: ACCEPT`` or ``REJECT``, ``APPROVED``, and the
auditor's ``R1: MET - <citation>``. Each used to be read by its own function
in ``session.py``, and each stripped formatting its own way. So ``RESOLVED.``
read as unresolved, ``BLOCKING: none`` was a finding to one reader and a note
to the next, and the blind-render reply in capitals was also a design
judgement (whole-repo review, 2026-11-10).

Everything here goes through one normaliser, and two rules settle the
doubtful cases:

- Formatting never changes a verdict. Bold, backticks, a heading mark, a list
  bullet, trailing punctuation and letter case are removed before a marker is
  compared. The one exception is deliberate and older than this module: a
  design review approves only with the exact word ``APPROVED``
  (:func:`design_blocking`).
- A reply accepts only when it says so plainly. An acceptance with a
  condition after it (``VERDICT: ACCEPT once fixed``), an approval beside a
  blocking line, or a closing verdict line that is anything but the plain
  word is a rejection. A rejection stays a rejection whatever follows it.

A marker is read as written. Nothing here interprets prose, apart from the
narrow negations the docstrings name.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

__all__ = [
    "HARNESS_BLOCKERS",
    "RENDERS_BLIND",
    "REVIEW_COMPLETE",
    "audit_lines",
    "blocking_findings",
    "design_blocking",
    "design_judgement",
    "has_blocking_finding",
    "has_prefixed_finding",
    "has_security_finding",
    "inspection_lines",
    "is_empty_note",
    "names_id",
    "no_findings",
    "normalise",
    "plain_acceptance",
    "recheck_line",
    "renders_blind",
    "requirement_line",
    "resolved_verdict",
    "review_complete",
    "review_reading",
    "row_line",
    "security_finding_stands",
    "security_verdict_line",
    "unmarked",
    "unresolved_line",
    "unstyled",
]


# -- the normaliser -------------------------------------------------------------

#: What may open a line before its text: a heading mark, a list bullet or a
#: list number. They nest ("- ## x"), so they are removed in a loop. A quote
#: mark stays: a quoted line repeats what someone else wrote, and a reviewer
#: quoting its old finding above RESOLVED has not raised it again.
_LEAD = re.compile(r"(?:#{1,6}(?=\s)|(?:[-*+•]|\d{1,3}[.)])(?=\s))\s*")
#: Stars that open or close a word. A star inside one (2*3) is content.
_STARS = re.compile(r"(?<![A-Za-z0-9])\*{1,3}(?=\S)|(?<=\S)\*{1,3}(?![A-Za-z0-9])")
#: Underscore emphasis is removed only where it wraps the whole line, so an
#: identifier (test_name, _private) keeps its underscores.
_UNDERLINED = re.compile(r"_{1,3}([^_].*[^_]|[^_])_{1,3}")


def unstyled(line: str) -> str:
    """One line with its formatting removed and its wording kept.

    Backticks, emphasis marks, a heading mark and a list bullet or number
    go; whitespace is collapsed. Case and punctuation stay, so the result
    can still be shown to a person.
    """
    return _plain((line or "").replace("`", ""))


def unmarked(line: str) -> str:
    """:func:`unstyled`, with backticks inside the line kept: a quoted
    command in a reason is evidence a caller may check. A pair wrapping the
    whole line is formatting and goes."""
    text = (line or "").strip()
    if len(text) > 1 and text[0] == text[-1] == "`" and text.count("`") == 2:
        text = text[1:-1]
    return _plain(text)


def _plain(line: str) -> str:
    text = (line or "").strip()
    while True:
        lead = _LEAD.match(text)
        if lead is None or not lead.end():
            break
        text = text[lead.end():]
    text = _STARS.sub("", text)
    wrapped = _UNDERLINED.fullmatch(text)
    if wrapped is not None:
        text = wrapped.group(1)
    return " ".join(text.split())


def normalise(line: str) -> str:
    """The form two markers are compared in: :func:`unstyled`, trailing
    punctuation dropped, case folded."""
    return unstyled(line).rstrip(" .!").casefold()


def _says_something(line: str) -> bool:
    """A blank line, a bare code fence or a rule of dashes says nothing."""
    return bool(re.search(r"\w", normalise(line)))


def _content_lines(text) -> List[str]:
    """The lines that say something; the rest are skipped."""
    return [line for line in str(text or "").splitlines() if _says_something(line)]


#: A line that ends by negating the word on the next one.
_NEGATION_ABOVE = re.compile(r"(?:\b(?:not|never)|n't)$", re.IGNORECASE)


def _closes_sentence_above(lines, index: int, marker: str) -> bool:
    """Whether the marker alone on ``lines[index]`` is really the last words
    of a sentence wrapped from the line above.

    A marker is forgiven its trailing punctuation and its case, so the end
    of a wrapped paragraph ("... the second finding is not" / "resolved.")
    would otherwise read as a verdict line, and an acceptance. The words
    stand as a marker when nothing is directly above them (the first line,
    or a blank line, a fence or a rule above), or when the line above is a
    heading or closed its sentence. Under a sentence left open only the
    marker in capitals stands, and not even that under a line ending in not,
    never or n't: "NOT" / "RESOLVED" says not resolved across the line break.
    """
    above = unstyled(lines[index - 1]).rstrip("\"')]") if index else ""
    if not _says_something(above) or above[-1] in ".!?:;" or re.match(r"\s*#{1,6}\s", lines[index - 1]):
        return False
    return bool(_NEGATION_ABOVE.search(above)) or unstyled(lines[index]).rstrip(" .!") != marker


# -- findings -------------------------------------------------------------------

#: A line opening with either marker, in any case: before, UNRESOLVED stopped
#: in any case too (Codex review 5859031079), and must keep doing so.
_PREFIX = re.compile(r"(BLOCKING|UNRESOLVED)\s*:\s*(.*)$", re.IGNORECASE)
#: "BLOCKING: none found." is a note too (series rule-b1ff751 f6, 2026-10-06:
#: a verifier that accepted the code closed the task with its own report
#: filed as the open finding).
_EMPTY_FINDING = re.compile(r"(?:none|n/?a|nothing)(?:\s+(?:found|identified|observed|noted|detected|recorded|"
                            r"remain(?:s|ing)?|to\s+report))?[\s.!]*$", re.IGNORECASE)
#: The marker as a verifier writes it: the uppercase word, not the English one.
_FINDING_MARKER = re.compile(r"\b(?:BLOCKING|UNRESOLVED)\b")
#: A marker directly after one of these is a note about findings, not one.
_NEGATION_BEFORE = re.compile(r"(?:\bnon-|\bnon |\bnot |\bneither |\bno |\bnothing )$", re.IGNORECASE)


def is_empty_note(content: str) -> bool:
    """Whether ``content`` says there is nothing: none, n/a, nothing, with
    or without "found", "recorded" and the like. The one empty-finding rule,
    applied by every reader."""
    return bool(_EMPTY_FINDING.match(unstyled(content)))


def _prefixed(line: str) -> Optional[Tuple[str, str]]:
    """``(marker, content)`` for a line that opens with BLOCKING: or
    UNRESOLVED: under any formatting, else None."""
    found = _PREFIX.match(unstyled(line))
    return (found.group(1).upper(), found.group(2).strip()) if found else None


def _findings(text, markers) -> List[Tuple[str, str]]:
    """Every prefixed line that names something, as ``(raw line, content)``."""
    out = []
    for raw in str(text or "").splitlines():
        found = _prefixed(raw)
        if found and found[0] in markers and not is_empty_note(found[1]):
            out.append((raw, found[1]))
    return out


def has_blocking_finding(text: str) -> bool:
    """A line opening with BLOCKING: that names something.

    Only a finding's explicit prefix controls the recheck loop, and
    "BLOCKING: none" names nothing: it used to count here, so a clean review
    bought a revision and a recheck of a finding that did not exist.
    """
    return bool(_findings(text, ("BLOCKING",)))


def blocking_findings(text: str) -> List[str]:
    """What each BLOCKING: line names, in order, by the reading
    :func:`has_blocking_finding` decides on. For the reviewer record, which
    counts the findings a review raised; it decides nothing."""
    return [content for _, content in _findings(text, ("BLOCKING",))]


def has_prefixed_finding(text: str) -> bool:
    """A line opening with BLOCKING: or UNRESOLVED: that names something.

    Narrower than :func:`has_security_finding`: markers in prose do not
    count. Used beside an explicit ACCEPT, where a verifier that still wrote
    a prefixed finding has contradicted itself and the finding stands.
    """
    return bool(_findings(text, ("BLOCKING", "UNRESOLVED")))


def has_security_finding(text: str) -> bool:
    """A finding is a marker as written, not the word in prose.

    The old test upper-cased every line before looking for BLOCKING or
    UNRESOLVED, so English prose became markers: in the Q9-v2 series
    (2026-09-24) "## Two minor notes, neither blocking" and "One non-blocking
    note for the record" each stopped a run whose verifier had accepted,
    before the terminal question, costing two completions of five.

    Now a line opening with "Blocking:" or "Unresolved:" counts in any case unless what follows
    is none, n/a or nothing (the verifier is told to write BLOCKING: lines, so
    "Blocking: none" is the likeliest note it writes). Elsewhere the uppercase
    marker counts ("This defect is BLOCKING.", "..., but UNRESOLVED: missing
    evidence.") unless a negation sits directly before it: non-, not, neither,
    no or nothing, in any case. Negations fail open only for those forms; any
    other doubt still stops the run, which is the safe side for security.
    """
    for raw in str(text or "").splitlines():
        line = unstyled(raw)
        prefixed = _PREFIX.match(line)
        if prefixed:
            if is_empty_note(prefixed.group(2)):
                continue
            return True
        for marker in _FINDING_MARKER.finditer(line):
            if not _NEGATION_BEFORE.search(line[:marker.start()]):
                return True
    return False


# -- the security verdict line --------------------------------------------------

#: The explicit verdict line the verifier is asked to end with, the word on
#: it, and what follows the word.
_VERDICT_LINE = re.compile(r"VERDICT\s*:\s*(.*)$", re.IGNORECASE)
_VERDICT_WORD = re.compile(r"(ACCEPT\b|REJECT\w*)(.*)$", re.IGNORECASE)


def security_verdict_line(text: str) -> Optional[bool]:
    """The verifier's explicit verdict: True for a plain ACCEPT, False for
    any other VERDICT line, None when it wrote no VERDICT line at all.

    Series b1ff751 f6 and rule-b1ff751 f6 (2026-10-05/06): the cross-family
    verifier accepted the work and the task still closed FindingsOpen,
    because the only signal read from a prose verdict was whether a marker
    appeared somewhere in it. The verifier is now asked to end with one line,
    VERDICT: ACCEPT or VERDICT: REJECT, and that line decides. The last such
    line wins, so a verdict restated after the findings is the one taken.

    An ACCEPT counts only when the line ends there. Every other VERDICT line
    is a rejection (integrator's ruling, review 1110): "VERDICT: ACCEPT once
    fixed", "VERDICT: NOT ACCEPTED", "VERDICT: ACCEPTED", "VERDICT: maybe"
    and the echoed instruction "VERDICT: ACCEPT or VERDICT: REJECT". These
    used to be no verdict line, which handed the decision to the marker
    scan, and the scan passes a report that names its defect in lower case:
    "The token must be fixed first." above "VERDICT: ACCEPT once fixed"
    passed security verification. A verifier that wrote a verdict line has
    answered; only a reply with none is read by the scan.
    """
    verdict = None
    for raw in str(text or "").splitlines():
        found = _VERDICT_LINE.match(unstyled(raw))
        if found is None:
            continue
        word = _VERDICT_WORD.match(found.group(1))
        verdict = bool(word and word.group(1).upper() == "ACCEPT" and not word.group(2).strip(" .!"))
    return verdict


def security_finding_stands(verdict: str) -> bool:
    """Whether a prose verdict opens a security finding.

    An explicit VERDICT line decides: anything but a plain ACCEPT opens one;
    a plain ACCEPT opens one only if the verifier also wrote a prefixed
    finding with content. With no VERDICT line at all, the marker scan
    applies as before.
    """
    explicit = security_verdict_line(verdict)
    if explicit is None:
        return has_security_finding(verdict)
    if explicit:
        return has_prefixed_finding(verdict)
    return True


# -- a collaborator's review ----------------------------------------------------

#: The line a reviewer ends with once it has read the whole work and
#: reported everything. A review is complete when it says NO FINDINGS,
#: names at least one BLOCKING finding, or carries this line; anything else
#: is a reply that arrived, not a review that finished (Codex review of
#: cd8c5b0: "UNFINISHED: did not reach a verdict" closed the task with
#: review=True). A marker as written, never a reading of the prose; the
#: verifier's VERDICT line is the same device.
REVIEW_COMPLETE = "REVIEW: COMPLETE"

_NO_FINDINGS = "no findings"
_COMPLETE_LINE = re.compile(r"review\s*:\s*complete")


def _states_nothing(line: str) -> bool:
    """A line that is only a marker and names no finding: NO FINDINGS, the
    REVIEW: COMPLETE line, or a BLOCKING line that says none.

    BLOCKING only: UNRESOLVED is the recheck's word, not a review's, and
    :func:`review_complete` does not read "UNRESOLVED: none" as a verdict.
    Counting it here made a reply that was no verdict also a clean review,
    so it was recorded as unfinished and then withheld from the lead."""
    plain = normalise(line)
    if plain == _NO_FINDINGS or _COMPLETE_LINE.fullmatch(plain):
        return True
    found = _prefixed(line)
    return found is not None and found[0] == "BLOCKING" and is_empty_note(found[1])


def no_findings(text: str) -> bool:
    """A review that leaves the lead nothing to answer: every line is NO
    FINDINGS, the REVIEW: COMPLETE line or a BLOCKING line that says none.

    Only the bare string "NO FINDINGS" used to qualify, so "NO FINDINGS"
    followed by "REVIEW: COMPLETE" bought a full revision call against an
    empty critique. Any other line is something the reviewer said, and it
    still reaches the lead.

    Nothing to answer is not the same as finished: "BLOCKING: none" by
    itself buys no revision and is still no verdict (:func:`review_complete`).
    """
    lines = _content_lines(text)
    return bool(lines) and all(_states_nothing(line) for line in lines)


def review_complete(text: str) -> bool:
    """Whether a collaborator's reply carries a verdict in a form the prompt
    names: NO FINDINGS opening or closing the reply, a BLOCKING finding, or
    the REVIEW: COMPLETE line. Those three and no other (integrator's ruling,
    review 1110): a BLOCKING line that says none is not a finding, and it is
    not one of the three either, so "BLOCKING: none" by itself is a reply
    that arrived and not a review that finished. With the REVIEW: COMPLETE
    line beside it, the review is complete and has no findings.

    A closing "no findings." that ends a sentence wrapped from the line
    above ("... so for the second file there are" / "no findings.") is that
    sentence, not the marker (:func:`_closes_sentence_above`)."""
    raw = str(text or "").splitlines()
    content = [index for index, line in enumerate(raw) if _says_something(line)]
    if not content:
        return False
    plain = [normalise(raw[index]) for index in content]
    closing = plain[-1] == _NO_FINDINGS and not _closes_sentence_above(raw, content[-1], "NO FINDINGS")
    if plain[0] == _NO_FINDINGS or closing or any(_COMPLETE_LINE.fullmatch(p) for p in plain):
        return True
    return has_blocking_finding(text)


# -- a recheck ------------------------------------------------------------------

_UNRESOLVED_WORD = re.compile(r"\bunresolved\b|\bnot\s+resolved\b", re.IGNORECASE)
#: The only prose in which "unresolved" is not a finding: a whole clause
#: saying that nothing is. The subject and the linking words are closed lists,
#: so "No doubt it is unresolved" and "Nothing is left unresolved except the
#: leak" still count.
_NOTHING_UNRESOLVED = re.compile(
    r"(?:nothing|none|no(?:\s+(?:other|blocking|open|remaining|outstanding))?\s+"
    r"(?:findings?|issues?|items?|problems?|concerns?|blockers?|defects?))"
    r"(?:\s+(?:is|are|was|were|remains?|left|stays?|still)){0,2}\s+unresolved"
    r"|no\s+unresolved\s+(?:findings?|issues?|items?|problems?|concerns?|blockers?|defects?)"
    r"(?:\s+(?:remain|remains|left|are\s+left))?",
    re.IGNORECASE)
_CLAUSE_END = re.compile(r"[.;!?]+")


def _passages(lines, verdict: int) -> List[str]:
    """The reply as passages of running text, each on one line.

    A sentence may be wrapped over several lines, so the end of a line ends
    nothing by itself. A passage ends at a blank line, at the verdict line
    (``lines[verdict]``, and no other line that merely reads "resolved"),
    and where a list item or heading begins."""
    out: List[List[str]] = [[]]
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or index == verdict:
            out.append([])
            continue
        if _LEAD.match(line):
            out.append([])
        out[-1].append(line)
    return [" ".join(passage) for passage in out if passage]


def _says_unresolved(lines, verdict: int) -> bool:
    """Whether the reply says something is unresolved, in any case.

    Read by the clause, across line breaks: "Nothing is left unresolved"
    wrapped before "except the logging leak." is one clause that names a
    leak, and "is not" wrapped before "resolved" still says not resolved."""
    for passage in _passages(lines, verdict):
        for clause in _CLAUSE_END.split(passage):
            if _UNRESOLVED_WORD.search(clause) and not _NOTHING_UNRESOLVED.fullmatch(unstyled(clause)):
                return True
    return False


def resolved_verdict(text: str) -> bool:
    """Accept one standalone boundary verdict, rejecting conflicting markers.

    The requested response remains exactly RESOLVED or UNRESOLVED: <finding>.
    Explanations around a boundary verdict are tolerated, but a prefix match,
    duplicate verdict, or explicit unresolved finding cannot clear a review.
    This parses the declared verdict; it does not validate explanatory prose.

    The verdict line is compared without its formatting: "RESOLVED." and
    "**RESOLVED**" read as unresolved before, which bought another revision
    or left an open finding whose whole text was "RESOLVED.". A clause that
    says nothing is unresolved ("Nothing is left unresolved.") no longer
    contradicts the verdict; any other use of the word still does.

    Forgiving the full stop must not make a verdict of a sentence's last
    word: "The second finding is not" wrapped before "resolved." is no
    verdict line (:func:`_closes_sentence_above`), and is read as the text
    it is.
    """
    raw = str(text or "").splitlines()
    content = [index for index, line in enumerate(raw) if _says_something(line)]
    verdicts = [index for index in content
                if normalise(raw[index]) == "resolved" and not _closes_sentence_above(raw, index, "RESOLVED")]
    if len(verdicts) != 1 or verdicts[0] not in (content[0], content[-1]):
        return False
    return not (has_blocking_finding(text) or _says_unresolved(raw, verdicts[0]))


# -- a design review ------------------------------------------------------------

#: The reviewer's exact reply when the renders miss the change (see
#: ``Session._final_design_review``); read as a capture defect, not a design one.
RENDERS_BLIND = "the renders do not show the changed interface"

#: Blocking lines the harness writes itself: delivery, capture and verdict
#: failures. None is a design judgement a source fix could answer.
HARNESS_BLOCKERS = (
    "BLOCKING: the renders could not be handed", "BLOCKING: the renders changed while",
    "BLOCKING: the final design review gave no verdict", "BLOCKING: the renders do not show",
    "BLOCKING: the reviewer altered",
)
_HARNESS_BLOCKERS = tuple(line.casefold() for line in HARNESS_BLOCKERS)


def design_blocking(verdict) -> List[str]:
    """The blocking lines of a final design review, read whole. Empty means
    approved.

    Every line opening with BLOCKING: that names something is one, under any
    formatting, and one such line overrides an APPROVED beside it. With none,
    the review approved only if the whole reply is the exact word APPROVED.
    Here formatting is not forgiven: the evidence boundary holds that a
    response which is not an exact APPROVED never satisfies the reviewer
    edge (tests/test_workflow_evidence_boundary.py), so "Approved." and a
    bold APPROVED stay no verdict. Anything else is no verdict too, and is
    returned as the one blocker the harness writes for that.
    """
    text = str(verdict or "")
    blocking = [raw.strip() if raw.strip().startswith("BLOCKING:") else f"BLOCKING: {content}"
                for raw, content in _findings(text, ("BLOCKING",))]
    if blocking:
        return blocking
    if text.strip() == "APPROVED":
        return []
    return [f"BLOCKING: the final design review gave no verdict ({text[:120]})"]


def review_reading(review) -> str:
    """The whole decision of a final design review, never its first line.

    APPROVED only when the review approved; otherwise every BLOCKING line
    (at most five shown, the rest counted). A record without the stored
    decision is read from its verdict text by the same rule the session
    applies when it records one."""
    blocking = review.get("blocking")
    if blocking is None:
        blocking = design_blocking(review.get("verdict"))
    if not blocking:
        return "APPROVED"
    shown = "; ".join(str(line)[:300] for line in blocking[:5])
    more = f"; and {len(blocking) - 5} more BLOCKING lines" if len(blocking) > 5 else ""
    return f"not approved: {shown}{more}"


def renders_blind(verdict: str) -> bool:
    """Whether the verdict is the blind-render reply and nothing else.

    A reviewer that also names an independent blocker ("BLOCKING: the delete
    action silently removes saved projects") has judged the work, and a
    recapture must not clear that judgement (Codex reviews of c3abcf0 and
    9b8c056 on #52). The reviewer is told to reply with exactly the one line
    and judge nothing else, so that is what qualifies: the whole response,
    after harmless formatting is stripped (bold, a list marker, trailing
    punctuation, case), must equal "BLOCKING: " plus the phrase. A second
    clause on the same line, a second line of any kind, or any other
    content means the verdict stands as written.
    """
    lines = _content_lines(verdict)
    if len(lines) != 1:
        return False
    found = _prefixed(lines[0])
    return found is not None and found[0] == "BLOCKING" and normalise(found[1]) == RENDERS_BLIND


def _harness_blocker(line) -> bool:
    found = _prefixed(str(line))
    return (found is not None and found[0] == "BLOCKING"
            and f"blocking: {found[1]}".casefold().startswith(_HARNESS_BLOCKERS))


def design_judgement(blocking) -> bool:
    """Whether every blocking line is the reviewer's own design judgement.

    Read with the same normaliser as :func:`renders_blind`. The harness's
    own lines were matched case-sensitively before, so "BLOCKING: The
    renders do not show the changed interface." was the blind reply to one
    test and a design judgement to the other, and a capture defect bought an
    editing call, a gate run, a recapture and a second review.
    """
    return bool(blocking) and not any(_harness_blocker(line) for line in blocking)


# -- the requirements audit -----------------------------------------------------

#: A verdict line for one requirement, on an unstyled line: the id, then MET
#: or NOT MET as the first word after the colon. A line that merely restates
#: a requirement ("R1: the parser accepts empty input") is not one.
_AUDIT_LINE = re.compile(r"(R\d+)\s*:\s*(NOT\s+MET|MET)\b(.*)$", re.IGNORECASE)
#: What must follow MET at once: the separator before the citation.
_AUDIT_SEPARATOR = re.compile(r"\s*[-\u2013\u2014:]\s*(.*)$")


def audit_lines(reply: str) -> List[Tuple[str, bool, str]]:
    """The auditor's verdicts as ``(requirement id, met, why)``, one per id.

    A requirement is MET only on one line that reads ``R1: MET - <citation>``.
    The old read took the last line for an id and any text after the word,
    so "R1: NOT MET - missing" followed by "R1: MET - README.md" ended as
    met, and so did "R1: MET, but only partly; R2 untested - app.py".

    - MET must be followed at once by the separator (a dash or colon) and
      the citation, or by nothing. Anything else is no verdict for that
      requirement.
    - More than one verdict line for a requirement is no verdict for it,
      unless they all say NOT MET. The same line written twice is one line.
    - NOT MET keeps whatever follows it as the reason.

    No verdict comes back as not met with the reason, so the caller records
    why. Whether a MET's citation exists is the caller's check.
    """
    seen: dict = {}
    for raw in str(reply or "").splitlines():
        line = unstyled(raw)
        found = _AUDIT_LINE.match(line)
        if found is None:
            continue
        rid, rest = found.group(1).upper(), found.group(3)
        if found.group(2).upper() != "MET":
            entry = (False, rest.strip(" \t:-\u2013\u2014"))
        elif not rest.strip(" .!"):
            entry = (True, "")
        else:
            cited = _AUDIT_SEPARATOR.match(rest)
            entry = ((True, cited.group(1).strip()) if cited else
                     (None, f"not a verdict line (MET is followed by ' - ' and the citation): {line[:120]}"))
        lines = seen.setdefault(rid, [])
        if entry not in lines:
            lines.append(entry)
    out = []
    for rid, lines in seen.items():
        if len(lines) == 1:
            met, why = lines[0]
            out.append((rid, bool(met), why))
        elif all(met is False for met, _ in lines):
            out.append((rid, False, "; ".join(why for _, why in lines if why)))
        else:
            out.append((rid, False, f"the auditor wrote {len(lines)} verdict lines for {rid} that are not "
                                    "the same; none is read"))
    return out


# -- one line per id: rechecks, evidence rows, review lines, inspections ---------

#: What a reason may not open with: the words that make an acceptance wait on
#: something, take part of it back or question it. A closed list read at the
#: start of the reason only; the prompts that ask for these lines state it.
_CONDITION = re.compile(
    r"(?:if|once|unless|until|provided|providing|assuming|only|but|except|partly|partially|maybe|perhaps|"
    r"pending|or|however|although|though)\b"
    r"|no\b(?=\s*(?:[,.:;!?–—-]|wait\b|$))"
    r"|not\b(?=\s*(?:[,.:;!?]|yet\b|really\b|quite\b|fully\b|entirely\b|$))"
    r"|\?", re.IGNORECASE)
#: The rejecting words, in capitals as a seat writes a marker. One inside
#: the reason of an accepting line contradicts the line.
_CONTRARY = re.compile(r"\b(?:UNRESOLVED|NOT\s+RESOLVED|INSUFFICIENT|NOT\s+SUFFICIENT|NOT\s+MET|UNREAD|WEAKENED)\b")


def plain_acceptance(rest: str, word: str) -> Tuple[bool, str]:
    """Whether the text after an accepting word leaves it a plain acceptance.

    ``(True, reason)`` when the word ends the line, or is followed at once by
    the separator (a dash or a colon) and a reason. ``(False, why not)``
    for anything else: text with no separator ("RESOLVED once the check is
    added", "SUFFICIENT? No", "MET, but only partly", "RESOLVED or
    UNRESOLVED"), a reason that opens with a condition or a hedge
    ("RESOLVED - once fixed"), or a reason that names a rejecting word in
    capitals ("RESOLVED - no, wait: UNRESOLVED"). A line that is not a plain
    acceptance is no verdict for its id: never an acceptance, and never a
    rejection made up for the seat (conformance review, 2026-11-12: each of
    these closed a finding or judged evidence sufficient, and the run
    reported complete).
    """
    rest = str(rest or "")
    if not rest.strip(" .!"):
        return True, ""
    cited = _AUDIT_SEPARATOR.match(rest)
    reason = cited.group(1).strip() if cited else ""
    if cited is None or not reason:
        return False, (f"{word} is read only alone or as '{word} - <reason>'; other text after the word is no "
                       f"verdict: {rest.strip()[:120]}")
    if _CONDITION.match(reason):
        return False, f"{word} with a condition or a hedge is no verdict: {reason[:120]}"
    if _CONTRARY.search(reason):
        return False, f"{word} beside a rejecting word on its own line is no verdict: {reason[:120]}"
    return True, reason


def _free_tail(rest: str) -> str:
    """What follows a rejecting word, kept whole: a rejection stays one."""
    return re.sub(r"^\s*[-:,–—]?\s*", "", str(rest or "")).strip()


def _forms(line: str) -> Tuple[str, str]:
    """A line as it is matched: backticks kept where they can be, then
    without them, so a backticked id is still read."""
    return unmarked(line), unstyled(line)


_RECHECK_LINE = re.compile(r"(?P<fid>[A-Z][A-Z0-9]*-?\d+)\s*:\s*"
                           r"(?P<state>RESOLVED|UNRESOLVED|NOT\s+RESOLVED|INVALIDATED)\b(?P<rest>.*)$", re.IGNORECASE)


def recheck_line(line: str) -> Optional[Tuple[str, str, str]]:
    """One recheck line ``<id>: RESOLVED - <how>`` as ``(id, state, why)``,
    or None when the line is not one.

    ``state`` is RESOLVED, UNRESOLVED (NOT RESOLVED reads the same) or
    INVALIDATED, or the empty string for a RESOLVED or INVALIDATED that is
    not a plain acceptance (:func:`plain_acceptance`); ``why`` is then the
    reader's reason. UNRESOLVED keeps whatever follows it."""
    for form in _forms(line):
        found = _RECHECK_LINE.match(form)
        if found is None:
            continue
        state = re.sub(r"\s+", " ", found.group("state").upper())
        if state in ("UNRESOLVED", "NOT RESOLVED"):
            return found.group("fid"), "UNRESOLVED", _free_tail(found.group("rest"))
        plain, why = plain_acceptance(found.group("rest"), state)
        return found.group("fid"), (state if plain else ""), why
    return None


def unresolved_line(line: str) -> Optional[str]:
    """What a line opening with ``UNRESOLVED:`` names, under any formatting;
    None for any other line and for one that says none."""
    found = _prefixed(line)
    if found is None or found[0] != "UNRESOLVED" or is_empty_note(found[1]):
        return None
    return found[1]


_ROW = re.compile(r"(?P<oid>O-?\d+)\s*:\s*(?P<state>NOT\s+SUFFICIENT|SUFFICIENT|INSUFFICIENT)\b(?P<rest>.*)$",
                  re.IGNORECASE)


def row_line(line: str) -> Optional[Tuple[str, str, str]]:
    """One evidence row line ``<O-id>: SUFFICIENT - <why>`` as ``(id, state,
    why)``, or None when the line is not one. ``state`` is "sufficient",
    "insufficient", or the empty string for a SUFFICIENT that is not a plain
    acceptance, with the reader's reason as ``why``. INSUFFICIENT keeps
    whatever follows it."""
    for form in _forms(line):
        found = _ROW.match(form)
        if found is None:
            continue
        if found.group("state").upper() != "SUFFICIENT":
            # NOT SUFFICIENT is the same rejection in two words.
            return found.group("oid"), "insufficient", _free_tail(found.group("rest"))
        plain, why = plain_acceptance(found.group("rest"), "SUFFICIENT")
        return found.group("oid"), ("sufficient" if plain else ""), why
    return None


def requirement_line(line: str) -> Optional[Tuple[str, Optional[bool], str]]:
    """One per-requirement line of a review, ``R1: MET - <citation>``, as
    ``(id, met, rest)``, or None when the line is not one.

    ``met`` is True for a plain MET, False for NOT MET with whatever follows
    it, and None for a MET that is not a plain acceptance ("R1: MET, but
    only partly", "R1: MET once fixed", "R1: MET or NOT MET"): no verdict
    for that requirement, with the reader's reason as ``rest``. The rule
    :func:`audit_lines` applies to the auditor, for the reviewer's lines."""
    for form in _forms(line):
        found = _AUDIT_LINE.match(form)
        if found is None:
            continue
        rid = found.group(1).upper()
        if found.group(2).upper() != "MET":
            return rid, False, _free_tail(found.group(3))
        plain, why = plain_acceptance(found.group(3), "MET")
        return rid, (True if plain else None), why
    return None


def names_id(line: str, ids, words: str) -> Optional[str]:
    """The id among ``ids`` (upper case) that a line opens with as ``<id>:``
    when the rest holds one of the verdict ``words`` (a pattern), under any
    formatting; None otherwise. For a line that names an expected id and a
    verdict word in a form no reader takes ("RFT1-1: NOT YET RESOLVED",
    "O1: barely SUFFICIENT"): beside an accepting line for the same id it
    is a second, unread line, so the caller reads no verdict."""
    opened = re.match(r"([A-Z][A-Z0-9]*-?\d+)\s*:(.*)$", unstyled(line), re.IGNORECASE)
    if opened is None or opened.group(1).upper() not in ids:
        return None
    return opened.group(1).upper() if re.search(rf"\b(?:{words})\b", opened.group(2), re.IGNORECASE) else None


#: A READ reason that says the reading did not happen.
_NOT_READ = re.compile(r"(?:could\s*not|couldn'?t|cannot|can'?t|unable|did\s*not|didn'?t|failed|none|nothing|no)\b",
                       re.IGNORECASE)
_INSPECTION = re.compile(r"(?P<kind>READ|UNREAD)\s+(?P<gid>IG-[0-9a-f]{10})\b(?P<rest>.*)$", re.IGNORECASE)


def inspection_lines(reply: str) -> List[Tuple[str, str, str]]:
    """A reviewer's lines on the inspection gaps it was handed, as ``(kind,
    id, text)``: READ, UNREAD, or the empty string for a READ line that is
    not ``READ <id> - <what the files show>`` ("READ IG-... not yet", a READ
    with a condition, a READ with nothing after the id). A READ attests that
    files were read, so it is held to the stated form; UNREAD keeps whatever
    follows it."""
    out = []
    for raw in str(reply or "").splitlines():
        found = _INSPECTION.match(unstyled(raw))
        if found is None:
            continue
        gid = found.group("gid").upper()
        if found.group("kind").upper() == "UNREAD":
            out.append(("UNREAD", gid, _free_tail(found.group("rest"))))
            continue
        plain, why = plain_acceptance(found.group("rest"), "READ")
        # The stated form has a dash; "READ <id>: could not open either
        # file" is a colon and a refusal, so a colon does not stand in.
        dashed = re.match(r"\s*[-–—]", found.group("rest"))
        out.append(("READ" if plain and why and dashed and not _NOT_READ.match(why) else "", gid, why))
    return out
