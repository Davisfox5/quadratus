"""Offline regressions found by the repository correctness audit."""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.finding_state import from_result, legacy_parity
from quadratus.outcome import TaskOutcome, completion_blockers, missing_facts
from quadratus.project import Project
from quadratus.scope import TaskScope, changed_paths, count_change_lines_by_path
from quadratus.session import RunStalled, Session
from quadratus.taskmeta import AmbiguousMetadata, parse_metadata


@pytest.mark.parametrize("labels", [
    "KIND: backend\nKIND: backend complex",
    "KIND: unknown complex\nKIND: backend complex",
])
def test_conflicting_partial_labels_raise_the_metadata_error(labels):
    with pytest.raises(AmbiguousMetadata) as caught:
        parse_metadata(labels + "\nImplement the change.", known_kinds=("backend",),
                       known_difficulties=("complex",), default_kind="general",
                       default_difficulty="simple")
    assert len(caught.value.candidates) == 2
    assert caught.value.candidates == sorted(caught.value.candidates)


@pytest.mark.parametrize("correction", [
    "ASK: Which path?", "WORKER {}", "CONSULT claude:opus: inspect this",
])
def test_metadata_correction_cannot_turn_unresolved_controls_into_work(tmp_path, correction):
    replies = iter(["KIND: test rote\nKIND: security complex\nInspect the project.", correction])
    session = Session("Inspect the project", ArtifactStore(tmp_path), lambda *a, **k: next(replies))
    with pytest.raises(RunStalled, match="unresolved .* request cannot become a task"):
        session.next_task()


def test_metadata_correction_can_still_finish_the_decision(tmp_path):
    replies = iter(["KIND: backend\nKIND: backend complex\nInspect it.", "DONE"])
    session = Session("Inspect it", ArtifactStore(tmp_path), lambda *a, **k: next(replies))
    assert session.next_task() is None


@pytest.mark.parametrize("path,allowed", [
    ("src/config.py", True), ("src/deep/config.py", True),
    ("src/deep/nested/config.py", True), ("src/myconfig.py", False),
    ("src/myconfig.py.bak", False),
])
def test_recursive_scope_glob_preserves_the_filename(path, allowed):
    assert TaskScope(("src/**/config.py",)).permits(path) is allowed


@pytest.mark.parametrize("path", [
    "src/nested/config.py", "src/a/nested/config.py", "src/nested/b/config.py",
    "src/a/nested/b/config.py",
])
def test_each_recursive_directory_can_independently_match_zero_levels(path):
    assert TaskScope(("src/**/nested/**/config.py",)).permits(path)


def test_diff_payload_that_resembles_headers_keeps_its_real_path_and_size(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "notes.txt"
    source.write_text("-- old.txt\n")
    project = Project(root)
    before = project.contents()
    source.write_text("++ tests/fake.py\n++ second\n++ third\n")
    diff = project.diff(before)
    report = TaskScope(("notes.txt",), max_lines=2).assess(diff)
    assert changed_paths(diff) == ["notes.txt"]
    assert count_change_lines_by_path(diff) == {"notes.txt": 4}
    assert report.changed_lines == report.code_lines == 4 and report.test_lines == 0
    assert report.within_scope and report.oversized


def test_hunk_boundaries_do_not_hide_the_next_real_file(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    project = Project(root)
    before = project.contents()
    (root / "a.txt").write_text("++ fake.txt\n")
    (root / "b.txt").write_text("-- another.txt\n")
    report = TaskScope(("a.txt",)).assess(project.diff(before))
    assert report.changed == ["a.txt", "b.txt"]
    assert report.out_of_scope == ["b.txt"] and report.changed_lines == 2


@pytest.mark.parametrize("snapshot", [[], ["F1"], {"findings": "F1"}, {"findings": [{}]}])
def test_malformed_saved_finding_snapshots_report_debt_instead_of_raising(snapshot):
    saved = {"workflow": {"tasks": [{"task_id": "t1", "facts": [], "open_at_close": snapshot}],
                          "run": {"facts": []}}, "findings": []}
    state = from_result(saved)
    assert state.problems and not legacy_parity(state)["agree"]


@pytest.mark.parametrize("source", ["task", "run"])
def test_non_string_saved_fact_details_remain_active_and_report_malformed_data(source):
    fact = {"kind": "integrity", "detail": 7, "stage": "draft"}
    saved = {"workflow": {"tasks": [{"task_id": "t1", "facts": [fact] if source == "task" else []}],
                          "run": {"facts": [fact] if source == "run" else []}}, "findings": []}
    state = from_result(saved)
    assert state.problems and not legacy_parity(state)["agree"]
    assert len(state.active) == 1 and state.active[0].detail == "7"


@pytest.mark.parametrize("contract", [["broken"], {"owner": "lead", "required": ["broken"]}])
def test_malformed_contract_records_produce_completion_blockers(contract):
    task = TaskOutcome("t1", "implementation", lead="lead", closed_as="closed",
                       dispatch={"state": "dispatched", "owner": "lead"}, contract=contract,
                       source_before="before", source_after="after", dependency="n/a", partial={})
    assert any("contract" in problem for problem in missing_facts(task))
    assert any("contract" in blocker for blocker in completion_blockers([task]))
    assert task.to_dict()["unsatisfied"] == []
