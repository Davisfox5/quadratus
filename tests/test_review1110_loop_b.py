"""Parallel batches behave like serial tasks (review 1110, session second half).

Item 2: task ids come from one counter on the Session, never from the length
of the history, so the id of a child that failed or was not merged is not
issued again. Item 3: a merged child's design evidence is copied into the
project before its copy is deleted, without following a link, so the approved
digests and the recorded screenshot paths name files that exist; a child that
was not merged leaves a record that says its renders are not verified, and
renders approved before the merge gate changed the child's own files do not
stand (the merge fix is rendered and reviewed by nobody). Item 8: a
child is shown the standing rules and operator rulings, and what a capped or
failed child left reaches the task that continues it. Item 15: one function
reads the directive lines off a named task for the serial loop and for every
block of a batch; a line a batch cannot honour (RESOLVES, CONTINUES, and
TIER: direct on a run with the tier on) sends the batch back.

Whole Session runs on scripted replies, as tests/test_parallel_tasks.py
drives them; no vendor CLI and no browser.
"""

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from quadratus import batch_evidence
from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.integration import GateResult
from quadratus.providers import ProviderError, TurnLimitReached
from quadratus.session import RunStalled, Session, SessionConfig, SurveyConfig, TaskSpec
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import covered_met, judging
from tests.test_parallel_tasks import BATCH, Orchestrated, _block
from tests.test_parallel_tasks import _session as parallel_session

SERIAL = _block("c.py")


def _reading_proof(rid):
    """The proof scenario the development line's intake asks of a task that
    COVERS a requirement. Here it is a reading claim at README.md, which the
    scripted auditor judges on its own ``O<n>: SUFFICIENT`` line; a test
    scenario would need a gate that executes its case."""
    return H.proofs_for(rid, "README.md", evidence="reading")


#: The scripted auditor's line for each reading claim above.
READ = "SUFFICIENT - README.md states it"


def _design_block(path, covers=None):
    scope = {"permitted_paths": [path], "intended_result": f"{path} shows the button",
             "acceptance": ["the page shows a button"], "max_lines": 20}
    if covers:
        scope["proofs"] = _reading_proof(covers)
    return (f"KIND: frontend simple\nSCOPE: {json.dumps(scope)}\nAdd a button in {path}"
            + (f"\nCOVERS: {covers}" if covers else ""))


def _covering_block(path, rid):
    scope = json.dumps({"permitted_paths": [path], "intended_result": f"{path} written",
                        "acceptance": [f"{path} exists"], "max_lines": 20, "proofs": _reading_proof(rid)})
    return f"KIND: backend simple\nSCOPE: {scope}\nwork in {path}\nCOVERS: {rid}"


DESIGN_BATCH = "PARALLEL\n" + _design_block("templates/a.html") + "\n---\n" + _design_block("templates/b.html")


class Script:
    """A scripted orchestrator, leads and reviewers. ``lead`` is called as
    ``lead(root, target, prompt)`` and returns the lead's reply (or raises);
    by default it writes the one file its task names."""

    def __init__(self, plan, *, lead=None, audit=None):
        self.plan = list(plan)
        self.lead = lead or self.write
        self.audit = audit
        self.decisions, self.leads, self.audits, self.roots = [], [], [], []

    @staticmethod
    def write(root, target, prompt):
        (Path(root) / target).parent.mkdir(parents=True, exist_ok=True)
        (Path(root) / target).write_text(f"# {target}\n")
        return f'Wrote it\nCHANGED: ["{target}"]'

    def invoke_for(self, root):
        def invoke(model, prompt, system=None, allow_writes=False):
            if "Name the single next task" in prompt:
                self.decisions.append(prompt)
                return self.plan.pop(0) if self.plan else "DONE"
            if "You are an independent auditor." in prompt:
                self.audits.append(prompt)
                return self.audit
            if "You are leading" in prompt:
                target = re.search(r" in (\S+\.(?:py|html))", prompt).group(1)
                self.leads.append((str(root), target, prompt))
                return self.lead(root, target, prompt)
            if "final renders of this design work" in prompt:
                return "APPROVED"
            # A review of work that covers requirements owes a MET line for each.
            return ("NO FINDINGS\n" + covered_met(prompt)).strip()
        return invoke


def _session(tmp_path, script, *, invariants=None, **config):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "keep.py").write_text("# existing\n")

    def fork(root):
        script.roots.append(root)
        return script.invoke_for(root), (lambda: None)
    config.setdefault("requirements_ledger", False)
    session = Session("Build two things", ArtifactStore(tmp_path / "artifacts"), script.invoke_for(project),
                      invariants=invariants,
                      config=SessionConfig(project=project, allow_writes=True, fork=fork, **config))
    return session, project


def _captures(root, target, prompt):
    """A lead that edits its template and leaves a clean self-capture, as
    ``python -m quadratus.design_evidence`` would, in its own working copy.
    DESIGN_BATCH is always the first reply, so page a is t1 and page b t2."""
    reply = Script.write(root, target, prompt)
    task = {"templates/a.html": "t1", "templates/b.html": "t2"}[target]
    H.evidence(Path(root), task, age=H.FRESH)
    return reply


def _at_close_out(monkeypatch, before):
    """Call ``before(root)`` when a task's close-out is written. The harness
    writes the close-out itself (no model is asked), so this is the last step
    inside a task, after its renders were checked and approved."""
    real = Session._task_close_out

    def close_out(self, lead, spec, task, **kw):
        before(Path(self.project))
        return real(self, lead, spec, task, **kw)
    monkeypatch.setattr(Session, "_task_close_out", close_out)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _mismatches(session):
    return [m for o in session.task_outcomes for m in o.mismatches]


class PassingGate:
    def __init__(self, project):
        self.cwd = Path(project)

    def run(self):
        return GateResult(True, "check", 0, "1 passed")


# -- item 2: one monotonic id counter ------------------------------------------------

def test_a_new_run_starts_at_t1_and_a_dispatched_task_spends_its_id(tmp_path):
    session, _ = _session(tmp_path, Script([]))
    assert session._next_task_id() == "t1" and session._next_task_id(2) == "t3"
    assert session._next_task_id() == "t1", "reading the next id does not spend it"
    with pytest.raises(RunStalled, match="must declare a scope"):
        session.run_task(TaskSpec("t1", "Explain the module."))
    assert session.history == [] and session._next_task_id() == "t2", "spent, whatever became of the task"


def test_an_unmerged_childs_id_is_not_issued_again(tmp_path):
    """t2 wrote outside its scope and was not merged; the next task was named
    t2 again, sharing its evidence folder, capture fixtures and outcome
    lookups with the first t2. (Step 2 on the development line: such a child
    is a failed task on the history and the planner is asked again, where the
    run used to stop with one history entry.)"""
    script = Orchestrated([BATCH, "DONE", SERIAL], rogue="b.py", lead_delay=0)
    session, project = parallel_session(tmp_path, script)
    session.run(max_tasks=4)
    assert [(s.task_id, s.outcome) for s in session.history] == [("t1", "closed"), ("t2", "failed")]
    assert not (project / "b.py").exists() and "t2" in session._partial_tasks
    spec = session.next_task()
    assert spec.task_id == "t3", "t2 is spent although nothing of it was merged"
    assert [o.task_id for o in session.task_outcomes].count("t2") == 1


def test_a_child_that_raised_keeps_its_id_too(tmp_path):
    def lead(root, target, prompt):
        if target == "b.py":
            raise ProviderError("the transport gave up")
        return Script.write(root, target, prompt)
    script = Script([BATCH, SERIAL], lead=lead)
    session, _ = _session(tmp_path, script)
    session.run(max_tasks=4)
    assert [s.task_id for s in session.history] == ["t1"]
    assert any("Parallel task t2 was not merged (ProviderError" in f for f in session.open_findings)
    assert session.next_task().task_id == "t3"


def test_a_task_or_batch_that_is_sent_back_keeps_the_id_it_was_told(tmp_path):
    """Naming does not spend an id: the orchestrator is told the next id for
    its capture fixtures, and a correction must not move it."""
    bad = "PARALLEL\n" + _block("a.py") + "\nRESOLVES: F1\n---\n" + _block("b.py")
    script = Script([bad, BATCH, SERIAL, "DONE"])
    session, _ = _session(tmp_path, script)
    session.run(max_tasks=6)
    assert [s.task_id for s in session.history] == ["t1", "t2", "t3"]
    assert "BATCH SENT BACK" in script.decisions[1]


def test_children_and_listed_tasks_run_under_the_ids_the_counter_issued(tmp_path):
    script = Script([SERIAL, BATCH, "DONE"])
    session, _ = _session(tmp_path, script)
    session.run(max_tasks=6)
    assert session.parallel_batches[0]["tasks"] == ["t2", "t3"]
    assert [o.task_id for o in session.task_outcomes] == ["t1", "t2", "t3"] and session._next_task_id() == "t4"
    (tmp_path / "listed").mkdir()
    session, _ = _session(tmp_path / "listed", Script([]))
    session.run(max_tasks=4, tasks=[_block("a.py"), _block("b.py")])
    assert session.explicit["ran"] == [dict(index=1, task="t1"), dict(index=2, task="t2")]


def test_the_orchestrator_is_told_the_id_the_counter_will_issue(tmp_path):
    from tests.test_capture_scope_guidance import _session as capture_session
    session, seen = capture_session(tmp_path, iter(["DONE", "DONE"]))
    session._issue_task_id("t1")
    session._issue_task_id("t2")       # a child that left no history entry
    assert session.history == [] and session.next_task() is None
    assert "This task's id will be t3." in seen[-1]


def test_the_progress_note_the_monitor_reads_back_carries_the_tasks_own_number(tmp_path):
    """monitor_story reads 'task N: ...' back as task tN."""
    notes = []
    session, _ = _session(tmp_path, Script([]), progress=notes.append)
    session._ids_issued = 4
    session._read_directives(TaskSpec(session._next_task_id(), "Explain the module."))
    assert notes == ["task 5: Explain the module. [general/standard]"]
    assert session._task_number("t12") == "12" and session._task_number("t3-merge") == "t3-merge"


def test_no_id_is_derived_from_the_history_length():
    import inspect
    source = inspect.getsource(S.Session)
    assert not re.search(r't\{len\(self\.history\)', source)
    assert not re.search(r'task \{len\(self\.history\)', source)


# -- item 3: a merged child's design evidence comes home ---------------------------------

def test_a_merged_childs_design_evidence_is_in_the_project_and_the_run_completes(tmp_path):
    """Before: the run ended CompletionUnproven, 'approved evidence changed
    after its review', because the approved files had gone with the copy."""
    script = Script([DESIGN_BATCH, "DONE"], lead=_captures)
    session, project = _session(tmp_path, script)
    session.run(max_tasks=4)
    assert session.completed, session.stop_reason
    assert not session._note_replaced_evidence() and _mismatches(session) == []
    assert [o.task_id for o in session.task_outcomes] == ["t1", "t2"]
    for outcome in session.task_outcomes:
        files = outcome.delivery["files"]
        assert len(files) == 5 and outcome.edges["reviewer"] is True
        for rel, digest in files.items():
            assert _sha(project / rel) == digest, f"{rel} holds the approved bytes"
    for record in [*session.design_checks, *[o.evidence for o in session.task_outcomes]]:
        shots = [s for s in record["screenshots"] if s.endswith(".png")]
        assert len(shots) == 2 and record["verified"] is True
        for shot in shots:
            assert Path(shot).is_file() and Path(shot).is_relative_to(project), shot
    assert all(not Path(root).exists() for root in script.roots), "the copies are gone; the evidence is not"


@pytest.mark.requirements_ledger
def test_the_audit_is_shown_render_paths_that_exist(tmp_path):
    batch = ("PARALLEL\n" + _design_block("templates/a.html", covers="R1") + "\n---\n"
             + _design_block("templates/b.html", covers="R2"))
    script = Script([batch, "DONE"], lead=_captures, audit="R1: MET - templates/a.html\nR2: MET - templates/b.html"
                                                           f"\nO1: {READ}\nO2: {READ}")
    session, project = _session(tmp_path, script, requirements_ledger=True)
    (project / "README.md").write_text("Page a and page b each show a button.\n")
    session.memory.ledger.requirements = {"R1": "page a shows a button", "R2": "page b shows a button"}
    session.requirement_reviews.append({"reviewer": "claude:opus", "result": "reviewed"})
    session.run(max_tasks=4)
    assert session.completed, session.stop_reason
    (prompt,) = script.audits
    paths = re.findall(r"(/\S+?\.png)", prompt)
    assert len(paths) == 4, prompt
    assert all(Path(p).is_file() and Path(p).is_relative_to(project) for p in paths), paths


def test_evidence_that_cannot_be_brought_over_leaves_the_design_unverified(tmp_path):
    """The project's evidence folder is a link out of the project: nothing is
    written through it, and the approval does not stand in for missing files."""
    script = Script([DESIGN_BATCH, "DONE"], lead=_captures)
    session, project = _session(tmp_path, script)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".quadratus").mkdir()
    (project / ".quadratus" / "design-evidence").symlink_to(outside, target_is_directory=True)
    session.run(max_tasks=4)
    assert list(outside.iterdir()) == [], "nothing was written through the link"
    assert not session.completed
    assert session.stop_reason.startswith("DesignUnverified: task t"), session.stop_reason
    assert "could not be brought into the project" in session.stop_reason
    assert script.plan == ["DONE"], "the run stopped after the batch"
    for outcome in session.task_outcomes:
        assert outcome.edges["evidence"] is False and outcome.evidence["verified"] is False
        assert outcome.evidence["screenshots"] == []
    assert {t for t, _ in session._design_debt()} == {t for t, _ in session._design_unverified} == {"t1", "t2"}
    assert _mismatches(session) == [], "typed and legacy design debt agree"
    assert (project / "templates" / "a.html").exists(), "the source was merged all the same"


def test_an_unmerged_childs_renders_are_not_brought_over_and_its_record_says_so(tmp_path, monkeypatch):
    def fails(root):
        if (root / "templates" / "b.html").exists() and not (root / "templates" / "a.html").exists():
            raise ProviderError("the task failed at its close")    # after t2's renders were approved
    _at_close_out(monkeypatch, fails)
    script = Script([DESIGN_BATCH, "DONE"], lead=_captures)
    session, project = _session(tmp_path, script)
    session.run(max_tasks=4)
    assert not session.completed and any("t2 was not merged (ProviderError" in f for f in session.open_findings)
    by_task = {r["task"]: r for r in session.design_checks}
    assert by_task["t2"]["merged"] is False and "merged" not in by_task["t1"]
    assert (project / ".quadratus" / "design-evidence" / "t1" / "summary.json").is_file()
    assert not (project / ".quadratus" / "design-evidence" / "t2").exists()
    # The project's record of renders it does not hold: not verified, no
    # path into the deleted copy, and the harness's own facts say so.
    assert by_task["t2"]["verified"] is False and by_task["t2"]["screenshots"] == []
    assert by_task["t2"]["problem"].startswith("not merged: its renders show work that is not in the project")
    assert by_task["t2"]["final_review"]["approved"] is True, "the review is kept as history"
    assert by_task["t1"]["verified"] is True and by_task["t1"]["screenshots"]
    unmerged = next(o for o in session.task_outcomes if o.task_id == "t2")
    assert unmerged.evidence["merged"] is False and unmerged.evidence["screenshots"] == [], \
        "the outcome's own copy of the record names no path into the deleted copy either"
    assert all(str(root) not in json.dumps([session.design_checks, [o.evidence for o in session.task_outcomes]])
               for root in script.roots)
    session.checks.append(dict(passed=True, receipts=[]))
    assert "- renders from t2: NOT verified" in session._harness_facts()


def test_a_verified_record_naming_a_render_the_copy_lost_is_not_brought_over_as_verified(tmp_path, monkeypatch):
    """The copy mirrors what the child holds; a render its verified record
    names and the copy no longer holds would leave the record, and the audit,
    naming a file the project does not have."""
    def loses(root):
        lost = root / ".quadratus" / "design-evidence" / "t1" / "mobile" / "page.png"
        if lost.exists():
            lost.unlink()               # after t1's renders were checked and approved
    _at_close_out(monkeypatch, loses)
    script = Script([DESIGN_BATCH, "DONE"], lead=_captures)
    session, project = _session(tmp_path, script)
    session.run(max_tasks=4)
    assert not session.completed
    assert session.stop_reason.startswith("DesignUnverified: task t1"), session.stop_reason
    assert "the copy no longer holds mobile/page.png, which its verified record names" in session.stop_reason
    by_task = {r["task"]: r for r in session.design_checks}
    assert by_task["t1"]["verified"] is False and by_task["t1"]["screenshots"] == []
    assert by_task["t2"]["verified"] is True
    assert all(Path(s).is_file() for s in by_task["t2"]["screenshots"] if s.endswith(".png"))
    assert _mismatches(session) == [], "typed and legacy design debt agree"


class _MergeGate:
    """Fails on the merged tree until the named file holds 'fixed'; a child
    has no gate, so it only ever runs in the project."""

    def __init__(self, project, page):
        self.cwd, self.page, self.runs = Path(project), Path(project) / page, []

    def run(self):
        from tests.gate_facts import ASSERTION_FAILURE
        ok = self.page.exists() and "fixed" in self.page.read_text()
        self.runs.append(ok)
        return (GateResult(True, "check", 0, "1 passed") if ok else
                GateResult(False, "check", 1, "1 failed", report=ASSERTION_FAILURE))


def _design_batch_with_a_merge_fix(tmp_path, page, *, batch=DESIGN_BATCH, lead=_captures):
    """A batch under a gate the merged tree fails once; the merge fix
    rewrites ``page``."""
    project = tmp_path / "project"
    project.mkdir()
    gate = _MergeGate(project, page)
    script = Script([batch, "DONE"], lead=lead)
    session, project = _session(tmp_path, script, integration_gate=gate, max_gate_fixes=1)
    parent = session.invoke

    def invoke(model, prompt, system=None, allow_writes=False):
        if "integration check failed" in prompt:
            (project / page).parent.mkdir(parents=True, exist_ok=True)
            (project / page).write_text("<h1>another page altogether, fixed</h1>\n")
            return f'Fixed.\nCHANGED: ["{page}"]'
        return parent(model, prompt, system, allow_writes)
    session.invoke = invoke
    session.run(max_tasks=4)
    return session, gate


def test_renders_approved_before_the_merge_fix_rewrote_the_page_do_not_stand(tmp_path):
    """A serial task's gate fix comes before its capture. A child's renders
    are approved in its copy and the merge gate's fix comes after, with no
    capture and no review: once a merged child's renders reach the project,
    an approved render of the page as it was would stand for the page as
    delivered. (Before the renders were brought over no such run completed.)"""
    session, gate = _design_batch_with_a_merge_fix(tmp_path, "templates/a.html")
    assert gate.runs == [False, True] and session.parallel_batches[0]["merge_gate"]["gate_fixes"] == 1
    assert not session.completed
    assert session.stop_reason.startswith("DesignUnverified: task t1"), session.stop_reason
    assert "the merge fix changed templates/a.html after its renders were approved" in session.stop_reason
    by_task = {o.task_id: o for o in session.task_outcomes}
    assert by_task["t1"].edges["evidence"] is False and by_task["t1"].evidence["verified"] is False
    assert by_task["t2"].edges["evidence"] is True and by_task["t2"].evidence["verified"] is True, \
        "another child's page is another task's work"
    records = {r["task"]: r for r in session.design_checks}
    assert records["t1"]["verified"] is False and records["t1"]["screenshots"] == []
    assert [t for t, _ in session._design_unverified] == ["t1"] and _mismatches(session) == []


def test_a_merge_fix_counts_against_the_renders_of_the_page_it_changed_and_no_other(tmp_path):
    session, _ = _design_batch_with_a_merge_fix(tmp_path, "templates/b.html")
    assert session.stop_reason.startswith("DesignUnverified: task t2"), session.stop_reason
    records = {r["task"]: r for r in session.design_checks}
    assert records["t1"]["verified"] is True and records["t2"]["verified"] is False
    # Another task's file, as a later serial task's would be: a page beside
    # a module, and the merge fix rewrites the module.
    mixed = "PARALLEL\n" + _design_block("templates/a.html") + "\n---\n" + _block("b.py")
    (tmp_path / "mixed").mkdir()
    session, gate = _design_batch_with_a_merge_fix(
        tmp_path / "mixed", "b.py", batch=mixed,
        lead=lambda root, target, prompt: (_captures if target.endswith(".html") else Script.write)(root, target, prompt))
    assert gate.runs == [False, True] and session.completed, session.stop_reason
    assert [(r["task"], r["verified"]) for r in session.design_checks] == [("t1", True)]


def test_a_merge_gate_that_leaves_the_pages_alone_leaves_their_renders_standing(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    script = Script([DESIGN_BATCH, "DONE"], lead=_captures)
    session, _ = _session(tmp_path, script, integration_gate=PassingGate(project))
    session.run(max_tasks=4)
    assert session.completed, session.stop_reason
    assert all(r["verified"] is True and r["screenshots"] for r in session.design_checks)
    assert session.open_findings == [] and session._design_unverified == []


def test_a_task_with_no_renders_brings_nothing_and_owes_nothing(tmp_path):
    script = Orchestrated([BATCH, "DONE"], lead_delay=0)
    session, project = parallel_session(tmp_path, script)
    session.run(max_tasks=4)
    assert session.completed and not (project / ".quadratus" / "design-evidence").exists()


@pytest.mark.requirements_ledger
def test_the_independent_check_finds_a_merged_batchs_approved_renders_in_the_project(tmp_path, monkeypatch):
    """Whole controller on the replay harness: real Fleet forks, the real
    gate, scripted CLI replies. tools/truth_check.py imports nothing from
    quadratus and compares each approved digest with the project's file, so
    it is the files in the project that carry the verdict."""
    import importlib.util

    from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED
    from tests.lifecycle.test_lifecycle_matrix import Script as Replies
    spec = importlib.util.spec_from_file_location(
        "truth_check", Path(__file__).resolve().parents[1] / "tools" / "truth_check.py")
    truth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(truth)
    pages = {"t1": "templates/a.html", "t2": "templates/b.html"}

    def block(path, rid):
        scope = dict(permitted_paths=[path], intended_result=f"{path} shows a button",
                     acceptance=[f"{path} has a button"], max_lines=20, proofs=_reading_proof(rid))
        return f"KIND: frontend simple\nSCOPE: {json.dumps(scope)}\nAdd a button to {path}.\nCOVERS: {rid}"
    docs = ("KIND: docs simple\nSCOPE: " + json.dumps(dict(
        permitted_paths=["README.md"], intended_result="README names add", acceptance=["README names add"],
        max_lines=20, proofs=_reading_proof("R3"))) + "\nDocument add in README.md.\nCOVERS: R3")
    plan = ["REQUIREMENTS:\nR1: page a shows a button\nR2: page b shows a button\nR3: the README names add\n"
            "PARALLEL\n" + block(pages["t1"], "R1") + "\n---\n" + block(pages["t2"], "R2"), docs]

    def lead(call, replay):
        if call.task == "t3":
            H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
            return 'Documented add.\nCHANGED: ["README.md"]'
        H.write(call, {pages[call.task]: "<button>go</button>\n"})
        H.evidence(Path(call.cwd), call.task, age=H.FRESH)
        return f'Added the button.\nCHANGED: ["{pages[call.task]}"]'
    replies = Replies(orchestrator=lambda call, replay: plan.pop(0) if plan else "DONE", lead=lead,
                      **{"requirements-review": lambda call, replay: "COMPLETE",
                         "auditor": judging(lambda call, replay: ("R1: MET - templates/a.html\n"
                                                                  "R2: MET - templates/b.html\nR3: MET - README.md"),
                                            reading=READ),
                         "design-review": lambda call, replay: "APPROVED"})
    files = {**FILES, "app.py": FIXED, "templates/a.html": "<p>a</p>\n", "templates/b.html": "<p>b</p>\n"}
    replay = H.run(tmp_path, monkeypatch, replies, files=files, max_tasks=5)
    assert replay.result.completed, replay.result.error
    assert [b["tasks"] for b in H.result_json(replay)["parallel_batches"]] == [["t1", "t2"]]
    assert all(Path(c.cwd) != replay.project for c in replay.of("lead") if c.task in pages), \
        "the two UI leads worked in copies"
    verdict = truth.judge(replay.result.run_dir, replay.project)
    assert verdict["verdict"] == truth.VERIFIED, verdict
    (replay.project / ".quadratus" / "design-evidence" / "t1" / "desktop" / "page.png").unlink()
    verdict = truth.judge(replay.result.run_dir, replay.project)
    assert verdict["verdict"] == truth.UNVERIFIED and any(
        "task t1" in reason and "page.png is missing" in reason for reason in verdict["unverified"]), verdict


# the copy itself (quadratus.batch_evidence)

def _child_with_evidence(tmp_path, task="t2"):
    child, project = tmp_path / "child", tmp_path / "project"
    child.mkdir()
    project.mkdir()
    H.evidence(child, task, age=H.STALE)
    return child, project, child / ".quadratus" / "design-evidence" / task


def test_the_copy_takes_the_evidence_set_and_nothing_else(tmp_path):
    child, project, folder = _child_with_evidence(tmp_path)
    (folder / "notes.txt").write_text("a lead's scratch file")
    (folder / "desktop" / "trace.zip").write_bytes(b"x" * 10)
    result = batch_evidence.adopt(child, project, "t2")
    assert result["present"] and result["problems"] == [] and result["removed"] == []
    home = project / ".quadratus" / "design-evidence" / "t2"
    copied = sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file())
    assert copied == sorted(batch_evidence.EVIDENCE_NAMES)
    for name in batch_evidence.EVIDENCE_NAMES:
        rel = f".quadratus/design-evidence/t2/{name}"
        assert result["copied"][rel] == _sha(folder / name) == _sha(home / name)
    shot = "desktop/page.png"
    assert (home / shot).stat().st_mtime_ns == (folder / shot).stat().st_mtime_ns, "the capture's own time"


def test_the_copy_reads_no_link_and_no_special_file_in_the_child(tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"\x89PNG\r\n\x1a\n not the task's render")
    for case in ("leaf", "view", "task", "fifo"):
        base = tmp_path / case
        base.mkdir()
        child, project, folder = _child_with_evidence(base)
        if case == "leaf":
            (folder / "desktop" / "page.png").unlink()
            (folder / "desktop" / "page.png").symlink_to(secret)
        elif case == "view":
            real = base / "elsewhere"
            (folder / "mobile").rename(real)
            (folder / "mobile").symlink_to(real, target_is_directory=True)
        elif case == "task":
            real = base / "elsewhere"
            folder.rename(real)
            folder.symlink_to(real, target_is_directory=True)
        else:
            (folder / "summary.json").unlink()
            os.mkfifo(folder / "summary.json")
        result = batch_evidence.adopt(child, project, "t2")
        assert result["present"] and result["problems"], case
        assert result["copied"] == {} and not (project / ".quadratus").exists(), \
            f"{case}: a set that cannot be read whole writes nothing"


def test_the_copy_writes_through_no_link_in_the_project(tmp_path):
    child, project, folder = _child_with_evidence(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"the operator's own file")
    home = project / ".quadratus" / "design-evidence" / "t2"
    (home / "desktop").mkdir(parents=True)
    (home / "desktop" / "page.png").symlink_to(outside)
    result = batch_evidence.adopt(child, project, "t2")
    assert result["problems"] == []
    assert outside.read_bytes() == b"the operator's own file", "the link's target was never written"
    published = home / "desktop" / "page.png"
    assert not published.is_symlink() and _sha(published) == _sha(folder / "desktop" / "page.png")
    assert [p.name for p in (home / "desktop").iterdir() if p.name.startswith(".merge-")] == []

    for linked in (".quadratus", "design-evidence", "t2", "mobile"):
        base = tmp_path / f"linked-{linked}"
        base.mkdir()
        child, project, _ = _child_with_evidence(base)
        away = base / "away"
        away.mkdir()
        parts = [".quadratus", "design-evidence", "t2", "mobile"]
        spot = project.joinpath(*parts[:parts.index(linked)])
        spot.mkdir(parents=True, exist_ok=True)
        (spot / linked).symlink_to(away, target_is_directory=True)
        result = batch_evidence.adopt(child, project, "t2")
        assert result["problems"], linked
        assert list(away.iterdir()) == [], f"{linked}: nothing landed behind the link"


def test_the_copy_leaves_the_projects_set_the_same_as_the_childs(tmp_path):
    """Task ids restart at t1 each run, so an earlier run's renders can sit
    under the same name: what the child did not capture does not stay."""
    child, project, folder = _child_with_evidence(tmp_path)
    H.evidence(project, "t2", age=H.STALE, target="http://127.0.0.1:5000/old")
    (folder / "mobile" / "page.png").unlink()
    (folder / "mobile" / "evidence.json").unlink()
    result = batch_evidence.adopt(child, project, "t2")
    home = project / ".quadratus" / "design-evidence" / "t2"
    assert result["problems"] == []
    assert sorted(result["removed"]) == [".quadratus/design-evidence/t2/mobile/evidence.json",
                                         ".quadratus/design-evidence/t2/mobile/page.png"]
    assert not (home / "mobile" / "page.png").exists()
    assert json.loads((home / "summary.json").read_text())["target"] == "http://127.0.0.1:5000/import"


def test_a_child_with_no_evidence_folder_touches_nothing(tmp_path):
    child, project = tmp_path / "child", tmp_path / "project"
    child.mkdir()
    project.mkdir()
    H.evidence(project, "t2", age=H.STALE)
    before = {p: _sha(p) for p in project.rglob("*") if p.is_file()}
    result = batch_evidence.adopt(child, project, "t2")
    assert result == dict(present=False, copied={}, removed=[], problems=[])
    assert {p: _sha(p) for p in project.rglob("*") if p.is_file()} == before
    assert batch_evidence.adopt(child, project, "../t2")["problems"] == ["the task id is not a plain folder name"]


def test_the_copy_is_held_to_the_review_deliverys_bounds(tmp_path, monkeypatch):
    from quadratus import runtime
    assert batch_evidence._bounds() == (runtime._MAX_EVIDENCE_BYTES, runtime._MAX_EVIDENCE_TOTAL)
    assert set(batch_evidence.EVIDENCE_NAMES) == {
        "summary.json", "desktop/page.png", "desktop/evidence.json", "mobile/page.png", "mobile/evidence.json"}
    for name in batch_evidence.EVIDENCE_NAMES:
        assert runtime._EVIDENCE_PATH.fullmatch(f".quadratus/design-evidence/t2/{name}")
    child, project, _ = _child_with_evidence(tmp_path)
    monkeypatch.setattr(runtime, "_MAX_EVIDENCE_BYTES", 16)
    result = batch_evidence.adopt(child, project, "t2")
    assert any("larger than the evidence bound" in p for p in result["problems"])
    assert not (project / ".quadratus").exists()


def test_only_paths_under_the_copy_are_rebased(tmp_path):
    moved = batch_evidence.moved_path
    assert moved("/tmp/copy/.quadratus/design-evidence/t2/desktop/page.png", "/tmp/copy", "/home/p") == \
        "/home/p/.quadratus/design-evidence/t2/desktop/page.png"
    assert moved("target: http://127.0.0.1:5000/import", "/tmp/copy", "/home/p") is None
    assert moved("/tmp/copy-other/x.png", "/tmp/copy", "/home/p") is None


# -- item 8: what a child is shown, and what it hands back -------------------------------------

def test_a_parallel_lead_is_shown_the_standing_rules_and_operator_rulings(tmp_path):
    script = Script([SERIAL, BATCH, "DONE"])
    session, project = _session(tmp_path, script, invariants=["Never change keep.py."])
    session.memory.ledger.rulings.append("Q: tabs or spaces? -- A: spaces")
    session.run(max_tasks=6)
    assert session.completed, session.stop_reason
    children = [prompt for root, _, prompt in script.leads if Path(root) != project]
    assert len(children) == 2
    for prompt in children:
        assert "## Standing rules (always in force)\n\n- Never change keep.py." in prompt
        assert "## Operator rulings (asked and answered; do not re-ask)\n\n- Q: tabs or spaces? -- A: spaces" in prompt
        assert "_Nothing completed yet._" in prompt, "its ledger of completed work is its own, and empty"
    serial = next(prompt for root, _, prompt in script.leads if Path(root) == project)
    assert "- Never change keep.py." in serial, "the same blocks a serial lead is shown"


def test_a_capped_childs_handoff_reaches_the_task_that_continues_it(tmp_path):
    """Before: the child's record stayed in the child, so the continuation's
    lead had no handoff note and no baseline to diff against."""
    capped = []

    def lead(root, target, prompt):
        if target == "a.py" and not capped:
            capped.append(root)
            (Path(root) / "a.py").write_text("# half\n")
            raise TurnLimitReached("capped", partial_text="I had read the module", turns=14)
        return Script.write(root, target, prompt)
    finish = _block("a.py", "finish").replace("\nfinish", "\nCONTINUES: t1\nfinish")
    script = Script([BATCH, finish, "DONE"], lead=lead)
    session, project = _session(tmp_path, script)
    session.run(max_tasks=6)
    assert [(s.task_id, s.outcome) for s in session.history] == [
        ("t1", "turn_limited"), ("t2", "closed"), ("t3", "closed")]
    record = session.turn_limited_records["t1"]
    assert record["changed"] == ["a.py"] and record["turns"] == 14
    assert session._cap_baselines["t1"] == {"a.py": b""}, "what a.py held before the capped call"
    root, _, prompt = script.leads[-1]
    assert Path(root) == project and "## Handoff from t1" in prompt
    assert "I had read the module" in prompt, "labelled narration from the capped call"
    assert session.completed, session.stop_reason
    assert (project / "a.py").read_text() == "# a.py\n"


def test_a_failed_childs_record_comes_back_with_it(tmp_path):
    def lead(root, target, prompt):
        if target == "a.py" and "CONTINUES" not in prompt and "Handoff" not in prompt:
            (Path(root) / "a.py").write_text("# half\n")
            return "ASK: which name should the function take?"     # a lead has no ASK channel
        return Script.write(root, target, prompt)
    finish = _block("a.py", "finish").replace("\nfinish", "\nCONTINUES: t1\nfinish")
    script = Script([BATCH, finish, "DONE"], lead=lead)
    session, project = _session(tmp_path, script)
    session.run(max_tasks=6)
    assert session.history[0].outcome == "failed", [(s.task_id, s.outcome) for s in session.history]
    assert session.failed_records["t1"]["cause"] == "channel" and "t1" in session._cap_baselines
    assert "## Handoff from t1" in script.leads[-1][2]


def test_an_unmerged_childs_record_does_not_come_back(tmp_path):
    """Its files are not in the project, so a note saying what it changed
    there would be false. (Step 2 on the development line: the parent files
    its own failed record for such a child, which says nothing was merged.)"""
    def lead(root, target, prompt):
        if target == "b.py":
            (Path(root) / "shared.py").write_text("# not in scope\n")
        return Script.write(root, target, prompt)
    script = Script([BATCH], lead=lead)
    session, project = _session(tmp_path, script)
    session.run(max_tasks=4)
    assert session.failed == ["t2"] and not (project / "b.py").exists()
    record = session.failed_records["t2"]
    assert record["error"].startswith("not merged: changed files outside its scope: shared.py")
    assert record["note"] == "parallel batch: nothing was merged" and record["content"] == {}
    assert "already on disk" not in json.dumps(record), "the child's own note about the project is not kept"
    assert session._cap_baselines["t2"] == {"b.py": b"", "shared.py": b""}, "what the project still holds"


# -- item 15: one reading of the directive lines --------------------------------------------

def test_the_batch_and_the_serial_loop_read_directives_through_one_function():
    import inspect
    serial, batch = inspect.getsource(Session._run_tasks), inspect.getsource(Session._run_batch)
    assert "self._read_directives(spec)" in serial and "self._read_directives(named, batch=True)" in batch
    for reader in ("_read_continues(", "_read_tier(", "_read_hypothesis(", "_read_covers(", "_read_resolves(",
                   "_covers_problem(", "_resolves_problem(", "_verification_handoff(", "_capture_problem("):
        assert reader not in serial and reader not in batch, f"{reader} has one caller, _read_directives"
        assert reader in inspect.getsource(Session._read_directives)


def test_tier_and_hypothesis_lines_are_read_off_a_parallel_block(tmp_path):
    """Before: both lines stayed in the text the child's lead recited, and a
    survey run never recorded the hypothesis."""
    batch = ("PARALLEL\n" + _block("a.py") + "\nTIER: normal\nHYPOTHESIS: the cap was too low, so split the work\n---\n"
             + _block("b.py"))
    script = Script([batch, "DONE"])
    session, _ = _session(tmp_path, script, survey=SurveyConfig(recovery_tasks=2))
    session.run(max_tasks=4)
    assert session.completed, session.stop_reason
    assert len(script.leads) == 2
    for _, _, prompt in script.leads:
        assert "TIER:" not in prompt and "HYPOTHESIS:" not in prompt
    assert session.survey["hypotheses"] == [dict(task="t1", after=None,
                                                 text="the cap was too low, so split the work")]
    assert [o.contract["required"]["tier"] for o in session.task_outcomes] == ["normal", "normal"]


def test_a_direct_tier_block_sends_the_batch_back_instead_of_dropping_the_label(tmp_path):
    direct = "PARALLEL\n" + _block("a.py") + "\nTIER: direct\n---\n" + _block("b.py")
    script = Script([direct, BATCH, "DONE"])
    session, project = _session(tmp_path, script, direct_tier=True)
    session.run(max_tasks=6)
    assert "--- BATCH SENT BACK ---\nt1: TIER: direct is not supported in a parallel batch" in script.decisions[1]
    assert "None of the batch ran; name the tasks again." in script.decisions[1]
    assert "COVERS" not in script.decisions[1].split("--- BATCH SENT BACK ---")[1].split("\n\n")[0], \
        "the COVERS reminder belongs to a COVERS problem"
    assert len(script.leads) == 2 and len(session.parallel_batches) == 1, "no lead ran for the batch sent back"
    assert [s.task_id for s in session.history] == ["t1", "t2"] and session.completed


def test_with_the_direct_tier_off_a_label_in_a_block_is_refused_and_recorded_as_a_serial_one_is(tmp_path):
    """The orchestrator was never offered the tier, so there is nothing to
    send back for: the line is read off, the task runs with review, and its
    contract says why the label was refused, exactly as for a task named alone."""
    direct = "PARALLEL\n" + _block("a.py") + "\nTIER: direct\n---\n" + _block("b.py")
    script = Script([direct, "DONE"])
    session, _ = _session(tmp_path, script)
    session.run(max_tasks=4)
    assert session.completed, session.stop_reason
    assert len(script.decisions) == 2 and "BATCH SENT BACK" not in script.decisions[1]
    assert all("TIER:" not in prompt for _, _, prompt in script.leads)
    required = [o.contract["required"] for o in session.task_outcomes]
    assert [r["tier"] for r in required] == ["normal", "normal"]
    assert [r["tier_refused"] for r in required] == ["the direct tier is not enabled on this run", ""]
    (tmp_path / "serial").mkdir()
    alone, _ = _session(tmp_path / "serial", Script([_block("a.py") + "\nTIER: direct", "DONE"]))
    alone.run(max_tasks=4)
    assert alone.task_outcomes[0].contract["required"]["tier_refused"] == required[0]["tier_refused"]


def test_a_continues_block_sends_the_batch_back_and_the_task_named_alone_gets_its_handoff(tmp_path):
    """Before: the child was run with no handoff, the capped task's debt was
    cleared on its word, and a run with a check then ended CompletionUnproven
    ('t1.checks unsatisfied') after the batch had been paid for."""
    capped = []

    def lead(root, target, prompt):
        if target == "a.py" and not capped:
            capped.append(root)
            (Path(root) / "a.py").write_text("# half\n")
            raise TurnLimitReached("capped", turns=14)
        return Script.write(root, target, prompt)
    finish = _block("a.py", "finish").replace("\nfinish", "\nCONTINUES: t1\nfinish")
    script = Script([_block("a.py"), "PARALLEL\n" + finish + "\n---\n" + _block("b.py"), finish, "DONE"], lead=lead)
    project = tmp_path / "project"
    project.mkdir()
    session, _ = _session(tmp_path, script, integration_gate=PassingGate(project))
    session.run(max_tasks=6)
    assert "--- BATCH SENT BACK ---\nt2: CONTINUES is not supported in a parallel batch" in script.decisions[2]
    assert session.parallel_batches == [] and script.roots == [], "no child was forked"
    assert [(s.task_id, s.outcome) for s in session.history] == [("t1", "turn_limited"), ("t2", "closed")]
    assert "## Handoff from t1" in script.leads[-1][2]
    assert session.task_outcomes[-1].continues == "t1", "the continuation is on the typed record"
    assert session.completed, session.stop_reason


def test_a_batch_block_is_offered_to_the_verification_handoff(tmp_path, monkeypatch):
    """The serial loop does not dispatch a task that only verifies what the
    harness has verified; a batch block is asked the same question."""
    asked = []

    def handoff(self, spec, covers, resolves, continues):
        asked.append((spec.task_id, list(covers), resolves, continues))
        if spec.task_id == "t2":
            return ("\n\n--- TASK NOT DISPATCHED ---\nThat task only verifies, and the harness has already "
                    "verified the delivered source. Reply DONE to have it judged.")
        return None
    monkeypatch.setattr(Session, "_verification_handoff", handoff)
    script = Script([BATCH, "DONE"])
    session, _ = _session(tmp_path, script)
    session.run(max_tasks=4)
    assert asked[:2] == [("t1", [], None, None), ("t2", [], None, None)]
    assert script.leads == [] and session.history == [], "nothing ran"
    assert "--- BATCH SENT BACK ---\nt2: That task only verifies" in script.decisions[1]
    assert "TASK NOT DISPATCHED" not in script.decisions[1], "one heading, the batch's"
    assert "None of the batch ran; name its other tasks again without that one." in script.decisions[1]
    assert session._next_task_id() == "t1", "a batch sent back spends no id"


@pytest.mark.requirements_ledger
def test_a_block_with_a_covers_problem_sends_the_batch_back_with_the_covers_reminder(tmp_path):
    """The single-task COVERS rule, through the same reading; the reminder
    about COVERS lines is this problem's and no other's."""
    reqs = "REQUIREMENTS:\nR1: a works\nR2: b works\n"
    missing = "PARALLEL\n" + _covering_block("a.py", "R1") + "\n---\n" + _block("b.py")
    unknown = "PARALLEL\n" + _covering_block("a.py", "R1") + "\n---\n" + _block("b.py") + "\nCOVERS: R9"
    good = "PARALLEL\n" + _covering_block("a.py", "R1") + "\n---\n" + _covering_block("b.py", "R2")
    script = Script([reqs + missing, unknown, good], audit=f"R1: MET - a.py\nR2: MET - b.py\nO1: {READ}\nO2: {READ}")
    session, project = _session(tmp_path, script, requirements_ledger=True)
    (project / "README.md").write_text("a works and b works.\n")
    session.requirement_reviews.append({"reviewer": "claude:opus", "result": "reviewed"})
    session.run(max_tasks=6)
    assert ("--- BATCH SENT BACK ---\nt2: The task has no COVERS line. Name the tasks again with COVERS "
            "lines using the listed requirement ids.") in script.decisions[1]
    assert "t2: COVERS names requirements that do not exist: R9." in script.decisions[2]
    assert len(script.leads) == 2 and [s.task_id for s in session.history] == ["t1", "t2"]
    assert session.completed, session.stop_reason


@pytest.mark.requirements_ledger
def test_a_serial_task_is_refused_by_the_same_reading_in_the_same_order(tmp_path):
    """The serial loop words and counts its own send-backs; which check
    refuses is the shared function's answer."""
    from quadratus.scope import TaskScope
    session, _ = _session(tmp_path, Script([]), requirements_ledger=True)
    session.memory.ledger.requirements = {"R1": "a works"}

    def read(text):
        return session._read_directives(TaskSpec("t1", text, scope=TaskScope(permitted_paths=["a.py"])))
    assert read("Work in a.py").refused == "covers"
    session._named_last = ()
    two = read("Work in a.py\nCOVERS: R1\nRESOLVES: F1\nRESOLVES: F2")
    assert two.refused == "resolves" and "more than one line" in two.problem
    session._named_last = ()
    clean = read("Work in a.py\nCOVERS: R1\nTIER: normal\nHYPOTHESIS: split it\nCONTINUES: t0")
    assert clean.refused == "" and clean.covers == ["R1"] and clean.continues == "t0"
    assert clean.resolves is None and clean.spec.description == "Work in a.py" and clean.spec.tier == "normal"


def test_a_block_that_repeats_last_rounds_task_stalls_like_a_serial_repeat(tmp_path):
    again = "PARALLEL\n" + SERIAL + "\n---\n" + _block("b.py")
    script = Script([SERIAL, again, "DONE"])
    session, _ = _session(tmp_path, script)
    with pytest.raises(RunStalled, match="named the same task twice in a row"):
        session.run(max_tasks=6)
    assert [s.task_id for s in session.history] == ["t1"] and len(script.leads) == 1


def test_a_task_or_batch_that_repeats_a_block_of_the_last_batch_stalls(tmp_path):
    script = Script([BATCH, _block("b.py"), "DONE"])
    session, _ = _session(tmp_path, script)
    with pytest.raises(RunStalled, match="named the same task twice in a row"):
        session.run(max_tasks=6)
    assert [s.task_id for s in session.history] == ["t1", "t2"]
    script = Script([BATCH, BATCH, "DONE"])
    (tmp_path / "second").mkdir()
    session, _ = _session(tmp_path / "second", script)
    with pytest.raises(RunStalled, match="named the same task twice in a row"):
        session.run(max_tasks=6)
    assert len(session.parallel_batches) == 1


def test_a_batch_sent_back_and_named_again_is_a_correction_not_a_repeat(tmp_path):
    """As for a serial task: the send-back clears what a repeat is measured
    against, so the corrected batch may reuse a block word for word."""
    bad = "PARALLEL\n" + _block("a.py") + "\n---\n" + _block("b.py") + "\nTIER: direct"
    script = Script([SERIAL, bad, BATCH, "DONE"])
    session, _ = _session(tmp_path, script, direct_tier=True)
    session.run(max_tasks=6)
    assert "--- BATCH SENT BACK ---\nt3: TIER: direct" in script.decisions[2]
    assert [s.task_id for s in session.history] == ["t1", "t2", "t3"] and session.completed


def test_a_block_the_task_record_refuses_falls_back_like_any_invalid_block(tmp_path):
    """Before: an unknown NEEDS in a block raised a bare ValueError out of
    next_task; a serial task with the same line is a named stall."""
    notes = []
    bad = "PARALLEL\n" + _block("a.py") + '\nNEEDS: ["teleport"]\n---\n' + _block("b.py")
    session, _ = _session(tmp_path, Script([bad]), progress=notes.append)
    try:
        session.next_task()         # a bare ValueError here is the defect
    except RunStalled as stalled:
        assert "task" in str(stalled).lower()
    assert any("parallel batch not run: block 1 is not a valid task" in n for n in notes)
    assert session._batch == [], "no part of the batch was queued"


def test_the_orchestrator_is_told_what_a_batch_refuses():
    text = S._parallel_request(3)
    assert "so is a task that CONTINUES another" in text
    assert "sent back whole and none of it runs" in text
    assert "a block refused under one of them sends the whole batch back" in text
    assert ("A block that repeats a task named in the previous round, or a task that repeats a block of "
            "the previous round's batch, stops the run.") in text
    # The tier is opt-in and its rules are stated only when it is on
    # (tests/lifecycle/test_direct_tier.py), so its batch rule rides with them.
    assert "TIER" not in text
    tier = S._tier_request(40)
    assert "A direct task is named alone" in tier and "a batch holding the label is sent back whole" in tier
    source = __import__("inspect").getsource(Session._read_directives)
    for refusal in ("RESOLVES is not supported in a parallel batch", "CONTINUES is not supported in a parallel batch",
                    "TIER: direct is not supported in a parallel batch",
                    "A UI task with harness capture is not supported in a parallel batch"):
        assert refusal in source
