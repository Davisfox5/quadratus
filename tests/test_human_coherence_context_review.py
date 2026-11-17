"""Visible task continuity and blind first reads, using scripted model replies."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.config import Settings
from quadratus.delegation import invocation, invocation_context
from quadratus.memory import TaskMemory
from quadratus.providers import ProviderError
from quadratus.runtime import Fleet, ReviewCopyAltered
from quadratus.scope import TaskScope
from quadratus.session import Complexity, RunStalled, Session, SessionConfig, TaskSpec
from quadratus.task_kinds import TaskKind
from quadratus.workers import WorkerPool

SOL, OPUS = "openai:sol", "claude:opus"


class SourceView:
    restricted = False

    def in_directory(self, directory, *, allow_writes, disposable=False):
        return SimpleNamespace(workdir=Path(directory), allow_writes=allow_writes,
                               restricted=False, disposable=disposable)


def source_fleet(project, monkeypatch, generate):
    fleet = Fleet(Settings(backend="cli"), project=project, allow_writes=True)
    monkeypatch.setattr(fleet, "provider_for", lambda key: SourceView())
    monkeypatch.setattr(fleet, "_generate", generate)
    return fleet


@pytest.mark.parametrize("last_channel", ["write-worker", "packet"])
def test_fetched_reference_and_visible_decisions_survive_consult_and_reply_handoffs(tmp_path, last_channel):
    project = tmp_path / "project"
    project.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    reference = store.put("COMPATIBILITY_REFERENCE: preserve legacy quoted identifiers", kind="reference",
                          author="harness")
    decision = "Visible rationale " + "details " * 250 + "DECISION: preserve the old identifier parser."
    consult = "CONSULT claude:opus: Does the old parser cover quoted identifiers?"
    request = ("THIRD_DECISION: retain the helper's existing behavior.\nWORKER "
               + json.dumps(dict(errand="code", instruction="Propose a patch for the parser helper",
                                 needs=["patch"], write=True))
               if last_channel == "write-worker" else
               "PACKET EXTENSION: 20000\nNECESSARY: retain parser compatibility context\n"
               "WHY NOT SHORTER: the legacy rule needs its exact examples")
    lead_prompts, worker_grants = [], []

    def invoke(model, prompt, **kwargs):
        if "A colleague leading this task asks you" in prompt:
            return "CONSULT_ANSWER: keep the legacy quoting behavior."
        lead_prompts.append(prompt)
        if len(lead_prompts) == 1:
            return decision + f"\nFETCH: {reference.id}"
        if len(lead_prompts) == 2:
            return "SECOND_DECISION: reuse the parser, then confirm compatibility.\n" + consult
        assert "COMPATIBILITY_REFERENCE" in prompt
        assert decision in prompt
        assert consult in prompt and "CONSULT_ANSWER" in prompt
        assert "SECOND_DECISION" in prompt
        if len(lead_prompts) == 3:
            return request
        assert request in prompt
        assert "Historical context" in prompt and "Do not repeat a fulfilled request" in prompt
        return "The implementation preserves the compatibility rule."

    session = Session("Preserve the legacy contract", store, invoke, config=SessionConfig(
        project=project, allow_writes=True, in_session_workers=False, requirements_ledger=False))

    def worker(model, prompt, *, allow_writes=False):
        worker_grants.append(allow_writes)
        return "NO CHANGES: the existing helper already supports this."

    session.workers = WorkerPool(store=store, run=worker)
    spec = TaskSpec("t1", "Keep legacy parsing compatible")
    task = TaskMemory(spec.task_id, SOL, store)
    session._active_spec = spec
    with invocation(spec.task_id, "lead"):
        result = session._draft_with_channels(SOL, spec, task)
    assert result.startswith("The implementation") and len(lead_prompts) == 4
    assert worker_grants == ([True] if last_channel == "write-worker" else [])
    assert any(store.get(ref) == decision + f"\nFETCH: {reference.id}"
               for ref in task.refs if ref.kind == "lead-handoff")
    reviewer_prompt = session._collaborator_prompt(spec, result, OPUS)
    assert decision not in reviewer_prompt and "COMPATIBILITY_REFERENCE" not in reviewer_prompt


def test_retained_context_has_explicit_bounds_and_exact_archive_references(tmp_path, monkeypatch):
    monkeypatch.setattr("quadratus.session._FETCH_CHAR_CAP", 120)
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.put("FIRST_REFERENCE_BODY", kind="reference", author="harness")
    second = store.put("SECOND_REFERENCE_BODY", kind="reference", author="harness")
    rationale = "EARLIER_DECISION " + "exact rationale " * 30
    prompts = []
    replies = [rationale + f"\nFETCH: {first.id}",
               'WORKER {"errand":"read","instruction":"Read the helper contract"}',
               f"FETCH: {second.id}",
               'WORKER {"errand":"read","instruction":"Read the other helper contract"}', "done"]

    def invoke(model, prompt, **kwargs):
        prompts.append(prompt)
        return replies.pop(0) if replies else "done"

    session = Session("goal", store, invoke, config=SessionConfig(
        max_fetches=1, in_session_workers=False, requirements_ledger=False))
    session.workers = WorkerPool(store=store, run=lambda *args, **kwargs: "The helper is compatible.")
    task = TaskMemory("t1", SOL, store)
    session._draft_with_channels(SOL, TaskSpec("t1", "task"), task)

    final = prompts[-1]
    assert "FIRST_REFERENCE_BODY" not in final and "SECOND_REFERENCE_BODY" in final
    assert f"FETCH: {first.id} FROM 0" in final
    assert "Earlier fetched artifact windows retained by reference" in final
    assert rationale not in final and "Earlier replies not inlined" in final
    archived = next(ref for ref in task.refs if ref.kind == "lead-handoff"
                    and store.get(ref).startswith(rationale))
    assert f"FETCH: {archived.id}" in final
    assert session.memory.fetch(archived.id) == rationale + f"\nFETCH: {first.id}"

    # The next task receives neither the old context nor its private index.
    session._draft_with_channels(SOL, TaskSpec("t2", "another task"), TaskMemory("t2", SOL, store))
    assert archived.id not in prompts[-1] and first.id not in prompts[-1]


def test_review_counterpart_first_read_is_blind_and_reconciliation_keeps_both_reads(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    calls = []
    first_read = "LEAD_FINDING_UNIQUE: parser loses quoted identifiers."
    counterpart = "COUNTERPART_FINDING_UNIQUE: invalid escapes are accepted.\nREVIEW: COMPLETE"

    def invoke(model, prompt, **kwargs):
        calls.append((model, prompt))
        if "contributing an independent read" in prompt:
            assert first_read not in prompt
            assert "blind review" in prompt and "Inspect the source yourself" in prompt
            assert "do not judge claims or omissions in the withheld lead report" in prompt
            assert "a missed problem, a wrong claim" not in prompt
            return counterpart
        if "Independent reviews of it:" in prompt:
            assert first_read in prompt and counterpart in prompt
            return "Reconciled audit: quoted identifiers and invalid escapes both need correction."
        return first_read

    session = Session("Audit the parser", store, invoke, config=SessionConfig(requirements_ledger=False))
    session.run_task(TaskSpec("t1", "Review the parser source", Complexity.SIMPLE,
                              kind=TaskKind.REVIEW, lead=SOL))
    assert any(model == OPUS and "contributing an independent read" in prompt for model, prompt in calls)
    assert any("Independent reviews of it:" in prompt for _, prompt in calls)


def test_ordinary_implementation_review_still_receives_the_delivered_draft(tmp_path):
    session = Session("Implement the parser", ArtifactStore(tmp_path), lambda *args, **kwargs: "unused")
    prompt = session._collaborator_prompt(TaskSpec("t1", "Implement quoting", kind=TaskKind.GLUE),
                                          "DELIVERED_IMPLEMENTATION: parser code and explanation", OPUS)
    assert "DELIVERED_IMPLEMENTATION" in prompt and "Current work:" in prompt
    assert "blind review" not in prompt


def test_later_merged_review_judges_the_supplied_report_on_current_source(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    session = Session("Audit", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "unused",
                      config=SessionConfig(project=project, requirements_ledger=False))
    prompt = session._collaborator_prompt(TaskSpec("merged", "Review merged audit", kind=TaskKind.REVIEW),
                                          "MERGED_REVIEW_REPORT", OPUS)
    assert "Current work:\nMERGED_REVIEW_REPORT" in prompt
    assert "Judge the supplied report against the source" in prompt
    assert "blind first read" not in prompt and "withheld lead report" not in prompt


def test_in_session_worker_answer_survives_a_later_consult_handoff(tmp_path, monkeypatch):
    class ScriptedBridge:
        handler = None

        def __init__(self, handler):
            ScriptedBridge.handler = handler

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def spec(self):
            return None

    monkeypatch.setattr("quadratus.worker_bridge.WorkerBridge", ScriptedBridge)
    prompts = []

    def invoke(model, prompt, **kwargs):
        if "A colleague leading this task asks you" in prompt:
            return "The helper contract remains valid."
        prompts.append(prompt)
        if len(prompts) == 1:
            answer, _ = ScriptedBridge.handler(dict(errand="read", instruction="Read the helper contract"))
            assert "NATIVE_WORKER_FACT" in answer
            return "CONSULT claude:opus: Does that contract support the change?"
        assert "NATIVE_WORKER_FACT" in prompt
        assert "Read the helper contract" in prompt
        assert "Completed in-session commission_worker (historical)" in prompt
        return "done"

    store = ArtifactStore(tmp_path)
    session = Session("goal", store, invoke, config=SessionConfig(requirements_ledger=False))
    session.workers = WorkerPool(store=store, run=lambda *args, **kwargs: "NATIVE_WORKER_FACT: keep quoting")
    session._draft_with_channels(SOL, TaskSpec("t1", "task"), TaskMemory("t1", SOL, store))
    assert len(prompts) == 2


@pytest.mark.parametrize("unfinished", [False, True])
def test_written_audit_does_not_contaminate_counterpart_source_or_its_completion(tmp_path, monkeypatch,
                                                                              unfinished):
    project = tmp_path / "project"
    project.mkdir()
    (project / "parser.py").write_text("ORIGINAL_PARSER = True\n")
    lead_finding = "LEAD_FINDING_PRIVATE: quoted identifiers fail."
    counterpart = "COUNTERPART_FINDING: invalid escapes fail.\nREVIEW: COMPLETE"
    reviews, revisions = [], []

    def generate(model, view, prompt, role):
        context = invocation_context.get() or {}
        if context["role"] == "lead":
            assert view.workdir == project and view.allow_writes
            (project / "audit.md").write_text(lead_finding)
            (project / "parser.py").write_text("PARSER_CHANGED_DURING_LEAD_READ = True\n")
            return lead_finding + "\nCHANGED: audit.md, parser.py"
        if context["role"] == "collaborator":
            reviews.append(view.workdir)
            assert view.workdir != project and not view.allow_writes and view.disposable
            assert (view.workdir / "parser.py").read_text() == "ORIGINAL_PARSER = True\n"
            assert not (view.workdir / "audit.md").exists()
            assert lead_finding not in prompt + role
            assert "PARSER_CHANGED_DURING_LEAD_READ" not in prompt
            assert "POSTLEAD_" not in prompt + role
            assert "exact source bytes captured before either audit began" in role
            assert "Review the parser source" in prompt and "Audit the parser" in prompt
            assert not context.get("probe") and not context.get("harness_checks")
            assert not context.get("evidence_files")
            if unfinished and len(reviews) == 1:
                return "I inspected the original parser but did not finish the verdict."
            return counterpart
        if context["role"] == "revision":
            revisions.append(prompt)
            assert view.workdir == project and view.allow_writes
            assert (project / "audit.md").read_text() == lead_finding
            assert lead_finding in prompt and counterpart in prompt
            return "Reconciled both independent reads.\nCHANGED: none"
        return "The audit is complete."

    fleet = source_fleet(project, monkeypatch, generate)
    session = Session("Audit the parser", ArtifactStore(tmp_path / "artifacts"), fleet._invoke,
                      config=SessionConfig(project=project, allow_writes=True, requirements_ledger=False,
                                           in_session_workers=False))
    # All of these describe the lead's current work, not the starting source.
    for helper in ("_changed_originals_note", "_proof_rows", "_restated_review_block", "_sibling_note",
                   "_inspection_note"):
        monkeypatch.setattr(session, helper, lambda *a, **k: "POSTLEAD_FINDINGS_OR_PROOF")
    monkeypatch.setattr(session, "_harness_facts", lambda **kwargs: "POSTLEAD_CHECK_RECEIPT")
    session._review_evidence = ["POSTLEAD_DESIGN_ARTIFACT"]
    session.run_task(TaskSpec("t1", "Review the parser source", Complexity.SIMPLE,
                              kind=TaskKind.REVIEW, lead=SOL,
                              scope=TaskScope(permitted_paths=("audit.md", "parser.py"))))
    assert len(reviews) == (2 if unfinished else 1) and revisions
    assert all(not directory.exists() for directory in reviews)
    assert (project / "parser.py").read_text() == "PARSER_CHANGED_DURING_LEAD_READ = True\n"


def test_captured_review_source_integrity_and_cleanup_still_hold(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "parser.py").write_text("current source\n")
    handed = []

    def alter(model, view, prompt, role):
        handed.append(view.workdir)
        (view.workdir / "parser.py").write_text("reviewer edit\n")
        return "NO FINDINGS"

    fleet = source_fleet(project, monkeypatch, alter)
    with invocation("t1", "collaborator", review_source={"parser.py": b"starting source\n"}):
        with pytest.raises(ReviewCopyAltered):
            fleet._invoke(OPUS, "Review the starting source")
    assert handed and not handed[0].exists()
    assert (project / "parser.py").read_text() == "current source\n"


def test_captured_review_source_cannot_grant_writes_or_escape_its_copy(tmp_path, monkeypatch):
    called = []
    fleet = source_fleet(tmp_path, monkeypatch, lambda *args: called.append(args) or "NO FINDINGS")
    with invocation("t1", "collaborator", review_source={"../escape.py": b"outside"}):
        with pytest.raises(ProviderError, match="invalid file"):
            fleet._invoke(OPUS, "Review")
    with invocation("t1", "collaborator", review_source={}):
        with pytest.raises(ProviderError, match="read-only project counterpart"):
            fleet._invoke(OPUS, "Edit", allow_writes=True)
    assert called == [] and not (tmp_path.parent / "escape.py").exists()


def test_ordinary_review_still_uses_current_source_copy(tmp_path, monkeypatch):
    (tmp_path / "parser.py").write_text("current implementation\n")
    copies = []

    def generate(model, view, prompt, role):
        copies.append(view.workdir)
        assert (view.workdir / "parser.py").read_text() == "current implementation\n"
        return "NO FINDINGS"

    fleet = source_fleet(tmp_path, monkeypatch, generate)
    with invocation("implementation", "collaborator"):
        assert fleet._invoke(OPUS, "Review the implementation") == "NO FINDINGS"
    assert copies and not copies[0].exists()


def test_review_with_uncapturable_source_stops_before_first_model_call(tmp_path, monkeypatch):
    calls = []
    session = Session("Audit", ArtifactStore(tmp_path / "artifacts"),
                      lambda *args, **kwargs: calls.append(args) or "NO FINDINGS",
                      config=SessionConfig(project=tmp_path, requirements_ledger=False))
    monkeypatch.setattr(session, "_capture_source", lambda: None)
    with pytest.raises(RunStalled, match="bind independent audit reads"):
        session.run_task(TaskSpec("t1", "Review", kind=TaskKind.REVIEW, lead=SOL))
    assert not calls
