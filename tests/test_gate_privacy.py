"""Seats never see a gate's command or the paths it names (Q9-v2 integrity finding).

In the native Q9-v2 runs the grader's absolute path reached every lead through
the role packet's gate list and the gate result text, and seven baseline leads
opened the grader. The operator's records keep the full command.
"""

import json
import sys
from pathlib import Path

from quadratus.cli_providers import CLIProvider
from quadratus.config import Settings
from quadratus.integration import GateReceipt, GateResult
from quadratus.project_run import run_project


def test_the_model_view_names_the_gate_and_redacts_command_paths():
    secret = "/private/tmp/examiner-7/grader.py"
    receipt = GateReceipt(id="api-tests", status="failed", reason="nonzero exit", required=True,
                          command=f"python -m pytest {secret} -k preservation", returncode=1,
                          output=f"{secret}:82: AssertionError", tests=5)
    result = GateResult(False, "gate suite", 1,
                        f"api-tests: failed\n{secret}:82: AssertionError near /private/tmp/examiner-7/x.py",
                        (receipt,))
    view = result.for_models()
    assert "examiner-7" not in view and "grader.py" not in view
    assert "api-tests: failed: nonzero exit (5 tests)" in view
    assert "AssertionError" in view, "the failure itself still reaches the lead"
    assert secret in result.render(), "the operator's view keeps the command"


def test_no_prompt_ever_carries_the_gate_command(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.md").write_text("old\n")
    hidden = tmp_path / "hidden-examiner"
    hidden.mkdir()
    check = hidden / "check_heading.py"
    check.write_text("import pathlib, sys\n"
                     "ok = pathlib.Path('a.md').read_text() == '# Hello\\n'\n"
                     "print('checked a.md with', __file__)\n"
                     "sys.exit(0 if ok else 1)\n")
    monkeypatch.setattr(CLIProvider, "available", lambda _: True)
    prompts, systems, native_argv, plan = [], [], [], ["KIND: docs simple\nSCOPE: " + json.dumps({
        "permitted_paths": ["a.md"], "intended_result": "Heading reads Hello",
        "acceptance": ["Heading reads Hello"], "max_lines": 10}) + "\nCorrect the heading in a.md."]

    def call(self, prompt, system, history):
        prompts.append(prompt)
        systems.append(system)
        native_argv.append(self._build_argv(prompt, system))
        self.last_usage = {"input_tokens": 1, "output_tokens": 1}
        if "Name the single next task" in prompt:
            return plan.pop(0) if plan else "DONE"
        if "The task cap for this run has been reached" in prompt:
            return "DONE"
        if "You are leading" in prompt or "integration check failed" in prompt:
            edits = sum(1 for p in prompts if "You are leading" in p or "integration check failed" in p)
            Path(self.workdir, "a.md").write_text("# Hello\n" if edits > 1 else "# Hel\n")
            return 'Heading edited\nCHANGED: ["a.md"]'
        if "The task is finished" in prompt:
            return "SUMMARY: heading corrected\nREASONING: gate passed on the fix round"
        return "RESOLVED"

    monkeypatch.setattr(CLIProvider, "_call", call)
    result = run_project("Correct heading", project, Settings(backend="cli"), allow_writes=True,
                         check=f"{sys.executable} {check}", max_tasks=3)
    assert any("integration check failed" in p for p in prompts), "the fix round happened"
    leaked = [p[:160] for p in prompts if str(hidden) in p or "check_heading" in p]
    assert not leaked, leaked
    assert str(hidden) not in json.dumps(systems)
    assert 'check_heading' not in json.dumps(systems)
    assert str(hidden) not in json.dumps(native_argv)
    assert 'check_heading' not in json.dumps(native_argv)
    data = json.loads((result.run_dir / "result.json").read_text())
    assert any(str(check) in json.dumps(c) for c in data["checks"]), "the operator record keeps the command"
    assert (project / "a.md").read_text() == "# Hello\n"
