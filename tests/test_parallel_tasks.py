"""Independent tasks run at once, each in its own copy, then merge (Davis, 2026-09-25)."""

import json
import threading
import time
from pathlib import Path

from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig, _parallel_blocks


def _block(path, text="work"):
    scope = json.dumps({"permitted_paths": [path], "intended_result": f"{path} written",
                        "acceptance": [f"{path} exists"], "max_lines": 20})
    return f"KIND: backend simple\nSCOPE: {scope}\n{text} in {path}"


BATCH = "PARALLEL\n" + _block("a.py") + "\n---\n" + _block("b.py")


class Orchestrated:
    """Scripted orchestrator + leads. Leads write the file their task names,
    in whichever directory their fork is bound to, and record overlap."""

    def __init__(self, plan, *, lead_delay=0.3, rogue=None):
        self.plan = list(plan)
        self.lead_delay = lead_delay
        self.rogue = rogue
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.roots = []

    def invoke_for(self, root):
        def invoke(model, prompt, system=None, allow_writes=False):
            if "Name the single next task" in prompt:
                return self.plan.pop(0) if self.plan else "DONE"
            if "The task is finished" in prompt:
                return "SUMMARY: done\nREASONING: done"
            if "You are leading" in prompt:
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(self.lead_delay)
                target = "a.py" if " in a.py" in prompt else "b.py"
                (Path(root) / target).write_text(f"# {target} by {model}\n")
                if self.rogue and target == self.rogue:
                    (Path(root) / "shared.py").write_text("# not in scope\n")
                with self.lock:
                    self.active -= 1
                return f'Wrote it\nCHANGED: ["{target}"]'
            return "NO FINDINGS"
        return invoke


def _session(tmp_path, script, **config):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "keep.py").write_text("# existing\n")

    def fork(root):
        script.roots.append(root)
        return script.invoke_for(root), (lambda: None)

    return Session("Build two things", ArtifactStore(tmp_path / "artifacts"), script.invoke_for(project),
                   config=SessionConfig(project=project, allow_writes=True, fork=fork, **config)), project


def test_the_batch_is_split_into_blocks():
    assert len(_parallel_blocks(BATCH)) == 2
    assert _parallel_blocks("KIND: backend simple\nwork") is None


def test_two_independent_tasks_run_at_once_and_merge(tmp_path):
    script = Orchestrated([BATCH, "DONE"])
    session, project = _session(tmp_path, script)
    session.run(max_tasks=4)
    assert script.max_active == 2, "both leads were working at the same time"
    assert (project / "a.py").exists() and (project / "b.py").exists()
    assert (project / "keep.py").read_text() == "# existing\n"
    assert [s.task_id for s in session.history] == ["t1", "t2"]
    batch = session.parallel_batches[0]
    assert batch["tasks"] == ["t1", "t2"] and len({lead.partition(":")[0] for lead in batch["leads"]}) == 2
    assert all(root != project for root in script.roots)
    assert session.completed


def test_a_change_outside_a_tasks_scope_is_not_merged(tmp_path):
    script = Orchestrated([BATCH, "DONE"], rogue="b.py")
    session, project = _session(tmp_path, script)
    session.run(max_tasks=4)
    assert (project / "a.py").exists()
    assert not (project / "b.py").exists() and not (project / "shared.py").exists()
    assert any("t2 was not merged" in f for f in session.open_findings)
    assert not session.completed


def test_overlapping_files_fall_back_to_one_task_at_a_time(tmp_path):
    overlapping = "PARALLEL\n" + _block("a.py") + "\n---\n" + _block("a.py", "more")
    script = Orchestrated([overlapping, "DONE"])
    notes = []
    session, project = _session(tmp_path, script, progress=notes.append)
    session.run(max_tasks=4)
    assert any("files shared between tasks: a.py" in n for n in notes)
    assert script.max_active == 1 and not session.parallel_batches


def test_without_a_fork_batches_are_not_offered(tmp_path):
    script = Orchestrated(["KIND: backend simple\n" + _block("a.py").split("\n", 1)[1], "DONE"])
    project = tmp_path / "project"
    project.mkdir()
    session = Session("g", ArtifactStore(tmp_path / "a"), script.invoke_for(project),
                      config=SessionConfig(project=project, allow_writes=True))
    assert not session._parallel_enabled()


def test_a_batch_spends_one_task_slot_per_task(tmp_path):
    """Codex review: a 2-task batch under max_tasks=1 must not run two tasks."""
    script = Orchestrated([BATCH, "DONE"])
    notes = []
    session, project = _session(tmp_path, script, progress=notes.append)
    session.run(max_tasks=1)
    assert len(session.history) == 1 and not session.parallel_batches
    assert any("exceeds the 1 task slots left" in n for n in notes)
    script = Orchestrated([BATCH, "DONE"])
    (tmp_path / "second").mkdir()
    session, project = _session(tmp_path / "second", script)
    session.run(max_tasks=2)
    assert len(session.history) == 2 and session.parallel_batches
