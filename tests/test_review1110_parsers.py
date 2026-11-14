"""One way to read a verdict, and the reply parsers beside it (whole-repo
review, 2026-11-10).

The verdict readers in session.py had drifted into five readings of "is this
a finding", each stripping formatting its own way. They live in
quadratus.verdicts now, behind one normaliser, and the tables below hold
every reader to the two rules that settle a doubtful reply: formatting never
changes a verdict, and a reply accepts only when it says so plainly.

The second half covers the other module-level parsers the review named:
NEEDS, the directive lines, capture and fixture replies, the consult
request, the close-out and the audit line. Where a reader's rule changed in a
way a model must know, the prompt sentence is pinned here beside it.
"""

import inspect
import json

import pytest

from quadratus import session as S
from quadratus import verdicts as V
from quadratus.artifacts import ArtifactStore
from quadratus.scope import TaskScope
from quadratus.session import RunStalled, Session, SessionConfig, TaskSpec
from quadratus.task_kinds import TaskKind
from quadratus.taskmeta import AmbiguousMetadata

from .test_session import Recorder

# The OpenAI rows float by family since the merge (openai:sol, label "GPT
# Sol"); the names below are the ones a lead is shown now.
OPUS, SOL, GROK = "claude:opus", "openai:sol", "grok:default"
TRUST = [OPUS, SOL, GROK]


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/unused/cli")
    for vendor in ("OPENAI", "CLAUDE", "GROK"):
        monkeypatch.delenv(f"QUADRATUS_CLI_ARGS_{vendor}", raising=False)


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


# -- the normaliser -------------------------------------------------------------

@pytest.mark.parametrize("line, plain", [
    ("**RESOLVED**", "RESOLVED"),
    ("`RESOLVED`", "RESOLVED"),
    ("- **BLOCKING:** tenant leak", "BLOCKING: tenant leak"),
    ("* **BLOCKING**: tenant leak", "BLOCKING: tenant leak"),
    ("1. BLOCKING: tenant leak", "BLOCKING: tenant leak"),
    ("### BLOCKING: tenant leak", "BLOCKING: tenant leak"),
    ("- ## NO   FINDINGS", "NO FINDINGS"),
    ("> BLOCKING: quoted from the first review", "> BLOCKING: quoted from the first review"),   # a quote stays one
    ("_VERDICT: ACCEPT_", "VERDICT: ACCEPT"),
    ("the test_name in 2*3 px", "the test_name in 2*3 px"),     # content keeps its stars and underscores
    ("-1 is returned", "-1 is returned"),
])
def test_formatting_is_removed_and_wording_kept(line, plain):
    assert V.unstyled(line) == plain


def test_the_comparison_form_drops_trailing_punctuation_and_case():
    assert V.normalise("**Resolved.**") == V.normalise("RESOLVED!") == "resolved"
    assert V.normalise("```") == ""


def test_session_still_exposes_the_readers_under_their_private_names():
    for name, reader in (("_resolved_verdict", V.resolved_verdict), ("_has_blocking_finding", V.has_blocking_finding),
                         ("_has_prefixed_finding", V.has_prefixed_finding),
                         ("_has_security_finding", V.has_security_finding),
                         ("_security_verdict_line", V.security_verdict_line),
                         ("_security_finding_stands", V.security_finding_stands),
                         ("_review_complete", V.review_complete), ("_review_reading", V.review_reading),
                         ("_renders_blind", V.renders_blind), ("_design_judgement", V.design_judgement)):
        assert getattr(S, name) is reader, name
    assert S._REVIEW_COMPLETE == "REVIEW: COMPLETE" and S._RENDERS_BLIND == V.RENDERS_BLIND


# -- a recheck: RESOLVED (first half, item 2) -----------------------------------

@pytest.mark.parametrize("reply, resolved", [
    ("RESOLVED", True),
    ("RESOLVED.", True),                                   # the report's case: read as unresolved
    ("**RESOLVED**", True),                                # and this one
    ("`RESOLVED`", True),
    ("- RESOLVED", True),
    ("resolved", True),
    ("```\nRESOLVED\n```", True),
    ("RESOLVED\nThe escaping now handles surrogate pairs.", True),
    ("The escaping now handles surrogate pairs.\nRESOLVED", True),
    ("RESOLVED\nNothing is left unresolved.", True),       # the report's second case
    ("RESOLVED\nNo blocking finding remains unresolved.", True),
    ("RESOLVED\nNo unresolved findings remain.", True),
    ("> BLOCKING: the escaping drops surrogate pairs\nRESOLVED", True),   # its old finding, quoted
    # Anything that is not plainly an acceptance stays unresolved.
    ("", False),
    ("UNRESOLVED: the escaping still drops surrogate pairs", False),
    ("**UNRESOLVED:** the escaping still drops surrogate pairs", False),
    ("RESOLVED\nUNRESOLVED: one remains", False),
    ("RESOLVED\nRESOLVED", False),                         # a duplicate verdict
    ("RESOLVED: all good", False),                         # a prefix match
    ("Verdict: RESOLVED", False),
    ("Mostly fine.\nRESOLVED\nBut see below.", False),     # not at a boundary
    ("RESOLVED\nBLOCKING: the predicate is still not called", False),
    ("RESOLVED\nThe second finding is still unresolved.", False),
    ("RESOLVED\nNothing is left unresolved except the logging leak.", False),
    ("RESOLVED\nNot all findings are resolved; one is unresolved.", False),
    ("RESOLVED\nNo doubt it is unresolved.", False),
    ("RESOLVED\nNo, finding 2 remains unresolved.", False),
    ("RESOLVED\nThe first finding is not resolved.", False),
    # A sentence wrapped over two lines is still one sentence: the end of a
    # line must not cut "nothing is left unresolved" free of what follows or
    # precedes it, or split "not" from "resolved".
    ("RESOLVED\nNothing is left unresolved\nexcept the logging leak.", False),
    ("RESOLVED\nThe second finding is not\nresolved yet.", False),
    ("It is not the case that\nnothing is left unresolved\nRESOLVED", False),
    # A list item and a blank line do end one.
    ("- Finding 1: fixed in parse()\n- Nothing is left unresolved\nRESOLVED", True),
    ("RESOLVED\n\nNothing is left unresolved.\n\nThe escaping handles surrogate pairs now.", True),
])
def test_resolved_is_one_standalone_verdict_under_any_formatting(reply, resolved):
    assert V.resolved_verdict(reply) is resolved


@pytest.mark.parametrize("reply", [
    # Forgiving the full stop made a verdict line of a wrapped sentence's
    # last word. Each of these read as RESOLVED, and dropped the finding.
    "The second finding is not\nresolved.",
    "Finding 1 is fixed. Finding 2 is not\n**resolved**",
    "The leak is only partially\nresolved.",
    "I cannot say this is\nresolved",
    "- Finding 2 is not\n  resolved.",
    # In capitals the word is the marker, but not under a negation: this said
    # not resolved before the readers were unified, across the line break.
    "NOT\nRESOLVED",
    "Finding 2 was never\nRESOLVED.",
    "The second finding isn't\nRESOLVED",
    # The tail is text, so it still contradicts a verdict above it.
    "RESOLVED\nThe second finding is not\nresolved.",
])
def test_the_last_word_of_a_wrapped_sentence_is_not_a_verdict_line(reply):
    assert V.resolved_verdict(reply) is False


@pytest.mark.parametrize("reply", [
    "The escaping now handles surrogate pairs.\nResolved.",      # the line above closed its sentence
    "All three are fixed (see parse()).\nresolved",
    "The escaping now handles surrogate pairs\n\nResolved.",     # a blank line stands between
    "The escaping now handles surrogate pairs\nRESOLVED.",       # the marker in capitals stands
    "Checked:\nResolved.",
    "## Verdict\nresolved",                                      # a heading is no open sentence
    "```\npytest -q\n```\nResolved.",
    "RESOLVED\nEvery finding is now\nresolved.",                 # one verdict, and a sentence that agrees
])
def test_a_verdict_line_that_stands_apart_is_still_read(reply):
    assert V.resolved_verdict(reply) is True


# -- the security verdict line (first half, item 3) -----------------------------

@pytest.mark.parametrize("report, verdict", [
    ("VERDICT: ACCEPT", True),
    ("VERDICT: ACCEPT.", True),
    ("Verdict: accept", True),
    ("**VERDICT: ACCEPT**", True),
    ("VERDICT: **ACCEPT**", True),
    ("- VERDICT: ACCEPT", True),
    ("VERDICT: REJECT", False),
    ("**VERDICT:** REJECT", False),
    ("VERDICT: REJECT because the token is logged", False),    # a rejection, whatever follows
    ("VERDICT: REJECTED", False),
    ("VERDICT: ACCEPT\nsecond thoughts\nVERDICT: REJECT", False),
    ("VERDICT: REJECT\nfixed in review\nVERDICT: ACCEPT", True),     # the last line wins
    # A conditional or echoed verdict is a rejection (integrator's ruling 10:
    # it was no verdict line, and the marker scan could then pass it).
    ("VERDICT: ACCEPT once fixed", False),
    ("VERDICT: ACCEPT or VERDICT: REJECT", False),
    ("VERDICT: ACCEPT (no blocking findings)", False),
    ("VERDICT: ACCEPTED", False),
    ("VERDICT: maybe", False),
    ("The final VERDICT: ACCEPT", None),                             # no line opens with VERDICT
    # A conditional line after an acceptance is the last line, so it decides.
    ("VERDICT: ACCEPT\nVERDICT: ACCEPT once the logging is fixed", False),
    ("VERDICT: REJECT\nVERDICT: ACCEPT once the logging is fixed", False),
    # So does any later VERDICT line that is not a plain ACCEPT: these stayed
    # an acceptance, because a line with another word on it was not read.
    ("VERDICT: ACCEPT\nOn reflection:\nVERDICT: NOT ACCEPTED", False),
    ("VERDICT: ACCEPT\nVERDICT: undecided, the logging path is unread", False),
    ("VERDICT: REJECT\nVERDICT: undecided", False),
    ("VERDICT: NOT ACCEPTED\nVERDICT: ACCEPT", True),               # the last line still wins
])
def test_an_accept_counts_only_when_the_line_ends_there(report, verdict):
    assert V.security_verdict_line(report) is verdict


@pytest.mark.parametrize("report, stands", [
    # The report's case: the conditional line used to pass verification.
    ("This is BLOCKING - SQL injection.\nVERDICT: ACCEPT once fixed", True),
    ("BLOCKING: SQL injection in get_user\nVERDICT: ACCEPT once fixed", True),
    ("VERDICT: REJECT because the token is logged in clear text", True),
    ("Looks fine overall.\nVERDICT: REJECTED", True),
    ("BLOCKING: the token is logged in clear text\nVERDICT: ACCEPT", True),
    # Withdrawn by the line after it, which is itself the rejection.
    ("The token logging is BLOCKING.\nVERDICT: ACCEPT\nVERDICT: NOT ACCEPTED", True),
    ("The token must be fixed first.\nVERDICT: ACCEPT once fixed", True),   # ruling 10: no marker, still stands
    ("VERDICT: NOT ACCEPTED", True),
    ("Nothing here is BLOCKING for release.\n- BLOCKING: none found.\nVERDICT: ACCEPT", False),
    ("The guard is correct.\n**VERDICT: ACCEPT**", False),
])
def test_a_conditional_verdict_never_passes_on_the_line_alone(report, stands):
    assert V.security_finding_stands(report) is stands


def test_the_verifier_is_told_to_end_the_line_at_the_word(store):
    session = Session("goal", store, lambda *a, **k: "")
    prompt = session._verifier_prompt(TaskSpec("t1", "harden it"), "draft", OPUS)
    assert "Write nothing after the word on that line" in prompt
    assert "an ACCEPT followed by anything else is not read as an acceptance" in prompt
    assert V.security_verdict_line("VERDICT: ACCEPT once fixed") is False


# -- findings: one empty-finding rule (first half, item 7) ----------------------

EMPTY = ["BLOCKING: none", "BLOCKING: none.", "Blocking: n/a", "- **BLOCKING:** nothing", "BLOCKING: none found.",
         "BLOCKING: none identified", "### BLOCKING: nothing to report", "BLOCKING: none recorded"]
NAMED = ["BLOCKING: tenant leak in get_record", "- **BLOCKING:** tenant leak", "1. BLOCKING: the predicate is not called",
         "### BLOCKING: tenant leak", "blocking: none of the endpoints scope the lookup", "BLOCKING:",
         "BLOCKING: nothing tests negative numbers."]


@pytest.mark.parametrize("line", EMPTY)
def test_a_blocking_line_that_names_nothing_is_a_finding_to_no_reader(line):
    assert not V.has_blocking_finding(line)       # the report's case: this one said yes
    assert not V.has_prefixed_finding(line)
    assert not V.has_security_finding(line)
    # In a design review it is no blocking line either: no design judgement,
    # so no fix call is bought for "none".
    assert not V.design_judgement(V.design_blocking(line))


@pytest.mark.parametrize("line", NAMED)
def test_a_blocking_line_that_names_something_is_a_finding_to_every_reader(line):
    assert V.has_blocking_finding(line) and V.has_prefixed_finding(line) and V.has_security_finding(line)
    assert V.design_blocking("APPROVED\n" + line), "one blocking line overrides an APPROVED beside it"


def test_unresolved_is_a_security_prefix_and_not_a_review_one():
    assert V.has_prefixed_finding("UNRESOLVED: the missing-owner case raises KeyError")
    assert not V.has_blocking_finding("UNRESOLVED: the missing-owner case raises KeyError")
    assert not V.has_prefixed_finding("- UNRESOLVED: nothing remains")


def test_a_quoted_line_is_not_a_prefixed_finding_but_its_capitals_still_stop_a_security_report():
    quoted = "> BLOCKING: the token is logged in clear text"
    assert not V.has_blocking_finding(quoted) and not V.has_prefixed_finding(quoted)
    assert V.has_security_finding(quoted), "the marker scan reads capitals wherever they are, as before"


@pytest.mark.parametrize("text, finding", [
    ("No `BLOCKING` findings.", False),            # backticks used to hide the negation
    ("_BLOCKING_", True),
    ("One NON-BLOCKING note", False),
    ("This authorization defect is BLOCKING.", True),
])
def test_the_prose_marker_scan_reads_the_unstyled_line(text, finding):
    assert V.has_security_finding(text) is finding


# -- a collaborator's review (first half, item 8) -------------------------------

@pytest.mark.parametrize("review, clean, complete", [
    ("NO FINDINGS", True, True),
    ("No findings.", True, True),
    ("**NO FINDINGS**", True, True),                          # the report's case: an unfinished review
    ("NO FINDINGS\nREVIEW: COMPLETE", True, True),            # the report's case: bought a revision
    ("- NO FINDINGS\n- **Review: complete**", True, True),
    # Ruling 11: nothing to answer, so no revision; but none of the three
    # forms that make a reply a verdict, so an unfinished review.
    ("BLOCKING: none", True, False),
    ("BLOCKING: none\nREVIEW: COMPLETE", True, True),
    ("REVIEW: COMPLETE", True, True),
    # Something was said, so it reaches the lead; the verdict is still a verdict.
    ("NO FINDINGS\nOne note: the helper could be shorter.", False, True),
    ("Minor: rename x.\nREVIEW: COMPLETE", False, True),
    ("Minor: rename x.\nBLOCKING: none", False, False),
    ("BLOCKING: the escaping drops surrogate pairs", False, True),
    ("notes\n- **BLOCKING:** malformed numbers accepted", False, True),
    ("NO FINDINGS\nBLOCKING: the escaping drops surrogate pairs", False, True),
    # A reply that arrived is not a review that finished.
    ("UNFINISHED: did not reach a verdict", False, False),
    ("NO FINDINGS so far, I only read half", False, False),
    ("## Part one\nNO FINDINGS\n## Part two\nI ran out of rounds before reading this.", False, False),
    ("", False, False),
    # UNRESOLVED is the recheck's word: no verdict here, so not a clean review
    # either. It was both recorded as unfinished and withheld from the lead.
    ("UNRESOLVED: none", False, False),
    ("UNRESOLVED: none\nREVIEW: COMPLETE", False, True),
    # A closing "no findings." that ends a wrapped sentence is that sentence,
    # not the marker; apart from the text above it, it is the marker.
    ("I ran out of rounds, so for the second file there are\nno findings.", False, False),
    ("I read both files and the tests.\nNo findings.", False, True),
    ("I read both files and the tests\nNO FINDINGS", False, True),
])
def test_a_clean_review_and_a_finished_review_are_read_by_one_reader(review, clean, complete):
    assert V.no_findings(review) is clean
    assert V.review_complete(review) is complete
    lone = [line for line in review.splitlines() if line.strip()]
    assert not clean or complete or all(V.is_empty_note(line.split(":", 1)[-1]) for line in lone), (
        "a clean review is a finished one, except a reply that is only 'BLOCKING: none'")


class Reviewed(Recorder):
    """A standard task whose one collaborator answers as scripted."""

    def __init__(self, review, recheck="RESOLVED"):
        super().__init__()
        self.review, self.recheck = review, recheck

    def __call__(self, model, prompt, system=None):
        if "contributing an independent read" in prompt:
            self.calls.append({"model": model, "prompt": prompt})
            return self.review
        if "Check only your BLOCKING findings" in prompt:
            self.calls.append({"model": model, "prompt": prompt})
            return self.recheck
        return super().__call__(model, prompt, system)

    def asked(self, text):
        return [c["prompt"] for c in self.calls if text in c["prompt"]]


def _review(store, review, recheck="RESOLVED", **config):
    rec = Reviewed(review, recheck)
    session = Session("Build a JSON parser", store, rec, config=SessionConfig(**config))
    session.run_task(TaskSpec("t1", "work", complexity=S.Complexity.STANDARD))
    return session, rec


@pytest.mark.parametrize("review", ["NO FINDINGS\nREVIEW: COMPLETE", "**NO FINDINGS**", "BLOCKING: none",
                                    "BLOCKING: none\nREVIEW: COMPLETE"])
def test_a_clean_review_buys_no_revision_and_opens_no_finding(store, review):
    session, rec = _review(store, review)
    assert not rec.asked("Revise your work"), "a revision against an empty critique"
    assert not rec.asked("Check only your BLOCKING findings"), "a recheck of a finding that does not exist"
    # Ruling 11: "BLOCKING: none" by itself is none of the three forms that
    # make a reply a verdict, so it is the one clean reply recorded unfinished.
    assert any("unfinished review" in f for f in session.open_findings) is (review == "BLOCKING: none")


def test_a_formatted_resolved_ends_the_fix_cycle(store):
    session, rec = _review(store, "BLOCKING: the escaping drops surrogate pairs", recheck="**RESOLVED.**")
    assert len(rec.asked("Revise your work")) == 1
    assert not rec.asked("These blocking findings remain unresolved"), "RESOLVED. bought another fix round"
    assert not any("RESOLVED" in f for f in session.open_findings)


# -- a design review (second half, item 6) --------------------------------------

BLIND = "BLOCKING: the renders do not show the changed interface"


@pytest.mark.parametrize("verdict, blocking", [
    ("APPROVED", []),
    ("\n  APPROVED  \n", []),
    ("APPROVED\nBLOCKING: the delete button overlaps the count", ["BLOCKING: the delete button overlaps the count"]),
    ("- **BLOCKING:** the hint squeezes the field to 46 px.", ["BLOCKING: the hint squeezes the field to 46 px."]),
    ("### blocking: contrast is too low", ["BLOCKING: contrast is too low"]),
    ("  BLOCKING: contrast is too low  ", ["BLOCKING: contrast is too low"]),
])
def test_the_design_review_is_read_whole_and_its_blocking_lines_under_any_formatting(verdict, blocking):
    assert V.design_blocking(verdict) == blocking
    assert V.review_reading({"verdict": verdict}) == ("APPROVED" if not blocking else "not approved: " + blocking[0])


@pytest.mark.parametrize("verdict", ["looks fine", "", "APPROVED\nThe layout holds at a mobile width.",
                                     "APPROVED with one reservation", "BLOCKING: none", "APPROVED\nBLOCKING: none",
                                     # Only the exact word approves (the evidence boundary's rule, kept):
                                     "APPROVED.", "Approved.", "approved", "**APPROVED**",
                                     "UNFINISHED: the design review stopped at 20 rounds before giving a verdict."])
def test_anything_short_of_the_exact_approval_is_no_verdict(verdict):
    blocking = V.design_blocking(verdict)
    assert len(blocking) == 1 and blocking[0].startswith("BLOCKING: the final design review gave no verdict")
    assert not V.design_judgement(blocking), "the harness's own line is never a design judgement"


@pytest.mark.parametrize("line", [BLIND, "BLOCKING: The renders do not show the changed interface.",
                                  "- **BLOCKING:** the renders do not show the changed interface.",
                                  "blocking:  The Renders Do Not Show The Changed Interface"])
def test_the_blind_reply_is_never_also_a_design_judgement(line):
    # The report's case: blind=True and judgement=True for the capitalised
    # line, so a capture defect bought a design-fix call as well.
    assert V.renders_blind(line)
    assert not V.design_judgement(V.design_blocking(line))


@pytest.mark.parametrize("lines, judgement", [
    (["BLOCKING: the hint squeezes the field to 46 px"], True),
    (["BLOCKING: contrast is too low", "BLOCKING: the header overlaps the toolbar"], True),
    (["BLOCKING: The Renders Could Not Be Handed to the reviewer (x)"], False),
    (["**BLOCKING:** the renders changed while they were being prepared for review"], False),
    (["BLOCKING: contrast is too low", "BLOCKING: the reviewer altered its copy"], False),
    ([], False),
])
def test_only_the_reviewers_own_lines_are_a_design_judgement(lines, judgement):
    assert V.design_judgement(lines) is judgement


def test_a_capitalised_blind_reply_with_no_redeclaration_buys_no_design_fix(tmp_path, monkeypatch):
    """The full path: the redeclaration yields nothing, the verdict stands,
    and the same line must not then be treated as a design judgement. The
    scripted reviews hold one reply, so a design-fix call would fail the
    test by asking for a second."""
    from tests.test_recapture_round import _run
    session, spec, prompts, captures = _run(
        tmp_path, monkeypatch, ["BLOCKING: The renders do not show the changed interface."], "CAPTURE: none")
    record = session.design_checks[0]
    assert "review_fix" not in record
    assert not [p for _, p in prompts if "found these blocking design problems" in p]
    assert len(captures) == 1 and record["final_review"]["approved"] is False


# -- the capture redeclaration (first half, item 9) -----------------------------

def test_prose_with_the_marker_still_voids_a_declaration_and_the_prompt_says_so(tmp_path, monkeypatch):
    """The reader stays as strict as it was left: any second marker is a
    conflict. What changed is that the redeclaring call is told."""
    reply = 'The previous capture: timed out on step 1.\nCAPTURE: {"path": "/", "steps": []}'
    capture, why = S._parse_capture_line(reply, "t1")
    assert capture is None and "1 capture declaration(s) and 1 conflicting line(s)" in why
    assert "Write the word capture followed by a colon nowhere else in the reply" in S._ONE_CAPTURE_MARKER
    assert "start no other line with {" in S._ONE_CAPTURE_MARKER
    from tests.test_recapture_round import BLIND as blind
    from tests.test_recapture_round import REACHED, _run
    _, _, prompts, _ = _run(tmp_path, monkeypatch, [blind, "APPROVED"], "CAPTURE: " + json.dumps(REACHED))
    ask = next(p for _, p in prompts if "Reply with exactly one line" in p)
    assert S._ONE_CAPTURE_MARKER in ask


# -- the audit line (second half, item 11) --------------------------------------

@pytest.mark.parametrize("reply, verdicts", [
    ("R1: MET - app.py", [("R1", True, "app.py")]),
    ("**R1: MET** - `tests/test_a.py::test_b`", [("R1", True, "tests/test_a.py::test_b")]),
    ("- r1: met \u2014 README.md", [("R1", True, "README.md")]),
    ("R1: MET: app.py", [("R1", True, "app.py")]),
    ("R1: MET", [("R1", True, "")]),                 # no citation: the caller counts it as not met
    ("R1: NOT MET - the button is missing", [("R1", False, "the button is missing")]),
    ("R1: the parser accepts empty input\nR1: MET - app.py", [("R1", True, "app.py")]),   # a restated requirement
    ("R1: MET - app.py\nR1: MET - app.py", [("R1", True, "app.py")]),                     # the same line twice
    ("R1: NOT MET - a\nR1: NOT MET - b", [("R1", False, "a; b")]),
])
def test_the_audit_line_is_read_as_asked(reply, verdicts):
    assert V.audit_lines(reply) == verdicts


@pytest.mark.parametrize("reply", [
    "R1: NOT MET - missing\nR1: MET - README.md",              # the report's case: ended as MET
    "R1: MET - README.md\nR1: NOT MET - missing",
    "R1: MET - a.py\nR1: MET - b.py",
    "R1: MET, but only partly; R2 untested - app.py",           # the report's case: any MET prefix was MET
    "R1: MET but the README disagrees - app.py",
    "R1: met by the tests in tests/test_a.py",
    "R1: MET (see app.py)",
])
def test_a_conflicting_or_qualified_met_is_no_verdict(reply):
    (rid, met, why), = V.audit_lines(reply)
    assert rid == "R1" and met is False and why


def test_the_audit_does_not_take_the_last_line_for_a_requirement(tmp_path):
    from tests.test_requirements_ledger import PLAN, Script
    (tmp_path / "README.md").write_text("docs\n")
    script = Script([PLAN.replace("COVERS: R1", "COVERS: R1, R2, R3"), "DONE", "DONE"],
                    audits=["R1: NOT MET - no endpoint\nR1: MET - README.md\n"
                            "R2: MET, but only partly - README.md\nR3: MET - README.md"] * 2)
    session = Session("Build the preview", ArtifactStore(tmp_path / "a"), script,
                      config=SessionConfig(project=tmp_path, requirements_ledger=True, max_requirement_reopens=0,
                                           project_excludes=(tmp_path / "a",)))
    session.run(max_tasks=3)
    status = session.memory.ledger.requirement_status
    assert status["R1"].startswith("NOT MET") and "verdict lines" in status["R1"]
    assert status["R2"].startswith("NOT MET") and "not a verdict line" in status["R2"]
    assert status["R3"] == "met (audited)"
    assert not session.completed
    audit = next(p for _, p in script.prompts if "independent auditor" in p)
    assert S._AUDIT_LINE_RULE in audit


def test_the_auditor_is_told_the_line_rules():
    assert "MET is followed at once by ' - ' and the citation" in S._AUDIT_LINE_RULE
    assert "Two different lines for one requirement are read as no verdict" in S._AUDIT_LINE_RULE


# -- what a design task is (first half, item 5) ---------------------------------

@pytest.mark.parametrize("paths, design", [
    (["static"], True),                 # the report's case: a bare UI directory skipped capture and review
    (["templates"], True),
    (["app/templates/"], True),
    (["src/components"], True),
    (["static/app.css"], True),
    (["app.py", "tests/test_app.py"], False),
    (["views.py"], False),
    (["app"], False),                   # a parent directory names nothing rendered; see the docstring
])
def test_a_bare_ui_directory_is_a_rendered_scope(paths, design):
    spec = TaskSpec("t1", "x", kind=TaskKind.BACKEND, scope=TaskScope(permitted_paths=paths))
    assert S.is_design_task(spec) is design
    frontend = TaskSpec("t1", "x", kind=TaskKind.FRONTEND, scope=TaskScope(permitted_paths=paths))
    assert S._frontend_without_rendered_file(frontend) is (not design), "the two tests share one rule"


def test_the_capture_rules_say_a_ui_directory_itself_counts():
    assert "any permitted path is, or is under, a templates, static" in S._CAPTURE_SCOPE_REQUEST


# -- NEEDS (first half, item 6) -------------------------------------------------

@pytest.mark.parametrize("description, needs, kept", [
    ('NEEDS: ["execute"]\nRun the checks.', {"execute"}, "Run the checks."),
    ('needs: ["patch"]\nEdit it.', {"patch"}, "Edit it."),
    ("NEEDS: []\nWrite the note.", set(), "Write the note."),
    # The report's case: prose, not a declaration that failed.
    ("Needs: a migration for the users table\nAdd the column.", set(),
     "Needs: a migration for the users table\nAdd the column."),
    # Indented and not in capitals: prose, like the line above.
    ("Add the column.\n  Needs: a migration first", set(), "Add the column.\n  Needs: a migration first"),
])
def test_a_needs_line_is_the_word_at_the_margin_and_a_json_list(description, needs, kept):
    declared, rest = S._read_task_needs(description)
    assert declared == frozenset(needs) and rest == kept


@pytest.mark.parametrize("line", ["NEEDS: a migration for the users table", "NEEDS: execute", "NEEDS: {}",
                                  'NEEDS: "execute"', "NEEDS:", 'NEEDS: ["shell"]', "NEEDS: [true]",
                                  'needs: ["shell"]'])
def test_a_needs_declaration_that_cannot_be_read_is_a_problem_to_send_back(line):
    with pytest.raises(ValueError, match="NEEDS"):
        S._read_task_needs(line + "\nDo it.")
    with pytest.raises(AmbiguousMetadata, match="invalid task requirements"):
        S._read_metadata("KIND: docs rote\n" + line + "\nDo it.")


@pytest.mark.parametrize("line", ['  NEEDS: ["execute"]', "\tNEEDS: execute", '    NEEDS : ["patch"]'])
def test_an_indented_needs_line_in_capitals_is_sent_back_not_dropped(line):
    # Before the review it was read as a declaration. Reading it as prose
    # would run the task without what it declared, and say nothing.
    with pytest.raises(ValueError, match="starts at the left margin"):
        S._read_task_needs("Do it.\n" + line)
    with pytest.raises(AmbiguousMetadata, match="invalid task requirements.*starts at the left margin"):
        S._read_metadata("KIND: docs rote\nDo it.\n" + line)


def test_an_indented_needs_line_gets_the_one_correction(store):
    replies = ['KIND: docs rote\nUpdate the results.\n  NEEDS: ["execute"]',
               'KIND: docs rote\nNEEDS: ["execute"]\nUpdate the results.']
    prompts = []

    def invoke(model, prompt, system=None):
        prompts.append(prompt)
        return replies.pop(0)
    spec = Session("fix", store, invoke).next_task()
    assert spec.needs == frozenset({"execute"}) and spec.description == "Update the results."
    assert len(prompts) == 2 and "starts at the left margin" in prompts[1]


def test_a_prose_needs_line_names_a_task_and_stays_in_it(store):
    calls = []
    session = Session("fix", store, lambda *a: calls.append(a) or (
        "KIND: backend simple\nAdd the column.\nNeeds: a migration for the users table"))
    spec = session.next_task()
    assert len(calls) == 1 and "Needs: a migration for the users table" in spec.description


def test_an_unreadable_needs_line_gets_the_one_correction_not_a_stall(store):
    replies = ["KIND: docs rote\nNEEDS: a migration for the users table\nUpdate the results.",
               'KIND: docs rote\nNEEDS: ["execute"]\nUpdate the results.']
    prompts = []

    def invoke(model, prompt, system=None):
        prompts.append(prompt)
        return replies.pop(0)
    spec = Session("fix", store, invoke).next_task()
    assert spec.needs == frozenset({"execute"}) and spec.description == "Update the results."
    assert len(prompts) == 2 and "CORRECTION REQUIRED" in prompts[1]
    assert "is not a NEEDS declaration" in prompts[1]


def test_the_needs_rule_is_stated():
    assert "starts at the left margin and the rest of it is the JSON list" in S._NEEDS_REQUEST
    assert "is sent back once for correction" in S._NEEDS_REQUEST
    assert "two NEEDS lines that disagree are refused with no correction" in S._NEEDS_REQUEST


def test_a_lead_draft_is_not_read_for_task_directives():
    # _parse_kind reads leads' drafts: a NEEDS or TIER line there is prose.
    draft = "KIND: backend simple\nNEEDS: more time\nTIER: direct\nTIER: normal\nThe draft."
    assert S._parse_kind(draft)[2] == "NEEDS: more time\nTIER: direct\nTIER: normal\nThe draft."


# -- directive lines (first half, item 17) --------------------------------------

def test_covers_on_more_than_one_line_is_one_declaration():
    # The report's case: R4 was dropped without a word.
    ids, spec = S._read_covers(TaskSpec("t1", "Build it.\nCOVERS: R1\nThen the docs.\nCOVERS: R4, R1"))
    assert ids == ["R1", "R4"] and spec.description == "Build it.\n\nThen the docs."


@pytest.mark.parametrize("lines, problem", [
    ("TIER: direct\nTIER: normal", "more than one TIER line and they disagree (direct, normal)"),
    ("CONTINUES: t1\nCONTINUES: t2", "more than one CONTINUES line naming different tasks (t1, t2)"),
])
def test_two_directive_lines_that_disagree_are_sent_back(lines, problem):
    with pytest.raises(AmbiguousMetadata) as raised:
        S._read_metadata("KIND: backend simple\nDo it.\n" + lines)
    assert problem in str(raised.value)


@pytest.mark.parametrize("lines, reader, value", [
    ("TIER: direct\nTIER: Direct", S._read_tier, "direct"),
    ("CONTINUES: t1\nCONTINUES: t1", S._read_continues, "t1"),
    ("RESOLVES: F1, F2\nRESOLVES: F1 F2", S._read_resolves, ["F1", "F2"]),
])
def test_a_directive_line_repeated_word_for_word_is_one_line(lines, reader, value):
    meta = S._read_metadata("KIND: backend simple\nDo it.\n" + lines)
    assert len(S._RESOLVES.findall(meta.description)) <= 1, "the dispatch loop counts RESOLVES lines"
    got, spec = reader(TaskSpec("t1", meta.description))
    assert got == value and spec.description == "Do it."


def test_resolves_on_two_lines_that_differ_reaches_the_loops_own_refusal():
    meta = S._read_metadata("KIND: backend simple\nDo it.\nRESOLVES: F1\nRESOLVES: F2")
    assert len(S._RESOLVES.findall(meta.description)) == 2
    assert S._read_resolves(TaskSpec("t1", meta.description))[0] == ["F1", "F2"], "nothing is dropped unread"


def test_read_directly_a_conflict_never_takes_the_first_line():
    assert S._read_tier(TaskSpec("t1", "Do it.\nTIER: direct\nTIER: normal"))[0] == "normal"
    assert S._read_continues(TaskSpec("t1", "Do it.\nCONTINUES: t1\nCONTINUES: t2"))[0] is None
    assert S._read_hypothesis(TaskSpec("t1", "Do it.\nHYPOTHESIS: a\nHYPOTHESIS: b"))[0] == "a / b"


def test_conflicting_tier_lines_get_the_one_correction(store):
    replies = ["KIND: backend simple\nDo it.\nTIER: direct\nTIER: normal", "KIND: backend simple\nDo it.\nTIER: direct"]
    prompts = []

    def invoke(model, prompt, system=None):
        prompts.append(prompt)
        return replies.pop(0)
    spec = Session("fix", store, invoke).next_task()
    assert len(prompts) == 2 and "more than one TIER line and they disagree" in prompts[1]
    assert S._read_tier(spec)[0] == "direct"


def test_a_listed_task_with_conflicting_lines_is_refused_by_name(store):
    session = Session("fix", store, lambda *a, **k: pytest.fail("no model call"),
                      config=SessionConfig(requirements_ledger=False))
    with pytest.raises(S.TaskListInvalid, match="more than one CONTINUES line"):
        session.run(tasks=["KIND: backend simple\nDo it.\nCONTINUES: t1\nCONTINUES: t2"])


def test_the_one_line_rules_are_stated_where_the_lines_are_asked_for():
    assert "A task carries one TIER line: two that disagree are sent back once" in S._tier_request(40)
    assert "two CONTINUES lines naming different tasks are sent back once" in S._ONE_CONTINUES_LINE
    for method in (Session._close_turn_limited, Session._close_failed):
        assert "_ONE_CONTINUES_LINE" in inspect.getsource(method), method.__name__


# -- the check runner label (first half, item 18) -------------------------------

@pytest.mark.parametrize("argv, label", [
    (["pytest", "-m", "slow", "tests"], "pytest"),           # the report's case: "a project command"
    (["python", "-m", "pytest", "-q"], "pytest"),
    (["python3", "-m", "pytest", "-m", "slow"], "pytest"),
    (["/usr/bin/python3.12", "-X", "dev", "-m", "unittest"], "unittest"),
    (["python", "tools/run.py", "-m", "slow"], "python"),    # the script's own -m
    (["python3", "tools/run.py"], "python3"),
    (["python3.12", "tools/run.py"], "python"),
    (["python", "-m", "tools.private_runner"], "a project command"),
    (["node", "--test", "tests/ui/a.test.js"], "node"),
    (["./tools/check.sh", "-m", "pytest"], "a project command"),
    ([], "a project command"),
    (["python", "-c", "import x", "-m", "pytest"], "python"),    # the code's own argument
    (["pytest", "tests/python", "-m", "slow"], "pytest"),        # a path ending in "python" is no interpreter
    (["coverage", "report", "-m"], "a project command"),
])
def test_only_pythons_own_m_names_the_runner(argv, label):
    assert S._check_runner(argv) == label


@pytest.mark.parametrize("command", [
    "uv run python -m pytest tests", "poetry run python -m pytest", "hatch run python -m pytest",
    "env PYTHONPATH=. python -m pytest", "timeout 600 python -m pytest", "xvfb-run python -m pytest",
    "coverage run -m pytest", "coverage run --branch -m pytest -q", "py -m pytest", "python.exe -m pytest",
    r"C:\Python\python.exe -m pytest", "/opt/venv/bin/python3.12 -X dev -m pytest",
])
def test_python_behind_a_wrapper_still_names_its_module(command):
    # Each of these was labelled pytest before the review. Reading -m from
    # argv[0] alone made them "uv", "poetry" or "a project command", and a
    # goal that requires pytest would no longer find it among the checks.
    assert S._check_runner(command.split()) == "pytest"


@pytest.mark.parametrize("command, label", [
    ("uv run pytest -m slow", "uv"), ("timeout 600 pytest -m slow", "a project command"),
    ("env python", "a project command"), ("uv run python tools/run.py -m slow", "uv"),
])
def test_a_wrapper_without_pythons_m_keeps_its_own_label(command, label):
    assert S._check_runner(command.split()) == label


# -- who a consult names (first half, item 19; second half, item 18) ------------

@pytest.mark.parametrize("name, keys", [
    ("Sol", [SOL]), ("sol", [SOL]), ("GPT Sol", [SOL]), ("GPT", [SOL]), ("gpt", [SOL]),
    ("openai:sol", [SOL]), ("gpt sol", [SOL]),
    ("Opus", [OPUS]), ("Claude Opus", [OPUS]), ("Opus 5", [OPUS]), ("Claude", [OPUS]), ("`Opus`", [OPUS]),
    ("Grok", [GROK]), ("Grok 4.6", [GROK]),
    # The report's cases: a substring test resolved these to whoever came first.
    ("5", []), ("5.6", []), ("o", []), ("s", []), ("ro", []), ("pus", []),
    ("the docs", []), ("Gemini", []), ("", []),
])
def test_a_member_is_named_by_whole_words(name, keys):
    assert S._member_matches(name, TRUST) == keys


def test_the_lead_itself_and_a_stranger_do_not_resolve(store):
    session = Session("goal", store, lambda *a, **k: "")
    # Grok left the brain trust on the development line (owner directive,
    # 2026-11-01); it is still a roster row a consult parser can be handed.
    assert session.brain_trust == [OPUS, SOL]
    assert session._resolve_consultant("Sol", OPUS) == SOL
    assert session._resolve_consultant("Opus", OPUS) is None, "the lead cannot consult itself"
    assert session._resolve_consultant("5", OPUS) is None
    assert session._resolve_consultant("o", SOL) is None


@pytest.mark.parametrize("reply, requests", [
    ("CONSULT Sol: is bcrypt still the right choice?", [("Sol", "is bcrypt still the right choice?")]),
    ("CONSULT Sol: is 12:30 a valid time?", [("Sol", "is 12:30 a valid time?")]),
    ("CONSULT Sol: ask Opus: is the lock needed?", [("Sol", "ask Opus: is the lock needed?")]),
    ("CONSULT claude:opus: is the lock needed?", [("claude:opus", "is the lock needed?")]),
    ("CONSULT Sol: one?\nCONSULT Opus: two?", [("Sol", "one?"), ("Opus", "two?")]),
    ("CONSULT Grok 4.6: hmm?", [("Grok 4.6", "hmm?")]),
    # A name that mentions a member was meant for the roster: still a request,
    # refused in words when it does not resolve.
    ("CONSULT Sol (GPT-5.6): is bcrypt right?", [("Sol (GPT-5.6)", "is bcrypt right?")]),
    # The report's case: a draft that opens this way is not a consult request.
    ("CONSULT the docs: they say X.\nSo the draft follows them.", []),
    ("CONSULT 5: which version?\nMeanwhile, the draft.", []),
    ("CONSULT Sol: one?\nCONSULT the docs: they say X.\nSo the draft follows them.", [("Sol", "one?")]),
    ("A draft that mentions CONSULT Sol: in passing.", []),
    # A reply that is CONSULT lines and nothing else can only be a request:
    # an unknown name is returned for the caller to refuse in words, as it
    # was before the review, never read as prose (which fails the task).
    ("CONSULT Gemini: is bcrypt still the right choice?", [("Gemini", "is bcrypt still the right choice?")]),
    ("CONSULT 5: which version?", [("5", "which version?")]),
    ("CONSULT Sol: one?\nCONSULT the reviewer: two?", [("Sol", "one?"), ("the reviewer", "two?")]),
    ("CONSULT Sol", []),                                  # no colon: never a request
])
def test_a_consult_request_names_a_brain_trust_member(reply, requests):
    assert S._parse_consults(reply, TRUST) == requests


def test_a_long_line_of_colons_is_not_matched_colon_by_colon(monkeypatch):
    # Every colon on the line was tried as the end of a member's name, each
    # try reading the line up to it: 2,000 colons took a second, 20,000 over
    # a minute. A name is short, so only a colon near the start can end one.
    tried = []
    matches = S._member_matches
    monkeypatch.setattr(S, "_member_matches", lambda name, keys: tried.append(name) or matches(name, keys))
    reply = "CONSULT " + "a: " * 20000 + "\nSo the draft follows."
    assert S._parse_consults(reply, TRUST) == []
    assert len(tried) <= S._MEMBER_NAME_CHARS
    # A full roster key, colon and all, is still read whole.
    assert S._parse_consults("CONSULT openai:sol: is bcrypt right: yes or no?", TRUST) == [
        ("openai:sol", "is bcrypt right: yes or no?")]


def test_a_name_that_only_mentions_a_member_is_refused_in_words_not_resolved(store):
    session = Session("goal", store, lambda *a, **k: "")
    assert S._mentions_member("Sol (GPT-5.6)", TRUST) and not S._mentions_member("the solution notes", TRUST)
    assert session._resolve_consultant("Sol (GPT-5.6)", OPUS) is None
    assert session._resolve_consultant("the Grok docs", OPUS) is None


def test_without_the_roster_the_parser_reads_as_before():
    assert S._parse_consults("CONSULT the docs: they say X.") == [("the docs", "they say X.")]


def test_a_draft_opening_with_a_stranger_is_never_served_as_a_consult(store):
    class Drafting(Recorder):
        def __call__(self, model, prompt, system=None):
            if "You are leading" in prompt:
                self.calls.append({"model": model, "prompt": prompt})
                return "CONSULT the docs: they say X.\nSo the draft follows them."
            return super().__call__(model, prompt, system)

    rec = Drafting()
    session = Session("Build a JSON parser", store, rec)
    summary = session.run_task(TaskSpec("t1", "work", complexity=S.Complexity.SIMPLE))
    assert not any("area of strength" in c["prompt"] for c in rec.calls), "no consultant was called"
    assert not any("not a member you can consult" in c["prompt"] for c in rec.calls), "no consult was charged"
    # The rule the lead's prompt states: it is no request. Integrator's
    # ruling 13: a draft that opens this way and carries on is a delivery,
    # where it used to fail the task on its channel.
    assert getattr(summary, "outcome", "closed") != "failed" and "t1" not in session.failed_records
    lead = next(c["prompt"] for c in rec.calls if "You are leading" in c["prompt"])
    assert S._CONSULT_NAMES_RULE in lead
    assert "naming no member and then goes on to other text is not read as a request" in S._CONSULT_NAMES_RULE
    assert "it is read as your delivery" in S._CONSULT_NAMES_RULE


def test_a_consult_that_names_a_stranger_is_refused_in_words_and_the_lead_asked_again(store):
    """Nothing but a CONSULT line: the lead mistyped a name. Before the
    review it was told so and asked again; reading the line as prose failed
    the whole task instead."""
    class Mistyped(Recorder):
        asked = 0

        def __call__(self, model, prompt, system=None):
            if "You are leading" in prompt:
                self.calls.append({"model": model, "prompt": prompt})
                self.asked += 1
                return "CONSULT Gemini: is bcrypt still the right choice?" if self.asked == 1 else "The draft."
            return super().__call__(model, prompt, system)

    rec = Mistyped()
    session = Session("Build a JSON parser", store, rec)
    summary = session.run_task(TaskSpec("t1", "work", complexity=S.Complexity.SIMPLE))
    leading = [c["prompt"] for c in rec.calls if "You are leading" in c["prompt"]]
    assert len(leading) == 2 and "Gemini is not a member you can consult." in leading[1]
    assert not any("area of strength" in c["prompt"] for c in rec.calls), "no consultant was called"
    assert getattr(summary, "outcome", "closed") != "failed" and "t1" not in session.failed_records
    assert "a consult naming anyone else is refused and still counts against the allowance" in S._CONSULT_NAMES_RULE


# -- one path normaliser (first half, item 20; second half, item 14) ------------

@pytest.mark.parametrize("path, same", [
    (".env", ".env"), ("./.env", ".env"), ("env", "env"), ("./env", "env"),
    (".github/ci.yml", ".github/ci.yml"), ("./a//b.py", "a/b.py"), ("static/", "static"),
])
def test_a_leading_dot_slash_is_dropped_and_nothing_else(path, same):
    assert S._scope_path(path) == same


def test_dot_files_are_not_the_files_without_the_dot():
    # The report's case: strip("./") made ".env" and "env" one path.
    assert S._literal_paths(TaskScope([".env"])) == {".env"}
    assert S._literal_paths(TaskScope([".env"])) != S._literal_paths(TaskScope(["env"]))
    assert not (S._literal_paths(TaskScope([".github/ci.yml"])) & S._literal_paths(TaskScope(["github/ci.yml"])))
    assert S._literal_paths(TaskScope(["src/*.py"])) is None


def test_the_merge_check_compares_paths_the_same_way():
    source = inspect.getsource(Session._run_batch)
    assert "_scope_path(p) not in allowed" in source and 'strip("./")' not in source


def test_a_batch_scoped_to_a_dot_file_and_its_namesake_shares_no_file(tmp_path):
    from tests.test_parallel_tasks import Orchestrated, _block, _session
    script = Orchestrated(["PARALLEL\n" + _block(".github/ci.yml") + "\n---\n" + _block("github/ci.yml")])
    notes = []
    session, _ = _session(tmp_path, script, requirements_ledger=False, progress=notes.append)
    first = session.next_task()
    assert first is not None and len(session._batch) == 1, notes
    assert not any("files shared between tasks" in n for n in notes)


# -- fixture blocks (first half, item 22) ---------------------------------------

@pytest.mark.parametrize("reply, found, problem", [
    ("FIXTURE a.csv:\n```\n1,2\n```", {"a.csv": "1,2"}, ""),
    ("FIXTURE a.csv:\n```csv\n1,2\n```\nThat is the sample.", {"a.csv": "1,2"}, ""),
    # The report's case: cut at the first ``` and written as if complete.
    ("FIXTURE a.md:\n```\n# Title\n```\nx = 1\n```\ndone\n```", {}, "more than one fence line could close"),
    ("FIXTURE a.md:\n```\n# Title\n```python\nx = 1\n```\ndone\n```", {}, "more than one fence line could close"),
    ("FIXTURE a.csv:\n```\n1,2\n```\nUse it like this:\n```\ncat a.csv\n```", {}, "more than one fence line"),
    # The way to supply such a sample: a longer fence.
    ("FIXTURE a.md:\n````\n# Title\n```python\nx = 1\n```\ndone\n````",
     {"a.md": "# Title\n```python\nx = 1\n```\ndone"}, ""),
    ("FIXTURE a.md:\n````markdown\n```\n````", {"a.md": "```"}, ""),
    ("FIXTURE a.csv:\n```\n1,2", {}, "no complete fenced block"),
])
def test_a_fixture_block_ends_at_the_last_fence_or_keeps_nothing(reply, found, problem):
    got, problems = S._parse_fixture_blocks(reply)
    assert got == found
    assert (problems and problem in problems[0] and problems[0].startswith("a.")) if problem else not problems


def test_an_ambiguous_block_spoils_only_its_own_path():
    got, problems = S._parse_fixture_blocks("FIXTURE a.md:\n```\none\n```\ntwo\n```\nFIXTURE b.csv:\n```\n1\n```")
    assert got == {"b.csv": "1"} and len(problems) == 1 and problems[0].startswith("a.md:")


def test_the_supplying_call_is_told_how_to_fence_a_sample_that_holds_a_fence():
    assert "fence the block with four" in S._ONE_FIXTURE_FENCE
    assert "a block that more than one fence line could close is not written" in S._ONE_FIXTURE_FENCE
    assert "_ONE_FIXTURE_FENCE" in inspect.getsource(Session._supply_fixtures)


# -- the close-out (second half, items 16 and 25) -------------------------------
# Removed 2026-11-12 (merge ruling 2, no model writes a close-out): the eight
# tests of _parse_closeout went with that parser. The excerpt below is still
# used, by the review seats' measured diff block.

@pytest.mark.parametrize("limit", [0, 1, 5, 11, 50, 100, 109, 110, 150, 2000])
def test_the_excerpt_never_exceeds_its_bound(limit):
    # The report's case: limit 100 returned 10,100 bytes.
    text = "é" * 5050
    got = S._closeout_excerpt(text, limit)
    assert len(got.encode("utf-8")) <= limit
    if limit >= 11:
        assert "TRUNCATED" in got
    assert S._closeout_excerpt("short", 2000) == "short"


def test_run_stalled_is_still_what_an_uncorrected_needs_line_ends_as(store):
    session = Session("fix", store, lambda *a: "KIND: docs rote\nNEEDS: execute\nUpdate the results.")
    with pytest.raises(RunStalled, match="invalid task requirements"):
        session.next_task()
