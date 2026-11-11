"""Regressions for the 2026-11-10 whole-repo review: the command-line and GUI wiring.

The fix workstreams added their options as keyword parameters and environment
settings and left the flags and form fields to this step. Each test here
holds one piece of that wiring to the same three things: the value reaches
the shared runner (or the monitor server), a line or a form that leaves it
alone makes the call it made before, and a flag given where it would be
ignored is refused by name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from quadratus import (
    child_env,
    cli,
    decisions,
    gui,
    isolated_run,
    monitor_server,
    policy,
    project_run,
    project_watch,
    runtime,
)
from quadratus.config import Settings
from quadratus.registry import MODE_ROSTERS, mode_audit_gap
from quadratus.run_budget import (
    LIMIT_FIELDS,
    LIMIT_LABELS,
    RunBudget,
    RunBudgetExceeded,
    RunLimits,
    limits_from_fields,
    whole_number,
)
from quadratus.session import Session, SessionConfig
from quadratus.usage import UsageMeter

GOAL = "Fix the bug"
DIGEST = "ab" * 32
REPO = Path(__file__).resolve().parents[1]
TRUST_KEYS = {"trust_project_policy", "allow_project_agent_config", "declared_checks"}


@pytest.fixture(autouse=True)
def _no_standing_settings(monkeypatch):
    """The settings of whoever runs the suite must not decide these tests."""
    for name in (policy.TRUST_ENV, project_watch.ALLOW_AGENT_CONFIG_ENV, monitor_server.ALLOW_HOST_ENV,
                 monitor_server.ALLOW_PROJECT_ENV, child_env.PASS_VARIABLE):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def runner(monkeypatch, tmp_path):
    """Stand in for the shared runner and record what the command line sent it."""
    seen = {}

    def run(goal, project, settings, **kwargs):
        seen.update(kwargs, goal=goal, project=project, settings=settings)
        return SimpleNamespace(completed=True, report="checked", run_dir=tmp_path)

    monkeypatch.setattr(project_run, "run_project", run)
    return seen


def _form_runner(monkeypatch, tmp_path):
    """The same stand-in for the Project form, which wants a report and a diff."""
    seen = {}

    def run(goal, folder, settings, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(report="report", diff="", run_dir=tmp_path)

    monkeypatch.setattr(project_run, "run_project", run)
    return seen


def _refused(argv, capsys, monkeypatch):
    """The argparse refusal for ``argv``: exit 2, nothing configured or started."""
    monkeypatch.setattr(cli, "_build_settings",
                        lambda *a: pytest.fail("refused lines must not reach settings or providers"))
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2
    return capsys.readouterr().err


def _flat_help(monkeypatch):
    # Wide, so argparse wraps nothing and breaks no hyphenated name.
    monkeypatch.setenv("COLUMNS", "100000")
    return " ".join(cli.build_parser().format_help().split())


def _callback(demo, name):
    for block in demo.fns.values():
        if getattr(block.fn, "__name__", None) == name:
            return block
    raise AssertionError(f"no {name} callback")


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def _inside(block, section) -> bool:
    """Whether ``block`` sits anywhere under ``section`` in the page."""
    while block is not None:
        if block is section:
            return True
        block = getattr(block, "parent", None)
    return False


# A policy file that grants something: a command gate (the trust workstream's fixture).

MARKER = "import pathlib;pathlib.Path('ran.txt').write_text('x');print('1 passed')"


def _command_policy(root):
    doc = {
        "kind": "policy", "schema_version": 1, "repository_id": "fixture/project",
        "library": {"version": "pilot-1"},
        "defaults": {"family": "pure-logic", "required_gates": ["scope"],
                     "scope_max_lines": 50, "overrun_ratio": 1.1, "worker_depth": 1},
        "capability_policy": {"deny_write": [], "sensitive": [],
                              "external_effects": "deny", "native_delegation": "deny"},
        "path_rules": [], "gate_bindings": {},
        "gates": [{"id": "scope", "runner": "builtin:scope", "required": True},
                  {"id": "unit-tests", "runner": "command", "required": True,
                   "argv": [sys.executable, "-c" + MARKER], "minimum_tests": 1}],
        "decisions": [],
    }
    path = Path(root) / ".quadratus/policy.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(doc))
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- 1. trust flags on the command line -----------------------------------------------------------


def test_the_trust_flags_reach_the_shared_runner(tmp_path, runner):
    assert cli.main([GOAL, "--project", str(tmp_path), "--trust-project-policy", "SHA256:" + DIGEST.upper(),
                     "--allow-project-agent-config", "--no-declared-checks"]) == 0
    assert runner["trust_project_policy"] == DIGEST, "one canonical spelling, as the loader reads it"
    assert runner["allow_project_agent_config"] is True and runner["declared_checks"] is False


def test_a_line_without_trust_flags_is_the_call_made_before(tmp_path, runner):
    assert cli.main([GOAL, "--project", str(tmp_path)]) == 0
    assert not TRUST_KEYS & set(runner), "nothing new is passed, so the environment settings still decide"
    assert cli._trust(cli.build_parser().parse_args([])) == {}


@pytest.mark.parametrize("flag", [["--trust-project-policy", DIGEST], ["--allow-project-agent-config"],
                                  ["--no-declared-checks"]])
def test_a_trust_flag_is_refused_where_no_project_run_would_read_it(flag, tmp_path, capsys, monkeypatch):
    for engine in ([], ["--session"]):
        assert f"{flag[0]} require --project" in _refused([GOAL, *engine, *flag], capsys, monkeypatch)
    error = _refused(["--monitor", "--project", str(tmp_path), *flag], capsys, monkeypatch)
    assert "--monitor reads a run and starts nothing" in error and flag[0] in error


@pytest.mark.parametrize("bad", ["yes", "abc123", "sha256:", DIGEST[:-1], DIGEST + "0", "", "g" * 64])
def test_a_value_that_is_not_a_sha256_is_refused_by_flag(bad, tmp_path, capsys, monkeypatch, runner):
    """A project with no policy file never reads the approval, so a mistyped
    one would otherwise be accepted and passed over."""
    error = _refused([GOAL, "--project", str(tmp_path), "--trust-project-policy", bad], capsys, monkeypatch)
    assert "--trust-project-policy" in error and "64 hexadecimal characters" in error
    assert not runner


def test_the_policy_refusal_prints_the_flag_that_approves_it(tmp_path, capsys):
    """The message has named ``--trust-project-policy`` since the trust change;
    the words it prints are now a line the command line takes."""
    digest = _command_policy(tmp_path)
    line = ["--project", str(tmp_path), "--policy-preview", "--path", "app.py"]
    assert cli.main(line) == 1
    refusal = capsys.readouterr().out
    offered = re.search(r"--trust-project-policy [0-9a-f]{64}", refusal)
    assert refusal.startswith("Error: ") and offered and offered[0].split()[1] == digest
    cli.main([*line, *offered[0].split()])
    plan = json.loads(capsys.readouterr().out)
    assert plan["policy_approved"] is True and plan["policy_sha256"] == digest
    # An approval of other bytes is not an approval of these.
    assert cli.main([*line, "--trust-project-policy", "0" * 64]) == 1
    assert "is for different bytes" in capsys.readouterr().out
    assert not (tmp_path / "ran.txt").exists(), "a preview runs no check"


def test_the_preview_is_given_the_approval_only_when_one_was_typed(tmp_path, monkeypatch, capsys):
    calls = []

    def preview(root, paths=(), **kwargs):
        calls.append(kwargs)
        return {"blocked": []}

    monkeypatch.setattr(policy, "preview_policy", preview)
    assert cli.main(["--project", str(tmp_path), "--policy-preview"]) == 0
    assert cli.main(["--project", str(tmp_path), "--policy-preview", "--trust-project-policy", DIGEST]) == 0
    assert "trust_project_policy" not in calls[0] and calls[1]["trust_project_policy"] == DIGEST


def test_the_preview_is_told_declared_checks_are_declined_only_when_they_are(tmp_path, monkeypatch, capsys):
    calls = []

    def preview(root, paths=(), **kwargs):
        calls.append(kwargs)
        return {"blocked": []}

    monkeypatch.setattr(policy, "preview_policy", preview)
    assert cli.main(["--project", str(tmp_path), "--policy-preview"]) == 0
    assert cli.main(["--project", str(tmp_path), "--policy-preview", "--no-declared-checks"]) == 0
    assert "declared_checks" not in calls[0] and calls[1]["declared_checks"] is False


def test_the_preview_of_a_run_without_declared_checks_is_that_runs_plan(tmp_path, monkeypatch, capsys):
    """``--policy-preview --no-declared-checks`` took the flag and listed the
    project's own check all the same, a check the run it previews never runs."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    line = ["--project", str(tmp_path), "--policy-preview"]

    def previewed(*flags):
        assert cli.main([*line, *flags]) == 0
        return json.loads(capsys.readouterr().out)

    usual, declined = previewed(), previewed("--no-declared-checks")
    assert [gate["id"] for gate in usual["gates"]] == ["scope", "unit-tests"]
    assert [gate["id"] for gate in declined["gates"]] == ["scope"]
    assert [gate["id"] for gate in declined["absent_gates"]] == ["unit-tests"]
    assert policy.preview_policy(tmp_path) == usual, "left out, the preview is the one made before"
    assert policy.preview_policy(tmp_path, declared_checks=False) == declined
    # The run the second line previews resolves the same plan.
    monkeypatch.setattr(Session, "run", lambda self, **kw: None)
    cli.main([GOAL, "--project", str(tmp_path), "--no-declared-checks"])
    capsys.readouterr()
    run_dir = next((tmp_path / ".quadratus" / "runs").iterdir())
    assert json.loads((run_dir / "policy-plan.json").read_text())["hash"] == declined["hash"] != usual["hash"]
    with pytest.raises(policy.PolicyError, match="Project folder does not exist"):
        policy.preview_policy(tmp_path / "missing", declared_checks=False)


def test_the_agent_config_refusal_prints_the_flag_that_allows_it(tmp_path, monkeypatch, capsys):
    (tmp_path / "app.py").write_text("x\n")
    (tmp_path / ".mcp.json").write_text("{}")
    ran = []
    monkeypatch.setattr(Session, "run", lambda self, **kw: ran.append(1))
    assert cli.main([GOAL, "--project", str(tmp_path)]) == 1
    refusal = capsys.readouterr().out
    assert "Error: The project carries vendor agent configuration: .mcp.json" in refusal
    assert "--allow-project-agent-config" in refusal and not ran
    cli.main([GOAL, "--project", str(tmp_path), "--allow-project-agent-config"])
    assert ran == [1], "the flag the refusal names starts the run"


def test_no_declared_checks_leaves_only_the_operators_own(tmp_path, monkeypatch, capsys):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    monkeypatch.setattr(Session, "run", lambda self, **kw: None)

    def plan(*flags):
        cli.main([GOAL, "--project", str(tmp_path), *flags])
        capsys.readouterr()
        newest = max((tmp_path / ".quadratus" / "runs").iterdir(), key=lambda p: p.stat().st_mtime_ns)
        # The commands: a declined run's plan ends with one entry that names
        # the choice and is not a command (tests/test_review1110_glue2.py).
        return [gate["argv"] for gate in json.loads((newest / "gate-plan.json").read_text())
                if not gate.get("declined")]

    assert any(argv[1:3] == ["-m", "pytest"] for argv in plan()), "the project's own check is run by default"
    assert plan("--no-declared-checks") == []
    assert plan("--no-declared-checks", "--check", "true") == [["true"]]


def test_the_trust_help_names_each_setting_it_stands_for(monkeypatch):
    assert cli._TRUST_ENV == policy.TRUST_ENV and cli._AGENT_CONFIG_ENV == project_watch.ALLOW_AGENT_CONFIG_ENV
    flat = _flat_help(monkeypatch)
    assert f"Same as {policy.TRUST_ENV}." in flat
    assert f"Same as {project_watch.ALLOW_AGENT_CONFIG_ENV}=1." in flat
    assert "nothing the project declares is added beside them or run in their place" in flat


# -- 1. the same three inputs on the Project form ---------------------------------------------------


def test_the_form_runner_passes_the_trust_choices_and_nothing_when_left_alone(tmp_path, monkeypatch):
    seen = _form_runner(monkeypatch, tmp_path)
    list(gui.run_project_ui("goal", str(tmp_path), True, "", "adversarial", 3, Settings(),
                            trust_project_policy=f" {DIGEST} ", allow_project_agent_config=True,
                            declared_checks=False))
    assert seen["trust_project_policy"] == DIGEST
    assert seen["allow_project_agent_config"] is True and seen["declared_checks"] is False
    seen.clear()
    list(gui.run_project_ui("goal", str(tmp_path), True, "", "adversarial", 3, Settings(),
                            trust_project_policy="  "))
    assert seen and not TRUST_KEYS & set(seen), "a blank field is not an approval of nothing"


def test_the_project_tab_has_one_collapsed_trust_section_with_a_line_for_each_input():
    pytest.importorskip("gradio")
    demo = gui.build_interface(Settings())
    blocks = list(demo.blocks.values())
    sections = [b for b in blocks if type(b).__name__ == "Accordion" and getattr(b, "label", None) == "Trust"]
    assert len(sections) == 1 and sections[0].open is False
    by_label = {getattr(b, "label", None): b for b in blocks}
    assert set(gui.TRUST_LABELS) == set(gui.TRUST_ALLOWS)
    for key, label in gui.TRUST_LABELS.items():
        field = by_label[label]
        assert _inside(field, sections[0]), label
        assert field.info == gui.TRUST_ALLOWS[key] and field.info.startswith("Allows "), label
    flags = {name for action in cli.build_parser().flags for name in action.option_strings}
    for key, label in gui.TRUST_LABELS.items():
        assert "--" + key.replace("_", "-") in flags and "--" + key.replace("_", "-") in label
    assert type(by_label[gui.TRUST_LABELS["trust_project_policy"]]).__name__ == "Textbox"
    for key in ("allow_project_agent_config", "no_declared_checks"):
        assert type(by_label[gui.TRUST_LABELS[key]]).__name__ == "Checkbox"
        assert by_label[gui.TRUST_LABELS[key]].value is False, "nothing is trusted until the operator says so"


def test_the_real_run_button_carries_the_trust_section_to_the_runner(tmp_path, monkeypatch):
    pytest.importorskip("gradio")
    import gradio as gr
    seen = _form_runner(monkeypatch, tmp_path)
    demo = gui.build_interface(Settings())
    block = _callback(demo, "run_selected")
    # The Trust section's inputs (four since the watched-paths box), then the Contests choice.
    assert [getattr(b, "label", None) for b in block.inputs][-5:] == [*gui.TRUST_LABELS.values(), gui.CONTEST_LABEL]
    # goal, folder, writes, check, mode, tasks, paths, forbid, repairs, neutral, profile, extra, probes
    common = ("goal", str(tmp_path), True, "", "adversarial", 10, "", "", "", False, "", "", "")
    limits = (False, 24, 500_000, 900, 2, 0, 0, 0)
    list(block.fn(*common, *limits, "sha256:" + DIGEST.upper(), True, True))
    assert seen["trust_project_policy"] == DIGEST
    assert seen["allow_project_agent_config"] is True and seen["declared_checks"] is False
    seen.clear()
    list(block.fn(*common, *limits, "", False, False))
    assert seen and not TRUST_KEYS & set(seen), "the section left alone is the call made before it existed"
    seen.clear()
    # A caller written before the section existed stops after the limits and chose nothing.
    list(block.fn(*common, *limits))
    assert seen and not TRUST_KEYS & set(seen)
    seen.clear()
    list(block.fn(*common, *limits, DIGEST))
    assert TRUST_KEYS & set(seen) == {"trust_project_policy"} and seen["trust_project_policy"] == DIGEST
    seen.clear()
    with pytest.raises(gr.Error, match="Approve this project's policy file") as refused:
        list(block.fn(*common, *limits, "not a hash", False, False))
    assert "64 hexadecimal characters" in str(refused.value) and seen == {}, "refused before the runner"


def test_opening_a_project_clears_the_trust_section(tmp_path, monkeypatch):
    """A trust choice is for the project that is open. The section is folded
    shut, so a box ticked for one project stayed ticked, unseen, for the next
    one opened or cloned in the same form."""
    pytest.importorskip("gradio")
    seen = _form_runner(monkeypatch, tmp_path)
    demo = gui.build_interface(Settings())
    opened, cleared = _callback(demo, "open_project"), _callback(demo, "clear_trust")
    assert cleared.trigger_after == opened._id and cleared.trigger_only_on_success is True
    assert [getattr(b, "label", None) for b in cleared.outputs] == list(gui.TRUST_LABELS.values())
    assert not cleared.inputs
    values = cleared.fn()
    assert values == ("", False, False, False)
    # What the section holds after that is the form that chose nothing.
    common = ("goal", str(tmp_path), True, "", "adversarial", 10, "", "", "", False, "", "", "")
    list(_callback(demo, "run_selected").fn(*common, False, 24, 500_000, 900, 2, 0, 0, 0, *values))
    assert seen and not TRUST_KEYS & set(seen)
    notes = [getattr(b, "value", "") for b in demo.blocks.values() if type(b).__name__ == "Markdown"]
    assert any("opening a project clears this section" in str(note) for note in notes), "and the form says so"


def test_the_form_preview_passes_the_approval(tmp_path):
    digest = _command_policy(tmp_path)
    refusal = gui.policy_preview_ui(str(tmp_path), "app.py", "", False)
    assert refusal.startswith("Policy preview failed: ") and f"--trust-project-policy {digest}" in refusal
    assert "Plan hash" in gui.policy_preview_ui(str(tmp_path), "app.py", "", False, digest.upper())
    mistyped = gui.policy_preview_ui(str(tmp_path), "app.py", "", False, "yes")
    assert mistyped.startswith("Policy preview failed: Approve this project's policy file")
    assert not (tmp_path / "ran.txt").exists()


def test_the_preview_button_reads_the_trust_fields_a_preview_can_honour():
    pytest.importorskip("gradio")
    block = _callback(gui.build_interface(Settings()), "policy_preview_ui")
    assert [getattr(b, "label", None) for b in block.inputs][-2:] == [
        gui.TRUST_LABELS["trust_project_policy"], gui.TRUST_LABELS["no_declared_checks"]]


def test_the_form_preview_leaves_out_the_checks_the_run_would_decline(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    assert "Checks to run: scope, unit-tests" in gui.policy_preview_ui(str(tmp_path), "", "", False, "")
    declined = gui.policy_preview_ui(str(tmp_path), "", "", False, "", True)
    assert "Checks to run: scope\n" in declined and "Not configured: unit-tests" in declined
    calls = []

    def preview(root, paths=(), **kwargs):
        calls.append(kwargs)
        return {}  # never rendered: the call is what is read

    monkeypatch.setattr(policy, "preview_policy", preview)
    monkeypatch.setattr(policy, "render_preview", lambda plan: "rendered")
    assert gui.policy_preview_ui(str(tmp_path), "", "", False, "", False) == "rendered"
    assert gui.policy_preview_ui(str(tmp_path), "", "", False, DIGEST, True) == "rendered"
    assert "declared_checks" not in calls[0] and "trust_project_policy" not in calls[0]
    assert calls[1]["declared_checks"] is False and calls[1]["trust_project_policy"] == DIGEST


def test_one_reading_of_an_approvals_spelling(monkeypatch):
    """The loader, the flag and the form field read an approval by one rule."""
    assert policy.approval_digest(" SHA256:" + DIGEST.upper() + " ") == DIGEST == cli._policy_hash(DIGEST)
    assert policy.approval_digest("") == policy.approval_digest(None) == "" == gui.policy_approval("  ")
    assert gui.policy_approval("sha256:" + DIGEST) == DIGEST
    with pytest.raises(policy.PolicyError, match="^the field must be the policy file's sha256"):
        policy.approval_digest("yes", "the field")
    # The helper never reads the environment; the loader's own reading still does.
    monkeypatch.setenv(policy.TRUST_ENV, DIGEST)
    assert policy.approval_digest("") == "" and policy._approval("") == DIGEST


# -- 2. monitor flags -------------------------------------------------------------------------------


@pytest.fixture
def served(monkeypatch):
    seen = {}

    def serve(port, **kwargs):
        seen.update(kwargs, port=port)
        return 0

    monkeypatch.setattr(monitor_server, "serve", serve)
    return seen


def test_the_monitor_flags_reach_the_server(tmp_path, served):
    assert cli.main(["--monitor", "--serve", "--project", str(tmp_path),
                     "--allow-host", "mac.example.ts.net", "--allow-host", "tunnel.example.com:8443",
                     "--allow-project", "/abs/a", "--allow-project", "/abs/b,c"]) == 0
    assert served["allow_hosts"] == ("mac.example.ts.net", "tunnel.example.com:8443")
    assert served["allow_projects"] == ("/abs/a", "/abs/b,c"), "a folder with a comma, which the setting cannot name"
    assert served["port"] == 7861 and served["project"] == str(tmp_path)


def test_a_served_monitor_without_them_is_the_call_made_before(tmp_path, served):
    assert cli.main(["--monitor", "--serve", "8123", "--project", str(tmp_path), "--history", "3"]) == 0
    assert served == dict(port=8123, project=str(tmp_path), series=None, state_dir=None, history=3)


@pytest.mark.parametrize("flag", [["--allow-host", "mac.example.ts.net"], ["--allow-project", "/abs/a"]])
def test_a_monitor_flag_is_refused_where_nothing_is_served(flag, tmp_path, capsys, monkeypatch, served):
    error = _refused(["--monitor", "--project", str(tmp_path), *flag], capsys, monkeypatch)
    assert f"{flag[0]} require --serve" in error
    for line in ([GOAL, *flag], [GOAL, "--project", str(tmp_path), *flag], ["--session", GOAL, *flag]):
        assert f"{flag[0]} can only be used with --monitor" in _refused(line, capsys, monkeypatch)
    assert not served


def test_a_bad_host_flag_stops_the_monitor_and_is_reported_under_the_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(monitor_server, "MonitorServer",
                        lambda *a, **kw: pytest.fail("nothing is served with a bad host entry"))
    assert cli.main(["--monitor", "--serve", "--project", str(tmp_path),
                     "--allow-host", "https://mac.example.ts.net/"]) == 2
    assert capsys.readouterr().out.strip() == (
        "Error: --allow-host: 'https://mac.example.ts.net/' is not a host name. "
        "Give the name alone, such as mac.example.ts.net, without http:// or a path.")


def test_the_flags_add_to_the_settings_and_the_launch_line_names_both(tmp_path, monkeypatch, capsys):
    built = {}

    class FakeServer:
        def __init__(self, port, **kw):
            built.update(kw)
            self.server_address = ("127.0.0.1", 7861)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(monitor_server, "MonitorServer", FakeServer)
    monkeypatch.setenv(monitor_server.ALLOW_HOST_ENV, "standing.example")
    monkeypatch.setenv(monitor_server.ALLOW_PROJECT_ENV, "/abs/standing")
    assert cli.main(["--monitor", "--serve", "--project", str(tmp_path), "--allow-host", "flag.example",
                     "--allow-project", "/abs/flag"]) == 0
    assert built["allow_hosts"] == ["flag.example", "standing.example"]
    assert built["allow_projects"] == ["/abs/flag", "/abs/standing"]
    lines = capsys.readouterr().out.splitlines()
    assert "flag.example, standing.example" in lines[2]
    assert "--allow-host <name>" in lines[2] and f"{monitor_server.ALLOW_HOST_ENV}=<name>" in lines[2]


def test_every_text_that_says_how_to_admit_more_names_the_flag_beside_the_setting(monkeypatch):
    flags = {name for action in cli.build_parser().flags for name in action.option_strings}
    assert {monitor_server.ALLOW_HOST_FLAG, monitor_server.ALLOW_PROJECT_FLAG} <= flags, "the texts name real flags"
    assert "--allow-project DIR" in monitor_server.PATH_REFUSAL
    assert monitor_server.ALLOW_PROJECT_ENV in monitor_server.PATH_REFUSAL
    for values in (["mac.example.ts.net"], ["<script>"], []):
        refusal = monitor_server._host_refusal(values)
        assert "--allow-host NAME" in refusal and monitor_server.ALLOW_HOST_ENV in refusal
    flat = _flat_help(monkeypatch)
    assert "admit that address's host name with --allow-host" in flat
    assert f"Adds to {monitor_server.ALLOW_HOST_ENV}." in flat and f"Adds to {monitor_server.ALLOW_PROJECT_ENV}." in flat
    readme = (REPO / "tools" / "monitor" / "README.md").read_text(encoding="utf-8")
    assert "--serve --project DIR --allow-host mac.example.ts.net" in readme
    assert "--allow-project /abs/project-a" in readme and "Both flags need `--serve`" in readme
    assert monitor_server.ALLOW_HOST_ENV in readme and monitor_server.ALLOW_PROJECT_ENV in readme


# -- 3. the decider's help says what is sent and where ----------------------------------------------


def test_the_decider_help_says_what_leaves_the_machine_and_where_it_goes(monkeypatch):
    flat = _flat_help(monkeypatch)
    gateway = urlsplit(decisions.JEV_GATEWAY_BASE_URL).hostname
    assert (f"each task description it routes, up to {decisions.TASK_TEXT_LIMIT:,} characters, is sent to "
            f"{decisions.JEV_DEFAULT_HOST} ({gateway} for a {decisions.JEV_GATEWAY_KEY_PREFIX} key, or the "
            f"host in {decisions.JEV_BASE_URL_ENV}) as a billed API call") in flat
    assert "Task text can carry file paths, requirement text and code" in flat
    assert "the run names the destination in its first progress lines" in flat
    assert cli._DECIDER_TEXT_LIMIT == decisions.TASK_TEXT_LIMIT, "the help quotes the limit the decider applies"


def test_a_projectless_session_names_the_destination_as_the_help_says(tmp_path, monkeypatch, capsys):
    """The help promises "the run names the destination in its first progress
    lines". A project run did; ``--session`` alone built the decider and said
    nothing before task text could be sent."""
    order = []

    def session(goal, store, *, fleet, config, **kw):
        order.append(("session", config.decider))
        return SimpleNamespace(run=lambda **kwargs: [])

    monkeypatch.setattr("quadratus.runtime.Fleet", _Fleet)
    monkeypatch.setattr("quadratus.runtime.new_session", session)
    monkeypatch.delenv(decisions.JEV_API_KEY_ENV, raising=False)
    monkeypatch.delenv(decisions.JEV_BASE_URL_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--session", GOAL, "--decider", "jev"]) == 0
    out = capsys.readouterr().out
    said = decisions.disclosure(order[0][1])
    assert isinstance(order[0][1], decisions.JevDecider) and decisions.JEV_DEFAULT_HOST in said
    assert f">> {said}" in out and out.index(said) < out.index("Fleet:"), "said before anything is seated"
    # The rule sends nothing anywhere, so it says nothing: the line made before.
    order.clear()
    assert cli.main(["--session", GOAL]) == 0
    assert order == [("session", None)] and "Decider" not in capsys.readouterr().out


# -- 4. a contained run carries the operator's trust settings ---------------------------------------


@pytest.fixture
def docker(monkeypatch, tmp_path):
    """Stand in for the docker command line: record each argv and exit 0."""
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")

    monkeypatch.setattr(isolated_run.subprocess, "run", run)
    work, runtime = tmp_path / "work", tmp_path / "runtime"
    work.mkdir()
    runtime.mkdir()

    def start(**kwargs):
        calls.clear()
        result = isolated_run.run_isolated(image="sha256:" + "0" * 64, work=work, runtime=runtime,
                                           command=["true"], **kwargs)
        assert result.outcome == "success"
        create = calls[0]
        return [create[i + 1] for i, word in enumerate(create) if word == "--env"]

    return start


FIXED_ENV = ["HOME=/tmp/solver-home", "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=/opt/quadratus"]


def test_a_contained_run_is_given_the_trust_settings_the_operator_set(docker, monkeypatch):
    assert isolated_run.TRUST_SETTINGS == (policy.TRUST_ENV, project_watch.ALLOW_AGENT_CONFIG_ENV)
    assert docker() == FIXED_ENV, "unset is unset inside too"
    monkeypatch.setenv(policy.TRUST_ENV, "SHA256:" + DIGEST.upper())
    monkeypatch.setenv(project_watch.ALLOW_AGENT_CONFIG_ENV, "yes")
    assert docker() == [*FIXED_ENV, f"{policy.TRUST_ENV}=SHA256:{DIGEST.upper()}",
                        f"{project_watch.ALLOW_AGENT_CONFIG_ENV}=yes"], "as written: the run inside reads it"
    monkeypatch.setenv(policy.TRUST_ENV, "   ")
    assert docker() == [*FIXED_ENV, f"{project_watch.ALLOW_AGENT_CONFIG_ENV}=yes"], "blank is not set"
    assert docker(forward_trust=False) == FIXED_ENV


def test_a_contained_run_is_given_nothing_else_from_the_environment(docker, monkeypatch):
    secrets = {"ANTHROPIC_API_KEY": "sk-ant-secret", "OPENAI_API_KEY": "sk-secret", "GH_TOKEN": "ghp_secret",
               "TYPESAFE_API_KEY": "ts-secret", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
               "QUADRATUS_ENV_PASS": "GH_TOKEN", "QUADRATUS_WATCH_SECONDS": "5"}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(project_watch.ALLOW_AGENT_CONFIG_ENV, "1")
    carried = docker()
    assert carried == [*FIXED_ENV, f"{project_watch.ALLOW_AGENT_CONFIG_ENV}=1"]
    for name in isolated_run.TRUST_SETTINGS:
        assert not child_env.withheld(name), "a trust setting is not a credential by the harness's own rule"


# -- 5. the form calls the shared run-limit rule ----------------------------------------------------


def test_the_project_form_calls_the_shared_validation_and_keeps_no_copy(monkeypatch):
    calls = []

    def shared(enabled, fields, *args, **kwargs):
        calls.append((enabled, fields, args, kwargs))
        return "limits", "survey"

    monkeypatch.setattr(gui, "limits_from_fields", shared)
    values = (90, 5_000_000, 3600, 2, 250_000, 1_500_000, 4)
    assert gui.limits_from_form(True, *values) == ("limits", "survey")
    assert calls == [(True, dict(zip(LIMIT_FIELDS, values, strict=True)), (), {})]
    for copy in ("_whole", "_LIMIT_LABELS"):
        assert not hasattr(gui, copy), f"gui.{copy} was the second copy of the rule"
    assert gui.LIMIT_FIELDS is LIMIT_FIELDS and gui.LIMIT_LABELS is LIMIT_LABELS
    assert gui.limits_from_fields is shared and gui.whole_number is whole_number


def test_the_task_limit_is_read_by_the_shared_rule_too():
    assert gui.task_limit(20.0) == 20 == whole_number(20.0, LIMIT_LABELS["max_tasks"], 1)
    for bad in (None, "", 0, 1.5, True, "many"):
        with pytest.raises(ValueError) as form:
            gui.task_limit(bad)
        with pytest.raises(ValueError) as shared:
            whole_number(bad, LIMIT_LABELS["max_tasks"], 1)
        assert str(form.value) == str(shared.value) and "Task limit" in str(form.value)
    assert "not valid" in gui.limits_summary(0, False, 24, 500_000, 900, 2, 0, 0, 0)
    limits, survey = gui.limits_from_form(True, 24, 500_000, 900, 2, 0, 0, 0)
    assert (limits, survey) == limits_from_fields(True, dict(zip(LIMIT_FIELDS, (24, 500_000, 900, 2, 0, 0, 0),
                                                                 strict=True)))


def test_the_form_labels_are_the_shared_labels():
    pytest.importorskip("gradio")
    labels = {getattr(block, "label", None) for block in gui.build_interface(Settings()).blocks.values()}
    assert {LIMIT_LABELS[name] for name in LIMIT_FIELDS} <= labels


# -- 6. the form offers no mode the runner refuses --------------------------------------------------


@pytest.mark.requirements_ledger
def test_the_form_does_not_offer_a_mode_the_runner_refuses_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_REQUIREMENTS_LEDGER", "1")
    offered, note = gui.mode_choices()
    assert offered == ["adversarial", "collaborative"]
    assert note.startswith("solo is not offered: ") and mode_audit_gap("solo") in note
    assert "QUADRATUS_REQUIREMENTS_LEDGER=0" in note, "the way to get it back is said"
    monkeypatch.setattr(project_run, "_project_lock", lambda *a: pytest.fail("refused before the lock"))
    for mode in sorted(MODE_ROSTERS):
        if mode in offered:
            continue
        with pytest.raises(ValueError):
            project_run.run_project(GOAL, tmp_path, Settings(backend="cli"), mode=mode)
    pytest.importorskip("gradio")
    by_label = {getattr(b, "label", None): b for b in gui.build_interface(Settings()).blocks.values()}
    dropdown = by_label["Collaboration"]
    assert [choice[1] for choice in dropdown.choices] == offered and dropdown.value == "adversarial"
    assert dropdown.info == note


def test_every_mode_is_offered_when_the_runner_would_start_each(tmp_path, monkeypatch):
    assert not SessionConfig().requirements_ledger, "the suite's default: the ledger is off"
    offered, note = gui.mode_choices()
    assert offered == sorted(MODE_ROSTERS) and "solo" in offered and note == ""
    pytest.importorskip("gradio")
    by_label = {getattr(b, "label", None): b for b in gui.build_interface(Settings()).blocks.values()}
    assert [choice[1] for choice in by_label["Collaboration"].choices] == offered
    assert not by_label["Collaboration"].info


# -- 7. chosen session settings are part of the run record ------------------------------------------


def _saved(run_dir):
    return (json.loads((run_dir / "run-limits.json").read_text()),
            json.loads((run_dir / "result.json").read_text()))


def test_chosen_session_settings_are_in_the_run_record(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x\n")
    before_first_call = {}

    def run(self, **kwargs):
        newest = max((tmp_path / ".quadratus" / "runs").iterdir(), key=lambda p: p.stat().st_mtime_ns)
        before_first_call.update(json.loads((newest / "run-limits.json").read_text()))

    monkeypatch.setattr(Session, "run", run)
    defaults = SessionConfig()
    chosen = dict(design_cross_check=False, recent_entries=4,
                  # Typed, but the value every run has: not a difference to record.
                  max_fix_cycles=defaults.max_fix_cycles, spread_leads=defaults.spread_leads)
    result = project_run.run_project(GOAL, tmp_path, Settings(backend="cli"), session_options=chosen)
    limits, record = _saved(result.run_dir)
    assert limits["session_settings"] == {"design_cross_check": False, "recent_entries": 4}
    assert before_first_call == limits, "saved before the first call, so an interrupted run says it too"
    assert record["session_settings"] == limits["session_settings"]
    assert record["selected_limits"] == limits, "run-limits.json is still repeated whole"


def test_a_run_that_chose_no_setting_has_the_record_it_always_had(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x\n")
    monkeypatch.setattr(Session, "run", lambda self, **kw: None)
    defaults = SessionConfig()
    for options in (None, {}, dict(max_consults=defaults.max_consults, design_cross_check=True)):
        result = project_run.run_project(GOAL, tmp_path, Settings(backend="cli"), session_options=options)
        limits, record = _saved(result.run_dir)
        assert "session_settings" not in limits and "session_settings" not in record
        assert set(limits) == {"max_tasks", "run_limits", "summary_call_reserve_tokens", "lead_max_turns",
                               "lead_max_turns_source", "survey_recovery_tasks", "described"}
    assert "session_settings" not in project_run._selected_limits(20, None, None)


def test_the_record_reads_the_defaults_from_the_real_class_when_a_caller_wraps_the_name(tmp_path, monkeypatch):
    """Five existing tests put a wrapper under ``project_run.SessionConfig`` to
    add a setting. The record read the defaults through that name with
    ``dataclasses.fields`` and every such run ended in a TypeError."""
    (tmp_path / "app.py").write_text("x\n")
    real = project_run.SessionConfig
    monkeypatch.setattr(project_run, "SessionConfig", lambda **kw: real(**{**kw, "security_verdict_json": True}))
    monkeypatch.setattr(Session, "run", lambda self, **kw: None)
    assert project_run._selected_limits(20, None, None, session_settings={
        "design_cross_check": False, "max_consults": real().max_consults})["session_settings"] == {
            "design_cross_check": False}
    result = project_run.run_project(GOAL, tmp_path, Settings(backend="cli"),
                                     session_options={"recent_entries": 4})
    limits, record = _saved(result.run_dir)
    assert limits["session_settings"] == record["session_settings"] == {"recent_entries": 4}


def test_a_setting_flag_reaches_the_record_through_the_command_line(tmp_path, monkeypatch, capsys):
    (tmp_path / "app.py").write_text("x\n")
    monkeypatch.setattr(Session, "run", lambda self, **kw: None)
    cli.main([GOAL, "--project", str(tmp_path), "--no-design-cross-check", "--max-consults", "1"])
    capsys.readouterr()
    limits, record = _saved(next((tmp_path / ".quadratus" / "runs").iterdir()))
    assert limits["session_settings"] == record["session_settings"] == {"design_cross_check": False,
                                                                        "max_consults": 1}


# -- 8. the usage log and the budget state are private from creation --------------------------------


@pytest.fixture
def open_umask():
    """A umask that would leave a plainly created file readable by everyone."""
    old = os.umask(0o022)
    yield
    os.umask(old)


def test_the_usage_log_is_private_from_its_first_line(tmp_path, open_umask):
    log = tmp_path / "usage.jsonl"
    meter = UsageMeter(log)
    meter.record(model="claude:opus", prompt="p" * 40, reply="r" * 8)
    assert _mode(log) == 0o600
    meter.record(model="claude:opus", input_tokens=5, output_tokens=7)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [row["input_tokens"] for row in rows] == [10, 5] and rows[1]["measured"] is True


def test_a_usage_log_an_older_version_left_open_is_closed_on_the_next_line(tmp_path, open_umask):
    log = tmp_path / "usage.jsonl"
    log.write_text('{"model": "old"}\n')
    assert _mode(log) == 0o644
    UsageMeter(log).record(model="claude:opus", prompt="p", reply="r")
    assert _mode(log) == 0o600 and len(log.read_text().splitlines()) == 2


def test_the_usage_log_is_never_written_through_a_link_and_metering_still_fails_no_call(tmp_path):
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("not a usage log\n")
    log = tmp_path / "usage.jsonl"
    log.symlink_to(elsewhere)
    meter = UsageMeter(log)
    with pytest.raises(OSError):
        meter.record(model="claude:opus", prompt="p", reply="r")
    assert elsewhere.read_text() == "not a usage log\n"
    # The wrapper is how a run meters: the call's answer comes back all the same.
    assert meter.wrap(lambda model, prompt: "the reply")("claude:opus", "prompt") == "the reply"
    assert elsewhere.read_text() == "not a usage log\n"


def test_the_budget_state_and_a_preserved_reply_are_private_and_whole(tmp_path, open_umask):
    state = tmp_path / "run"
    state.mkdir()
    budget = RunBudget(RunLimits(max_calls=3, max_reported_tokens=10), path=state / "budget.json")
    ticket, _ = budget.reserve()
    assert _mode(state / "budget.json") == 0o600
    assert json.loads((state / "budget.json").read_text())["in_flight"] == 1
    with pytest.raises(RunBudgetExceeded, match="reported_token_threshold"):
        budget.finish(ticket, {"input_tokens": 9, "output_tokens": 4}, reply="the reply that crossed the line")
    saved = json.loads((state / "budget.json").read_text())
    assert saved["stop_reason"] == "reported_token_threshold"
    assert saved["preserved_responses"] == ["budget-responses/attempt-1.txt"]
    assert _mode(state / "budget.json") == 0o600
    assert _mode(state / "budget-responses") == 0o700
    reply = state / "budget-responses" / "attempt-1.txt"
    assert _mode(reply) == 0o600 and reply.read_text() == "the reply that crossed the line"
    assert sorted(os.listdir(state)) == ["budget-responses", "budget.json"], "no temporary file is left behind"
    assert os.listdir(state / "budget-responses") == ["attempt-1.txt"]


def test_the_budget_state_replaces_a_link_and_never_writes_through_it(tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}")
    (tmp_path / "budget.json").symlink_to(elsewhere)
    budget = RunBudget(RunLimits(max_calls=3, max_reported_tokens=10), path=tmp_path / "budget.json")
    budget.reserve()
    assert elsewhere.read_text() == "{}"
    assert not (tmp_path / "budget.json").is_symlink()
    assert json.loads((tmp_path / "budget.json").read_text())["reserved_attempts"] == 1


def test_a_budget_state_that_cannot_be_written_still_stops_the_run(tmp_path):
    blocked = tmp_path / "not-a-folder"
    blocked.write_text("x")
    budget = RunBudget(RunLimits(max_calls=3, max_reported_tokens=10), path=blocked / "budget.json")
    with pytest.raises(RunBudgetExceeded, match="budget_state_unavailable"):
        budget.reserve()


def test_a_real_run_leaves_both_files_private(tmp_path, monkeypatch, open_umask):
    (tmp_path / "app.py").write_text("x\n")
    fleets, seen = [], {}

    class Fleet(runtime.Fleet):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            fleets.append(self)

    def run(self, **kwargs):
        # What one model call does to the two files, with no model called.
        fleet = fleets[-1]
        fleet.usage_meter.record(model="claude:opus", prompt="p", reply="r")
        ticket, _ = fleet.run_budget.reserve()
        fleet.run_budget.finish(ticket, {"input_tokens": 3, "output_tokens": 4})
        folder = fleet.usage_meter.path.parent
        seen.update({name: _mode(folder / name) for name in ("usage.jsonl", "budget.json")})

    monkeypatch.setattr(runtime, "Fleet", Fleet)
    monkeypatch.setattr(Session, "run", run)
    limits = RunLimits(max_calls=3, max_reported_tokens=1_000, wall_seconds=60)
    result = project_run.run_project(GOAL, tmp_path, Settings(backend="cli"), run_limits=limits)
    assert seen == {"usage.jsonl": 0o600, "budget.json": 0o600}, "while the run is going, not only at its end"
    for name in sorted(os.listdir(result.run_dir)):
        path = result.run_dir / name
        assert _mode(path) == (0o700 if path.is_dir() else 0o600), name
    assert json.loads((result.run_dir / "result.json").read_text())["budget"]["reported_tokens"] == 7


# -- 9. a task limit that is not a whole number is refused in words ---------------------------------


@pytest.mark.parametrize("bad", [None, "20", 2.5, 20.0, True, [3]])
def test_the_runner_refuses_a_task_limit_that_is_not_a_whole_number_before_anything_else(bad, tmp_path, monkeypatch):
    monkeypatch.setattr(project_run, "_project_lock", lambda *a: pytest.fail("must refuse before locking"))
    # An empty goal is the next refusal in line: the task limit is read first.
    with pytest.raises(ValueError) as refused:
        project_run.run_project("", tmp_path, Settings(backend="cli"), max_tasks=bad)
    assert str(refused.value) == f"max_tasks must be a whole number of at least 1, not {bad!r}"


def test_a_whole_task_limit_is_read_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(project_run, "_project_lock", lambda *a: pytest.fail("must refuse before locking"))
    for low in (0, -3):
        with pytest.raises(ValueError, match="^max_tasks must be at least 1$"):
            project_run.run_project(GOAL, tmp_path, Settings(backend="cli"), max_tasks=low)
    with pytest.raises(ValueError, match="Describe the project change"):
        project_run.run_project("", tmp_path, Settings(backend="cli"), max_tasks=1)


def test_a_cleared_task_limit_reaches_the_form_as_words(tmp_path):
    outputs = list(gui.run_project_ui("goal", str(tmp_path), True, "", "adversarial", None, Settings()))
    assert outputs[-1] == ("Run failed: max_tasks must be a whole number of at least 1, not None", "", [])
    assert not (tmp_path / ".quadratus").exists(), "refused before the project is touched"


# -- 10. the names a child process is not given are visible without -v ------------------------------


def test_the_withheld_line_names_what_a_check_does_not_inherit_and_never_a_value():
    base = {"PATH": "/usr/bin", "HOME": "/home/op", "MY_SERVICE_TOKEN": "tok-secret-value",
            "DOCKER_HOST": "tcp://secret-host:2376", "EDITOR": "vi"}
    line = child_env.withheld_notice(base)
    assert "DOCKER_HOST, MY_SERVICE_TOKEN" in line and f"Set {child_env.PASS_VARIABLE}=NAME,NAME to keep one." in line
    assert "secret" not in line and "PATH" not in line.replace(child_env.PASS_VARIABLE, "")
    assert child_env.withheld_notice({"PATH": "/usr/bin", "EDITOR": "vi"}) == ""
    kept = dict(base, **{child_env.PASS_VARIABLE: "DOCKER_HOST"})
    assert "DOCKER_HOST" not in child_env.withheld_notice(kept) and "MY_SERVICE_TOKEN" in child_env.withheld_notice(kept)
    assert child_env.withheld_names(base) == ["DOCKER_HOST", "MY_SERVICE_TOKEN"], "the line lists exactly these"


def test_status_shows_the_withheld_names(monkeypatch, capsys):
    monkeypatch.setattr(cli, "Orchestrator", lambda settings: SimpleNamespace(status_lines=lambda: ["fake: ok"]))
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok-secret-value")
    assert cli.main(["--status"]) == 0
    out = capsys.readouterr().out
    assert "Child environment:" in out and "MY_SERVICE_TOKEN" in out and "tok-secret-value" not in out
    assert child_env.PASS_VARIABLE in out


def test_a_run_says_what_its_checks_will_not_inherit_before_it_starts(tmp_path, monkeypatch, capsys, runner):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok-secret-value")
    assert cli.main([GOAL, "--project", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.index(">> Environment variables not passed to checks") < out.index("checked")
    assert "MY_SERVICE_TOKEN" in out and "tok-secret-value" not in out
    seen = _form_runner(monkeypatch, tmp_path)
    outputs = list(gui.run_project_ui("goal", str(tmp_path), True, "", "adversarial", 3, Settings()))
    assert seen and outputs[0][0].startswith("**Run limits:**")
    assert outputs[-1][0] == "report"


def test_the_form_shows_the_withheld_names_while_the_run_is_going(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok-secret-value")

    def run(goal, folder, settings, *, progress, **kwargs):
        progress("a first progress line")
        import time
        time.sleep(0.6)
        return SimpleNamespace(report="report", diff="", run_dir=tmp_path)

    monkeypatch.setattr(project_run, "run_project", run)
    shown = [text for text, _, _ in gui.run_project_ui("goal", str(tmp_path), True, "", "adversarial", 3, Settings())]
    during = [text for text in shown if "a first progress line" in text]
    assert during and all("MY_SERVICE_TOKEN" in text and "tok-secret-value" not in text for text in during)


# -- records glue: a bad rulings file is reported before any model call -----------------------------


def test_a_bad_rulings_file_is_reported_by_the_command_line_before_the_runner(tmp_path, runner, capsys):
    """The loader refuses a bad pattern when the file is read (records). The
    command line reads the file while building the call, so the refusal comes
    before the runner, and with it any model call, is reached."""
    rulings = tmp_path / "rulings.json"
    rulings.write_text(json.dumps([{"about": "unclosed (group", "answer": "x"}]))
    project = tmp_path / "project"
    project.mkdir()
    assert cli.main([GOAL, "--project", str(project), "--rulings", str(rulings)]) == 1
    assert "Error: " in capsys.readouterr().out and not runner, "refused before the runner was called"
    rulings.write_text(json.dumps([{"about": "storage", "answer": "use sqlite"}]))
    assert cli.main([GOAL, "--project", str(project), "--rulings", str(rulings)]) == 0
    assert runner["ask_operator"]("Which storage?") == "use sqlite"


# -- records glue: a projectless session's state folder ---------------------------------------------


class _Fleet:
    def __init__(self, settings=None, **kw):
        self.usage_meter = kw.get("usage_meter")

    def status_lines(self):
        return ["claude [cli]: Claude (opus)"]

    def close(self):
        pass


def test_a_projectless_sessions_state_folder_is_private_and_its_map_notices_are_shown(tmp_path, monkeypatch,
                                                                                         capsys, open_umask):
    made = {}

    def session(goal, store, *, fleet, config, **kw):
        made["config"] = config
        # One metered call, as a model call makes it.
        fleet.usage_meter.record(model="claude:opus", prompt="p", reply="r")
        return SimpleNamespace(run=lambda **kwargs: [])

    monkeypatch.setattr("quadratus.runtime.Fleet", _Fleet)
    monkeypatch.setattr("quadratus.runtime.new_session", session)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--session", GOAL]) == 0
    state = tmp_path / ".quadratus"
    assert _mode(state) == 0o700 and (state / ".gitignore").read_text().splitlines()[1] == "*"
    assert _mode(state / "usage.jsonl") == 0o600
    # A damaged map costs its damaged lines and the operator is told, as a project run tells them.
    (state / "codebase-map.jsonl").write_text("this is not a note\n")
    capsys.readouterr()
    assert cli.main(["--session", GOAL]) == 0
    assert ">> Codebase map: " in capsys.readouterr().out
