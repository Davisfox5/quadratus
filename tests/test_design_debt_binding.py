"""Design debt names only its own task's stop (phase 3, map G8).

The serial loop stops right after any task that leaves design debt, so an
earlier task's entry cannot name a later stop there today; the direct
controls defend that invariant rather than change a reachable route. The
reachable change is the parallel batch: a child's design debt used to stay
in the child, so the parent's stop was the generic FindingsOpen."""

import json

from quadratus.artifacts import ArtifactStore
from quadratus.session import Session
from tests.test_parallel_tasks import Orchestrated
from tests.test_parallel_tasks import _session as parallel_session


def _session(tmp_path):
    return Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE")


def test_another_tasks_design_debt_never_names_this_stop(tmp_path):
    session = _session(tmp_path)
    session._design_unverified = [("t1", "the renders are stale")]
    session.open_findings = ["Task t2: something else is open"]
    session._name_findings_stop({"t2"})
    assert not session.stop_reason.startswith("DesignUnverified"), session.stop_reason
    assert session.stop_reason.startswith("FindingsOpen: ")


def test_a_tasks_own_design_debt_names_its_stop(tmp_path):
    session = _session(tmp_path)
    session._design_unverified = [("t1", "the renders are stale"), ("t2", "no mobile render")]
    session.open_findings = ["Task t2 is design work without clean rendered evidence"]
    session._name_findings_stop({"t2"})
    assert session.stop_reason == ("DesignUnverified: task t2 is design work without clean rendered evidence: "
                                   "no mobile render. Work preserved.")


def _design_block(path):
    scope = json.dumps({"permitted_paths": [path], "intended_result": f"{path} shows the button",
                        "acceptance": ["the page shows a button"], "max_lines": 20})
    return f"KIND: frontend simple\nSCOPE: {scope}\nAdd a button in {path}"


class DesignOrchestrated(Orchestrated):
    def invoke_for(self, root):
        from pathlib import Path

        def invoke(model, prompt, system=None, allow_writes=False):
            if "Name the single next task" in prompt:
                return self.plan.pop(0) if self.plan else "DONE"
            if "The task is finished" in prompt:
                return "SUMMARY: done\nREASONING: done"
            if "You are leading" in prompt:
                target = "templates/a.html" if "templates/a.html" in prompt else "templates/b.html"
                (Path(root) / target).parent.mkdir(exist_ok=True)
                (Path(root) / target).write_text("<button>go</button>\n")
                return f'Wrote it\nCHANGED: ["{target}"]'
            if "contributing an independent read" in prompt:
                return "NO FINDINGS"
            return "Looked.\nCHANGED: []"
        return invoke


def test_a_parallel_childs_design_debt_names_the_parents_stop(tmp_path):
    batch = "PARALLEL\n" + _design_block("templates/a.html") + "\n---\n" + _design_block("templates/b.html")
    script = DesignOrchestrated([batch, "DONE"], lead_delay=0)
    session, _ = parallel_session(tmp_path, script)
    session.run(max_tasks=4)
    assert not session.completed
    assert session.stop_reason.startswith("DesignUnverified: task t"), session.stop_reason
    assert "is design work without clean rendered evidence" in session.stop_reason
