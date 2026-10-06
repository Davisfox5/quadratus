"""Review seats are capped like the lead, and a capped review is no verdict.

Series rule-2ffa7f6 (2026-10-06): every editing call was bound by the derived
20-round cap, while f3's and f5's Opus collaborators ran 15 to 20 rounds and
0.87M to 1.18M tokens uncapped and the cells stopped on the token threshold.
The same cap now binds collaborator, recheck, design-review and verifier
calls. A reviewer that hits it has given no verdict: its narration is never
read as findings, the task is recorded as not fully reviewed (an unverified
fact), a capped recheck buys no fix round, and a capped design review is a
review with no verdict rather than a design defect. Review copies also carry
the exact check commands and the no-.git note (f1: `node --test tests/ui/`
and `git status` in the copy).
"""


import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.config import Settings
from quadratus.delegation import invocation, invocation_context
from quadratus.providers import TurnLimitReached
from quadratus.runtime import LEAD_CAPPED_ROLES, REVIEW_CAPPED_ROLES, Fleet
from quadratus.session import Complexity, Session, SessionConfig, TaskSpec

from .test_session import Recorder


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/unused/cli")
    for vendor in ("OPENAI", "CLAUDE", "GROK"):
        monkeypatch.delenv(f"QUADRATUS_CLI_ARGS_{vendor}", raising=False)


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


class CappedSeat(Recorder):
    """One role's calls stop at the cap; everything else answers as scripted."""

    def __init__(self, capped_role, *, times=99, review="BLOCKING: the escaping drops surrogate pairs",
                 recheck="RESOLVED"):
        super().__init__()
        self.capped_role, self.times, self.review, self.recheck = capped_role, times, review, recheck
        self.capped = 0

    def __call__(self, model, prompt, system=None):
        role = (invocation_context.get() or {}).get("role")
        if role == self.capped_role and self.capped < self.times:
            self.capped += 1
            self.calls.append({"model": model, "prompt": prompt, "role": role})
            raise TurnLimitReached("stopped at its turn limit", partial_text="BLOCKING: I was about to", turns=20)
        if "contributing an independent read" in prompt:
            self.calls.append({"model": model, "prompt": prompt, "role": role})
            return self.review
        if "Check only your BLOCKING findings" in prompt:
            self.calls.append({"model": model, "prompt": prompt, "role": role})
            return self.recheck
        return super().__call__(model, prompt, system)


def _run(store, rec, **config):
    session = Session("Build a JSON parser", store, rec, config=SessionConfig(**config))
    summary = session.run_task(TaskSpec("t1", "work", complexity=Complexity.STANDARD))
    return session, summary


# -- the fleet applies the cap --------------------------------------------------


def test_the_capped_sets_are_disjoint_and_name_every_review_seat():
    assert REVIEW_CAPPED_ROLES == {"collaborator", "recheck", "design-review", "verifier", "auditor"}
    assert not (REVIEW_CAPPED_ROLES & LEAD_CAPPED_ROLES)


@pytest.mark.parametrize("role", sorted(REVIEW_CAPPED_ROLES))
def test_a_review_seat_in_a_source_copy_is_capped(monkeypatch, tmp_path, role):
    views = []
    monkeypatch.setattr(Fleet, "_generate",
                        lambda self, key, provider, prompt, system, **kw: views.append(provider) or "ok")
    (tmp_path / "a.py").write_text("x = 1\n")
    fleet = Fleet(Settings(backend="cli", lead_max_turns=20), project=tmp_path)
    with invocation("t1", role):
        fleet.invoke("claude:opus", "p")
    assert views[0].max_turns == 20


@pytest.mark.parametrize("role", ["orchestrator", "closeout", "worker"])
def test_the_other_seats_stay_uncapped(monkeypatch, tmp_path, role):
    views = []
    monkeypatch.setattr(Fleet, "_generate",
                        lambda self, key, provider, prompt, system, **kw: views.append(provider) or "ok")
    monkeypatch.setattr(Fleet, "_closeout", lambda self, key, provider, prompt: views.append(provider) or "ok")
    (tmp_path / "a.py").write_text("x = 1\n")
    fleet = Fleet(Settings(backend="cli", lead_max_turns=20), project=tmp_path)
    with invocation("t1", role):
        fleet.invoke("claude:opus", "p")
    assert getattr(views[0], "max_turns", None) is None


# -- review copies get the exact check commands --------------------------------


def test_a_review_copy_is_told_the_exact_check_commands_and_that_it_has_no_git(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(Fleet, "_generate",
                        lambda self, key, provider, prompt, system, **kw: seen.append(system) or "ok")
    (tmp_path / "a.py").write_text("x = 1\n")
    fleet = Fleet(Settings(backend="cli"), project=tmp_path)
    fleet.check_commands = ("node --test tests/ui/a.test.js tests/ui/b.test.js",)
    for role in sorted(REVIEW_CAPPED_ROLES):
        with invocation("t1", role):
            fleet.invoke("claude:opus", "p")
    for system in seen:
        assert "APPROVED CHECK COMMANDS" in system
        assert "node --test tests/ui/a.test.js tests/ui/b.test.js" in system
        assert "substitute a directory for the listed files" in system
        assert "not a git repository" in system
        assert "do not\nretry variants" not in system


def test_a_review_copy_without_checks_or_a_worker_errand_gets_no_check_block(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(Fleet, "_generate",
                        lambda self, key, provider, prompt, system, **kw: seen.append(system) or "ok")
    (tmp_path / "a.py").write_text("x = 1\n")
    fleet = Fleet(Settings(backend="cli"), project=tmp_path)
    with invocation("t1", "collaborator"):
        fleet.invoke("claude:opus", "p")
    fleet.check_commands = ("python -m pytest -q",)
    with invocation("t1", "worker"):
        fleet.invoke("claude:opus", "p")
    assert all("APPROVED CHECK COMMANDS" not in system for system in seen)


# -- a capped review is no verdict ----------------------------------------------


def test_a_capped_collaborator_is_recorded_as_unverified_and_its_narration_is_not_a_finding(store):
    rec = CappedSeat("collaborator")
    session, summary = _run(store, rec)
    assert rec.capped >= 1
    assert not any("Check only your BLOCKING" in c["prompt"] for c in rec.calls), \
        "narration at the cap is never read as a BLOCKING finding"
    unverified = [f for f in session.open_findings if "did not finish within its turn cap" in f]
    assert unverified and "no verdict" in unverified[0]
    assert any("surrogate" not in f for f in session.open_findings)
    assert summary is not None


def test_a_capped_review_alone_buys_no_revision_round(store):
    rec = CappedSeat("collaborator")
    _run(store, rec)
    assert not any("Revise your work" in c["prompt"] for c in rec.calls)


def test_a_capped_recheck_buys_no_fix_round_and_keeps_the_named_blocker(store):
    rec = CappedSeat("recheck")
    session, _ = _run(store, rec)
    assert rec.capped == 1
    fixes = [c for c in rec.calls if "blocking findings remain unresolved" in c["prompt"].lower()]
    assert fixes == []
    assert any("recheck by" in f and "did not finish within its turn cap" in f for f in session.open_findings)
    # Codex review of 0983dac: the reviewer's own finding is the unresolved
    # defect; the cap fact alone would retire it from the record.
    assert any("not re-checked" in f and "BLOCKING: the escaping drops surrogate pairs" in f
               for f in session.open_findings)
    closeout = next(c["prompt"] for c in rec.calls if "The task is finished" in c["prompt"])
    assert "not re-checked against the revision" in closeout and "surrogate pairs" in closeout


class WordyReviewer(Recorder):
    """An ordinary, completed review whose text happens to begin UNFINISHED:."""

    def __init__(self, review):
        super().__init__()
        self.review = review

    def __call__(self, model, prompt, system=None):
        if "contributing an independent read" in prompt:
            self.calls.append({"model": model, "prompt": prompt})
            return self.review
        if "Check only your BLOCKING findings" in prompt:
            self.calls.append({"model": model, "prompt": prompt})
            return "RESOLVED"
        return super().__call__(model, prompt, system)


def test_an_ordinary_reply_beginning_unfinished_is_read_like_any_other(store):
    # Codex review of 0983dac: the cap is a transport fact, never a prefix.
    rec = WordyReviewer("UNFINISHED: only partly reviewed\nBLOCKING: the escaping drops surrogate pairs")
    session, _ = _run(store, rec)
    assert any("Revise your work" in c["prompt"] for c in rec.calls), "the named blocker is a critique"
    assert any("Check only your BLOCKING" in c["prompt"] for c in rec.calls)
    assert not any("turn cap" in f for f in session.open_findings)


def test_a_reply_with_no_verdict_is_an_unfinished_review_not_a_cap(store):
    # Codex review of cd8c5b0: a reply that arrived is not a review that
    # finished. Recorded as unverified; the text still reaches the lead.
    rec = WordyReviewer("UNFINISHED: did not reach a verdict")
    session, _ = _run(store, rec)
    assert any("Revise your work" in c["prompt"] for c in rec.calls)
    assert any("ended without a verdict" in f and "unfinished review" in f for f in session.open_findings)
    assert not any("turn cap" in f for f in session.open_findings)


@pytest.mark.parametrize("review", ["NO FINDINGS", "No findings.",
                                    "Minor: rename x.\nREVIEW: COMPLETE",
                                    "Minor: rename x.\n- **Review: complete**",
                                    "BLOCKING: the escaping drops surrogate pairs",
                                    "notes\n- **BLOCKING:** malformed numbers accepted"])
def test_the_three_verdict_forms_count_as_a_finished_review(store, review):
    rec = WordyReviewer(review)
    session, _ = _run(store, rec)
    assert not any("unfinished review" in f for f in session.open_findings)


def test_reviewers_are_told_the_completion_line(store):
    rec = WordyReviewer("NO FINDINGS")
    _run(store, rec)
    prompt = next(c["prompt"] for c in rec.calls if "contributing an independent read" in c["prompt"])
    assert "REVIEW: COMPLETE" in prompt and "unfinished review" in prompt


def test_a_capped_recheck_keeps_multiline_and_mixed_format_findings(store):
    rec = CappedSeat("recheck", review="BLOCKING:\nThe escaping drops surrogate pairs.\n"
                                       "Reproduce with a non-BMP character.\n"
                                       "- **BLOCKING:** the parser accepts malformed numbers")
    session, _ = _run(store, rec)
    kept = [f for f in session.open_findings if "not re-checked" in f]
    assert kept and "non-BMP character" in kept[0] and "malformed numbers" in kept[0]


def test_a_finished_recheck_is_unchanged(store):
    rec = CappedSeat("nobody", recheck="UNRESOLVED: still drops them")
    session, _ = _run(store, rec)
    fixes = [c for c in rec.calls if "blocking findings remain unresolved" in c["prompt"].lower()]
    assert len(fixes) == 1
    assert not any("turn cap" in f for f in session.open_findings)


def test_reviewers_are_told_their_round_budget_only_where_a_cap_applies(store):
    rec = CappedSeat("nobody")
    _run(store, rec, lead_max_turns=20)
    reviews = [c["prompt"] for c in rec.calls if "contributing an independent read" in c["prompt"]]
    rechecks = [c["prompt"] for c in rec.calls if "Check only your BLOCKING" in c["prompt"]]
    assert reviews and rechecks
    for prompt in reviews + rechecks:
        assert "at most 20 tool rounds" in prompt and "recorded as unfinished" in prompt
    rec = CappedSeat("nobody")
    _run(store, rec)
    assert not any("tool rounds" in c["prompt"] for c in rec.calls)


def test_the_round_budget_is_silent_for_a_cli_without_a_turn_flag(store):
    session = Session("goal", store, lambda *a, **k: "", config=SessionConfig(lead_max_turns=20))
    assert session._review_turn_budget_note("openai:gpt-5.6-sol") == ""
    assert "at most 20 tool rounds" in session._review_turn_budget_note("claude:opus")


def test_a_capped_design_review_is_a_review_with_no_verdict(tmp_path, monkeypatch):
    from quadratus.memory import TaskMemory
    from tests.test_preferences_in_product import _design_session, _fake_evidence, _ui

    def invoke(model, prompt, **k):
        role = (invocation_context.get() or {}).get("role")
        if role == "design-review":
            raise TurnLimitReached("capped", partial_text="BLOCKING: the header", turns=20)
        return "tried"

    session = _design_session(tmp_path, invoke)
    _fake_evidence(tmp_path)
    monkeypatch.setattr(Session, "_harness_captures", lambda self, spec: False)
    session._check_design(_ui(), "grok:default", ["claude:opus"], TaskMemory("t6", "grok:default", session.store))
    record = session.design_checks[-1]
    assert record["final_review"]["verdict"].startswith("UNFINISHED: the design review stopped at 20 rounds")
    joined = " ".join(session.open_findings)
    assert "did not finish within its turn cap" in joined
    assert "gave no verdict" in joined
    assert "the header" not in joined, "the capped reviewer's narration is not a finding"


# -- the lead is told what the harness measures ---------------------------------


def test_the_scope_block_states_the_measured_rule_and_the_stop_line():
    from quadratus.scope import TaskScope
    text = TaskScope(permitted_paths=["app.py", "tests/test_app.py"], max_lines=100).render()
    assert "tests in full" in text
    assert "stops the task past 150 changed lines" in text
    assert "compact named cases" in text
    assert "Expected size: about 100 changed lines" in text
    assert "Expected size" not in TaskScope(permitted_paths=["app.py"]).render()
