"""Cycle20 units (PR73 6748219666, reconciled by root).

A. A quoted span in an auditor's basis pairs its marks; an apostrophe inside
   a word never opens or closes one. Provenance only: no verdict changes, and
   an absent quote (one with a literal ellipsis included) stays absent.
B. A clipped finish reason names the artifact that holds its exact words
   (the audit reply, the audit's completion reply, or the reason as the
   harness recorded it), never a reply that does not contain them; the
   terminal summary cuts outside labels and keeps the requirement id.
C. The audit may name up to three distinct examples in one NOT MET line;
   history keeps them within its bounds and points truthfully when clipped.
"""
from types import SimpleNamespace

import pytest

from quadratus import completion_decision as C
from quadratus import judgment
from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig

GOAL = "goal: add(a, b) returns the sum; it isn't valid to drop a sign"


def _session(tmp_path):
    session = Session(GOAL, ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=tmp_path, allow_writes=True, requirements_ledger=True))
    session.memory.ledger.requirements.update({"R1": "add(a, b) returns the sum"})
    return session


def _basis(session, basis):
    return session._not_met_basis("R1", f"add drops signs; basis: {basis}; evidence: source reading")


# -- A ------------------------------------------------------------------------------------------

def test_apostrophes_before_and_after_a_real_quote_are_not_quote_marks(tmp_path):
    session = _session(tmp_path)
    found = _basis(session, "the requirement's words \"returns the sum\" are false; the parser's split drops it")
    assert found["quoted"] == ["returns the sum"] and "R1" in found["quoted_in"]
    session.requirement_audits = [dict(bases={"R1": found})]
    assert session._basis_label("R1") == ""


def test_a_contraction_inside_a_real_quote_and_several_spans(tmp_path):
    session = _session(tmp_path)
    found = _basis(session, "“it isn't valid to drop a sign” and 'returns the sum' both hold")
    assert found["quoted"] == ["it isn't valid to drop a sign", "returns the sum"]
    assert set(found["quoted_in"]) == {"R1", "goal"}
    single = _basis(session, "'it isn't valid to drop a sign' is the rule")
    assert single["quoted"] == ["it isn't valid to drop a sign"]


@pytest.mark.parametrize("basis", ["\"returns the sum' is mismatched", "the users' rule and the goal's"])
def test_mismatched_marks_and_bare_apostrophes_quote_nothing(tmp_path, basis):
    assert _basis(_session(tmp_path), basis)["quoted"] == []


def test_a_quote_that_is_not_in_any_text_keeps_its_honest_label(tmp_path):
    """Retained control: passes on 6856c3f too. The cycle19 R12 basis (a quote
    with a literal ellipsis) is absent, and no fuzzy match is added."""
    session = _session(tmp_path)
    found = _basis(session, "“Invalid inputs … return HTTP 400”")
    assert found["quoted"] == ["Invalid inputs … return HTTP 400"] and found["quoted_in"] == []
    session.requirement_audits = [dict(bases={"R1": found})]
    assert "its quoted basis was not found" in session._basis_label("R1")


def test_a_boundary_single_quote_is_read(tmp_path):
    """Retained control: passes on 6856c3f too."""
    found = _basis(_session(tmp_path), "the rule 'returns the sum' holds")
    assert found["quoted"] == ["returns the sum"] and "R1" in found["quoted_in"]


# -- B ------------------------------------------------------------------------------------------

LONG = "add drops the sign of negative operands on every path, " * 8 + "example: add(-1, -2) returns 3"


def _audited(session, why, *, in_reply=True, completion=False):
    reply = session.store.put(f"R1: NOT MET - {why}" if in_reply and not completion else "R1: MET - app.py",
                              kind="audit-reply:openai:sol", author="openai:sol").id
    audit = dict(auditor="openai:sol", verdicts={"R1": dict(met=False, why=why)}, reply_artifact=reply,
                 completion=None)
    if completion:
        audit["completion"] = dict(reply_artifact=session.store.put(
            f"R1: NOT MET - {why}", kind="judgment-completion:audit:openai:sol", author="openai:sol").id)
    session.requirement_audits.append(audit)
    return audit


def test_a_short_reason_is_shown_unchanged(tmp_path):
    session = _session(tmp_path)
    assert session._shown_reason("R1", "add drops signs") == ("add drops signs", {})


def test_a_clipped_auditor_reason_points_at_the_reply_that_holds_it(tmp_path):
    session = _session(tmp_path)
    audit = _audited(session, LONG)
    text, fields = session._shown_reason("R1", LONG)
    ref = audit["reply_artifact"]
    assert text.startswith(LONG[:300]) and LONG[300:] not in text
    assert f"[... {len(LONG) - 300} more characters in audit reply {ref}; read it whole with FETCH: {ref}]" in text
    assert fields == dict(why_omitted=len(LONG) - 300, why_artifact=ref, why_source="audit reply")
    assert LONG in session.memory.fetch(ref), "the planner's FETCH reads the whole words"


def test_a_reason_answered_on_the_audits_completion_points_at_the_completion(tmp_path):
    session = _session(tmp_path)
    audit = _audited(session, LONG, completion=True)
    _, fields = session._shown_reason("R1", LONG)
    assert fields["why_artifact"] == audit["completion"]["reply_artifact"] != audit["reply_artifact"]
    assert fields["why_source"] == "audit completion reply"


def test_a_harness_reason_is_never_attributed_to_an_audit_reply(tmp_path):
    session = _session(tmp_path)
    lowered = "acceptance reported NOT RUN by the covering task: " + "t1: tests/test_api.py " * 30
    audit = _audited(session, lowered, in_reply=False)
    text, fields = session._shown_reason("R1", lowered)
    assert fields["why_artifact"] != audit["reply_artifact"] and fields["why_source"] == "the reason as recorded"
    assert session.store.get(fields["why_artifact"]) == lowered
    assert f"more characters in the reason as recorded {fields['why_artifact']}" in text


def test_a_legacy_audit_without_a_stored_reply_gets_the_recorded_copy(tmp_path):
    session = _session(tmp_path)
    session.requirement_audits = [dict(verdicts={"R1": dict(met=False, why=LONG)}, reply_artifact="gone00000000")]
    _, fields = session._shown_reason("R1", LONG)
    assert fields["why_artifact"] != "gone00000000" and session.store.get(fields["why_artifact"]) == LONG


def _terminal(reason, label=""):
    return SimpleNamespace(done_refusal=(
        "\n\n--- DONE SENT BACK ---\nAn independent audit found these requirements not met:\n"
        f"- R12: {reason}{label}\n  route: finding on the behaviour"))


def test_the_terminal_summary_never_splits_a_label_and_keeps_the_requirement():
    label = " (its quoted basis was not found in the goal, the requirement or a ruling; recorded only)"
    out = C._why(_terminal("x " * 140 + "[... 90 more characters in audit reply abc; read it whole with FETCH: abc]",
                           label))
    assert len(out) <= C._WHY_CHARS and "- R12: x x" in out
    assert "(its quoted basis" not in out or "recorded only)" in out
    assert "more characters; each reason, whole or with its artifact, is in result.json finish_routing]" in out


def test_a_short_terminal_summary_is_unchanged():
    """Retained control: passes on 6856c3f too."""
    short = _terminal("add drops signs")
    assert C._why(short) == short.done_refusal.replace("--- DONE SENT BACK ---", "").strip()


# -- C ------------------------------------------------------------------------------------------

EXAMPLES = "add(-1, -2) returns 3 | add(-5, 0) returns 5 | add(-1, 1) returns 2"


def test_history_keeps_every_example_and_points_at_the_reply_that_holds_a_clipped_one(tmp_path):
    session = _session(tmp_path)
    why = f"add drops signs; basis: \"returns the sum\"; example: {EXAMPLES}"
    audit = _audited(session, why, completion=True)
    audit.update(stated=["R1"], stated_reasons={"R1": why}, source="S1", revisions={"R1": 1}, bases={})
    history = session._audit_history(["R1"])
    assert f"Example: {EXAMPLES}." in history
    assert f"completion reply artifact {audit['completion']['reply_artifact']}" in history
    long_examples = " | ".join(f"add(-{n}, 0) returns {n}" + " and more" * 20 for n in range(1, 4))
    session.requirement_audits = []
    why = f"add drops signs; example: {long_examples}"
    audit = _audited(session, why)
    audit.update(stated=["R1"], stated_reasons={"R1": why}, source="S1", revisions={"R1": 1}, bases={})
    history = session._audit_history(["R1"])
    assert f"more characters in artifact {audit['reply_artifact']}" in history


def test_the_audit_form_allows_up_to_three_examples_and_parsing_is_unchanged():
    import inspect
    source = inspect.getsource(Session._audit_requirements)
    assert "_AUDIT_EXAMPLES" in source and S._AUDIT_EXAMPLES == 3
    assert "separated by ' | '" in source and "one is enough" in source
    line = f"R1: NOT MET - add drops signs; example: {EXAMPLES}"
    (rid, met, why), = [(m.group(1), m.group(2).upper() == "MET", m.group(3)) for m in S._AUDIT_LINE.finditer(line)]
    assert rid == "R1" and not met and why.endswith(EXAMPLES)


def test_a_not_met_beside_a_met_stays_not_met():
    """Retained control: passes on 6856c3f too."""
    combined = judgment.combine([("R1", False, f"add drops signs; example: {EXAMPLES}"), ("R1", True, "app.py")])
    assert combined["R1"].state != judgment.MET
