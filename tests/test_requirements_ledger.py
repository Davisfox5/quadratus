"""The goal's requirements, numbered, reviewed, covered and audited before DONE."""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig, TaskSpec, _read_covers, _read_requirements

pytestmark = pytest.mark.requirements_ledger

PLAN = ("REQUIREMENTS:\nR1: POST endpoint previews rows\nR2: UI button #btn-import-preview\n"
        "R3: must not write the project store\nKIND: backend simple\nBuild the parser.\nCOVERS: R1")


def test_the_block_is_read_and_stripped():
    found, rest = _read_requirements(PLAN)
    assert list(found) == ["R1", "R2", "R3"] and "REQUIREMENTS" not in rest and rest.startswith("KIND")
    ids, spec = _read_covers(TaskSpec("t1", "Build the parser.\nCOVERS: R1, R3"))
    assert ids == ["R1", "R3"] and spec.description == "Build the parser."


class Script:
    """A fake invoke: orchestrator replies in order, the reviewer and auditor as given."""

    def __init__(self, orchestrator, review="COMPLETE", audits=()):
        self.orchestrator, self.review, self.audits, self.prompts = list(orchestrator), review, list(audits), []

    def __call__(self, model, prompt, system=None, allow_writes=False):
        self.prompts.append((model, prompt))
        if "Compare the list with the goal" in prompt:
            return self.review
        if "independent auditor" in prompt:
            return self.audits.pop(0)
        if "Name the single next task" in prompt:
            return self.orchestrator.pop(0) if self.orchestrator else "DONE"
        if "The task is finished" in prompt:
            return "SUMMARY: done\nREASONING: done"
        return "work"


def _run(tmp_path, script, **config):
    session = Session("Build the preview", ArtifactStore(tmp_path / "a"), script, config=SessionConfig(**config))
    session.run(max_tasks=6)
    return session


def test_done_is_sent_back_while_a_requirement_is_uncovered(tmp_path):
    script = Script([PLAN, "DONE", "KIND: frontend simple\nAdd the button.\nCOVERS: R2, R3", "DONE"],
                    audits=["R1: MET - app.py::import_preview\nR2: MET - templates/index.html\n"
                            "R3: MET - tests/test_preview.py::test_writes_nothing"])
    session = _run(tmp_path, script)
    orchestrator_prompts = [p for m, p in script.prompts if "Name the single next task" in p]
    assert any("not covered by any finished task: R2, R3" in p for p in orchestrator_prompts)
    assert session.completed
    assert session.memory.ledger.requirement_status["R2"] == "met (audited)"


def test_an_audit_without_evidence_or_with_a_miss_reopens_then_stops(tmp_path):
    plan = PLAN.replace("COVERS: R1", "COVERS: R1, R2, R3")
    script = Script([plan, "DONE", "DONE", "DONE", "DONE"],
                    audits=["R1: MET - looks fine\nR2: NOT MET - no button\nR3: MET - app.py"] * 4)
    session = _run(tmp_path, script, max_requirement_reopens=2)
    assert not session.completed
    status = session.memory.ledger.requirement_status
    assert status["R1"].startswith("NOT MET") and "without a file or test" in status["R1"]
    assert status["R2"].startswith("NOT MET")
    assert len(session.requirement_audits) == 1, "no second audit until a task covers the misses again"
    assert any("found not met by the audit" in p for m, p in script.prompts if "Name the single next task" in p)


def test_the_review_adds_what_the_list_missed(tmp_path):
    script = Script([PLAN, "DONE"], review="ADD: docs/CSV_IMPORT.md documents the format\nAMBIGUOUS: R2 - which screen")
    session = _run(tmp_path, script, max_requirement_reopens=0)
    ledger = session.memory.ledger
    assert ledger.requirements["R4"] == "docs/CSV_IMPORT.md documents the format"
    assert ledger.requirement_status["R4"].startswith("open")
    assert ledger.ambiguous["R2"] == "which screen"
    reviewer = next(m for m, p in script.prompts if "Compare the list with the goal" in p)
    assert reviewer.partition(":")[0] != session.seat().key.partition(":")[0]


def test_without_a_list_no_task_runs_and_the_run_cannot_complete(tmp_path):
    """Codex review: a model that omits REQUIREMENTS must not finish the run."""
    from quadratus.session import RunStalled
    script = Script(["KIND: backend simple\nBuild it."] * 4)
    with pytest.raises(RunStalled, match="No requirements are listed"):
        _run(tmp_path, script, max_requirement_reopens=2)
    assert not any("You are leading" in p for m, p in script.prompts), "no lead call was spent"
    script = Script(["DONE"] * 4)
    assert not _run(tmp_path, script, max_requirement_reopens=2).completed


def test_a_task_with_unknown_or_missing_covers_is_sent_back_before_the_lead(tmp_path):
    plan = PLAN.replace("COVERS: R1", "COVERS: R9")
    script = Script([plan, "KIND: backend simple\nBuild the parser.\nCOVERS: R1, R2, R3", "DONE"],
                    audits=["R1: MET - app.py\nR2: MET - app.py\nR3: MET - app.py"])
    session = _run(tmp_path, script)
    leads = [p for m, p in script.prompts if "You are leading" in p]
    assert len(leads) == 1 and session.completed
    assert any("do not exist: R9" in p for m, p in script.prompts if "Name the single next task" in p)


def test_no_cross_vendor_auditor_keeps_done_blocked(tmp_path):
    plan = PLAN.replace("COVERS: R1", "COVERS: R1, R2, R3")
    script = Script([plan, "DONE", "DONE"], audits=[])
    session = Session("Build the preview", ArtifactStore(tmp_path / "a"), script,
                      config=SessionConfig(max_requirement_reopens=1))
    seat_vendor = session.seat().key.partition(":")[0]
    session._available = lambda key: key.partition(":")[0] == seat_vendor or key == session.seat().key
    session.run(max_tasks=4)
    assert not session.completed
    assert not any("independent auditor" in p for m, p in script.prompts)


def test_cited_evidence_must_exist_in_the_project(tmp_path):
    session = Session("g", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig())
    project = tmp_path / "p"
    (project / "tests").mkdir(parents=True)
    (project / "app.py").write_text("x = 1\n")
    (project / "tests" / "test_app.py").write_text("def test_preview_writes_nothing():\n    pass\n")
    session.project = project
    assert session._resolve_citations("app.py and imaginary.py") == ["app.py"]
    assert session._resolve_citations("test_preview_writes_nothing") == ["tests/test_app.py::test_preview_writes_nothing"]
    assert session._resolve_citations("test_nothing_like_it") == []


def test_a_review_that_is_not_complete_or_well_formed_is_not_a_review(tmp_path):
    plan = PLAN.replace("COVERS: R1", "COVERS: R1, R2, R3")
    script = Script([plan, "DONE", "DONE"], review="Looks mostly fine to me, maybe add docs.",
                    audits=["R1: MET - app.py\nR2: MET - app.py\nR3: MET - app.py"] * 3)
    session = _run(tmp_path, script, max_requirement_reopens=1)
    assert not session.completed
    assert all(r["result"].startswith("failed") for r in session.requirement_reviews)
    assert not any("independent auditor" in p for m, p in script.prompts)


def test_every_added_requirement_is_kept(tmp_path):
    many = "\n".join(f"ADD: requirement number {i}" for i in range(20))
    script = Script([PLAN, "DONE"], review=many)
    session = _run(tmp_path, script, max_requirement_reopens=0)
    assert len(session.memory.ledger.requirements) == 23


def test_an_ambiguous_requirement_needs_a_ruling_even_when_covered(tmp_path):
    plan = PLAN.replace("COVERS: R1", "COVERS: R1, R2, R3")
    script = Script([plan, "DONE", "DONE"], review="AMBIGUOUS: R2 - which screen gets the button",
                    audits=["R1: MET - app.py\nR2: MET - app.py\nR3: MET - app.py"] * 3)
    session = _run(tmp_path, script, max_requirement_reopens=1)
    assert not session.completed
    assert session.memory.ledger.requirement_status["R2"].startswith("covered")
    assert "R2" in session.memory.ledger.ambiguous
    assert any("need the operator's ruling: R2" in p for m, p in script.prompts if "Name the single next task" in p)
    session.memory.ledger.rulings.append("Q: R2, which screen? -- A: the tagging screen")
    session._done_refusal = ""
    session._requirements_satisfied()
    assert "operator's ruling" not in session._done_refusal, "a ruling naming R2 settles it"
