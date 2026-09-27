"""Editing-role caps and runtime-dependency identity (contract v2 A, C, D on #25).

Whole-controller replays: only the vendor CLI launch is faked. The gate is
real pytest or real node, the dependency identity reads the real tree, and a
capped call is the vendor's own ``error_max_turns`` envelope. Scripted
replies prove routing, never model compliance.

Run 19 (2026-09-27) is the reason for both halves: a gate-fix and a
design-fix ran past the operator's 14 rounds because only ``lead`` was
capped, and a lead wrote a ``node_modules`` shim that the source checks
could not see and the project's check then resolved.
"""

import json
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from quadratus.config import Settings
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import (
    DECL_T1,
    FILES,
    FILES_OK,
    FIXED,
    Script,
    _design_files,
    _design_script,
)

CAP = 14
SETTINGS = dict(lead_max_turns=CAP)
SHIM = "module.exports = 'real';\n"


def _run(tmp_path, monkeypatch, script, *, files=FILES_OK, settings_kw=SETTINGS, lead="claude:opus", **kw):
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: lead)
    return H.run(tmp_path, monkeypatch, script, files=files,
                 settings=Settings(backend="cli", **(settings_kw or {})), **kw)


def _turns_flag(call):
    argv = call.argv
    return argv[argv.index("--max-turns") + 1] if "--max-turns" in argv else None


def _lead_writes(files, reply="Done.\nCHANGED: []", delete=()):
    def lead(call, replay):
        H.write(call, files)
        for name in delete:
            shutil.rmtree(Path(call.cwd) / name)
        return reply
    return lead


# == A. the lead's editing roles are capped =============================================

@pytest.mark.parametrize("turns", [CAP, CAP + 1], ids=["at", "above"])
@pytest.mark.parametrize("edits", [False, True], ids=["unchanged", "changed"])
def test_a_capped_revision_takes_the_capped_task_path(tmp_path, monkeypatch, turns, edits):
    def revision(call, replay):
        if edits:
            H.write(call, {"app.py": FIXED + "# tidied\n"})
        return H.claude_cap("Still revising.", num_turns=turns)
    replay = _run(tmp_path, monkeypatch, Script(revision=revision), files=FILES)
    (rev,) = replay.of("revision")
    assert _turns_flag(rev) == str(CAP)
    assert H.result_json(replay)["turn_limited_tasks"] == ["t1"]
    assert not replay.of("gate-fix") and not replay.of("closeout"), "no continuation, gate or close-out"
    assert H.gate_results(replay) == [], "an unfinished task is not gated"
    assert (replay.project / "app.py").read_text() == FIXED + ("# tidied\n" if edits else ""), "edits kept"
    assert not replay.result.completed


def test_a_revision_below_the_cap_is_unchanged(tmp_path, monkeypatch):
    def revision(call, replay):
        return H.claude_ok("Nothing to change after review.\nCHANGED: []", num_turns=5)
    replay = _run(tmp_path, monkeypatch, Script(revision=revision), files=FILES)
    assert _turns_flag(replay.of("revision")[0]) == str(CAP)
    assert H.gate_results(replay) == ["PASSED"] and replay.result.error == ""
    assert H.result_json(replay)["turn_limited_tasks"] == []


def _looked(call, replay):
    return "Looked; left it.\nCHANGED: []"


@pytest.mark.parametrize("turns", [CAP, CAP + 1], ids=["at", "above"])
def test_a_capped_gate_fix_that_fixed_the_code_is_gated_again_and_passes(tmp_path, monkeypatch, turns):
    def gate_fix(call, replay):
        H.write(call, {"app.py": FIXED})
        return H.claude_cap("Fixed add; was about to rerun the tests.", num_turns=turns)
    replay = _run(tmp_path, monkeypatch, Script(lead=_looked, **{"gate-fix": gate_fix}), files=FILES)
    (fix,) = replay.of("gate-fix")
    assert _turns_flag(fix) == str(CAP)
    assert H.gate_results(replay) == ["PASSED"], "the required check decides, after the capped fix"
    assert replay.result.error == "" and replay.of("closeout")
    assert H.result_json(replay)["turn_limited_tasks"] == []
    assert replay.artifacts("capped-fix")


@pytest.mark.parametrize("turns", [CAP, CAP + 1], ids=["at", "above"])
def test_a_capped_gate_fix_that_changed_nothing_spends_its_attempt_and_the_check_stays_failed(
        tmp_path, monkeypatch, turns):
    def gate_fix(call, replay):
        return H.claude_cap("Read the test.", num_turns=turns)
    replay = _run(tmp_path, monkeypatch, Script(lead=_looked, **{"gate-fix": gate_fix}), files=FILES)
    assert len(replay.of("gate-fix")) == 1, "one attempt spent, no continuation or replay"
    assert H.gate_results(replay) == ["FAILED"]
    assert not replay.result.completed
    assert replay.artifacts("capped-fix")


def test_a_gate_fix_below_the_cap_is_unchanged(tmp_path, monkeypatch):
    def gate_fix(call, replay):
        H.write(call, {"app.py": FIXED})
        return H.claude_ok('Fixed add.\nCHANGED: ["app.py"]', num_turns=6)
    replay = _run(tmp_path, monkeypatch, Script(lead=_looked, **{"gate-fix": gate_fix}), files=FILES)
    assert _turns_flag(replay.of("gate-fix")[0]) == str(CAP)
    assert H.gate_results(replay) == ["PASSED"] and replay.result.error == ""
    assert not replay.artifacts("capped-fix")


def test_a_capped_gate_fix_outside_its_scope_still_stops_with_work_preserved(tmp_path, monkeypatch):
    def gate_fix(call, replay):
        H.write(call, {"app.py": FIXED, "README.md": "# rewritten\n"})
        return H.claude_cap("Fixed add and tidied the README.")
    replay = _run(tmp_path, monkeypatch, Script(lead=_looked, **{"gate-fix": gate_fix}), files=FILES)
    assert replay.result.error.startswith("PartialWorkStopped") and "exceeded the declared scope" in replay.result.error
    assert (replay.project / "README.md").read_text() == "# rewritten\n"


@pytest.mark.parametrize("turns", [CAP, CAP + 1], ids=["at", "above"])
def test_a_capped_design_fix_with_fresh_renders_is_checked_and_reviewed(tmp_path, monkeypatch, turns):
    script = _design_script("unused")

    def design_fix(call, replay):
        H.write(call, {"static/style.css": "#import { padding: 10px; }\n"})
        H.evidence(Path(call.cwd), "t1", age=H.FRESH)
        return H.claude_cap("Recaptured; was about to report.", num_turns=turns)
    script.overrides["design-fix"] = design_fix
    replay = _run(tmp_path, monkeypatch, script, files=_design_files())
    (fix,) = replay.of("design-fix")
    assert _turns_flag(fix) == str(CAP)
    assert replay.result.error == "" and len(replay.of("design-review")) == 1
    assert H.gate_results(replay)[-1] == "PASSED", "the gate re-ran after the capped fix"


@pytest.mark.parametrize("turns", [CAP, CAP + 1], ids=["at", "above"])
def test_a_capped_design_fix_that_left_nothing_is_unverified_never_met(tmp_path, monkeypatch, turns):
    script = _design_script("unused")
    script.overrides["design-fix"] = lambda call, replay: H.claude_cap("Looked around.", num_turns=turns)
    replay = _run(tmp_path, monkeypatch, script, files=_design_files())
    assert len(replay.of("design-fix")) == 1, "one attempt, no continuation"
    assert replay.result.error.startswith("DesignUnverified") and not replay.result.completed
    assert not replay.of("design-review")


def test_a_refused_gate_fix_keeps_the_refusal(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch,
                  Script(lead=_looked, **{"gate-fix": lambda call, replay: H.claude_refusal("cyber")}), files=FILES)
    assert replay.result.error.startswith("ProviderRefusal")


def test_a_gate_fix_denied_the_harness_command_at_the_cap_is_a_capability_stop(tmp_path, monkeypatch):
    def gate_fix(call, replay):
        capped = json.loads(H.claude_cap("Kept trying to capture."))
        capped["permission_denials"] = [dict(tool_name="Bash", tool_input={
            "command": "PYTHONPATH=/pkg python -m quadratus.design_evidence http://localhost:5000/ t1 ."})]
        return json.dumps(capped)
    replay = _run(tmp_path, monkeypatch, Script(lead=_looked, **{"gate-fix": gate_fix}), files=FILES)
    assert replay.result.error.startswith("CapabilityUnavailable") and "turn limit" in replay.result.error
    assert len(replay.of("gate-fix")) == 1 and H.gate_results(replay) == []


def test_reviewers_closeout_and_the_orchestrator_are_not_capped(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(), files=FILES)
    assert replay.result.error == ""
    for role in ("orchestrator", "collaborator", "recheck"):
        assert all(_turns_flag(c) is None for c in replay.of(role)), role
    # The close-out keeps its own one-turn, tool-less summary form.
    assert all(_turns_flag(c) == "1" for c in replay.of("closeout"))
    assert replay.of("collaborator") and replay.of("closeout")
    assert all(_turns_flag(c) == str(CAP) for c in replay.of("lead") + replay.of("revision"))


# == C. the capture-steps line ==========================================================

@pytest.mark.requirements_ledger
def test_the_orchestrator_is_told_when_steps_are_empty_and_that_they_never_replace_a_finding(
        tmp_path, monkeypatch):
    from tests.lifecycle.test_harness_capture import AUDIT, REQS, _no_edit, _profile
    from tests.lifecycle.test_harness_capture import _run as capture_run
    profile, _ = _profile(tmp_path)
    replay = capture_run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _no_edit}, profile=profile, max_tasks=1)
    prompt = replay.of("orchestrator")[0].prompt
    assert '"steps": []' in prompt and "visible without interaction" in prompt
    assert "never an element already on the page at load" in prompt
    assert 'RESOLVES a finding declares that finding\'s page and steps as measured; "steps": [] never ' \
           "replaces them" in prompt


def test_without_a_capture_profile_the_line_is_not_sent(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script())
    assert "visible without interaction" not in replay.of("orchestrator")[0].prompt


# == D. runtime-dependency identity =====================================================

def _stopped(replay, window, *paths):
    error = replay.result.error
    assert error.startswith("DependencyTreeChanged"), error
    assert window in error, error
    for path in paths:
        assert path in error, (path, error)
    record = H.result_json(replay)["dependency_identity"]
    assert record["status"] == "changed" and record["events"][-1]["window"].endswith(window)
    return record


def test_a_lead_that_creates_a_node_modules_shim_is_stopped_after_that_call(tmp_path, monkeypatch):
    lead = _lead_writes({"node_modules/playwright/index.js": SHIM})
    replay = _run(tmp_path, monkeypatch, Script(lead=lead))
    _stopped(replay, "during lead (t1)", "node_modules/playwright/index.js")
    assert len(replay.of("lead")) == 1 and not replay.of("collaborator") and H.gate_results(replay) == []
    assert (replay.project / "node_modules/playwright/index.js").read_text() == SHIM, "nothing reverted"


def test_a_lead_that_edits_an_existing_venv_file_is_stopped(tmp_path, monkeypatch):
    site = ".venv/lib/python3.11/site-packages/x.py"
    lead = _lead_writes({site: "VALUE = 2\n"})
    replay = _run(tmp_path, monkeypatch, Script(lead=lead), files={**FILES_OK, site: "VALUE = 1\n"})
    _stopped(replay, "during lead (t1)", site)


def test_a_new_nested_tree_is_a_change(tmp_path, monkeypatch):
    lead = _lead_writes({"pkg/node_modules/x/index.js": SHIM})
    replay = _run(tmp_path, monkeypatch, Script(lead=lead))
    _stopped(replay, "during lead (t1)", "pkg/node_modules")


def test_a_tree_deleted_by_an_editing_call_is_a_change(tmp_path, monkeypatch):
    lead = _lead_writes({}, delete=["node_modules"])
    replay = _run(tmp_path, monkeypatch, Script(lead=lead), files={**FILES_OK, "node_modules/pkg/index.js": SHIM})
    _stopped(replay, "during lead (t1)", "node_modules/pkg/index.js")


def test_a_revision_or_gate_fix_that_touches_a_tree_is_stopped_in_its_window(tmp_path, monkeypatch):
    def gate_fix(call, replay):
        H.write(call, {"app.py": FIXED, "node_modules/helper.js": SHIM})
        return 'Fixed add.\nCHANGED: ["app.py"]'
    replay = _run(tmp_path, monkeypatch, Script(lead=_looked, **{"gate-fix": gate_fix}), files=FILES)
    _stopped(replay, "during gate-fix (t1)", "node_modules/helper.js")
    assert H.gate_results(replay) == [], "no receipt after the change is accepted"


def test_a_capped_gate_fix_that_touched_a_tree_keeps_its_cap_and_is_stopped_before_the_check(
        tmp_path, monkeypatch):
    def gate_fix(call, replay):
        H.write(call, {"app.py": FIXED, "node_modules/helper.js": SHIM})
        return H.claude_cap("Fixed add.")
    replay = _run(tmp_path, monkeypatch, Script(lead=_looked, **{"gate-fix": gate_fix}), files=FILES)
    error = replay.result.error
    assert error.startswith("DependencyTreeChanged") and "before check (t1)" in error, error
    record = H.result_json(replay)["dependency_identity"]
    assert record["events"][0]["window"] == "during gate-fix (t1)" and "node_modules/helper.js" in record["events"][0]["changed"]


def _writing_check(relative):
    code = (f"import pathlib; p = pathlib.Path({relative!r}); p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text('1')")
    return shlex.join([sys.executable, "-c", code])


def test_a_check_that_writes_into_a_tree_is_stopped_and_its_receipt_is_not_accepted(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(), files={**FILES, "node_modules/pkg/index.js": SHIM},
                  check=_writing_check("node_modules/x.js"))
    _stopped(replay, "during check (t1)", "node_modules/x.js")
    assert H.result_json(replay)["checks"] == []


def test_a_check_writing_an_operator_declared_cache_proceeds_and_is_recorded(tmp_path, monkeypatch):
    from quadratus import project_run
    real = project_run.SessionConfig
    monkeypatch.setattr(project_run, "SessionConfig",
                        lambda **kw: real(**kw, dependency_cache_exemptions=("node_modules/.cache",)))
    replay = _run(tmp_path, monkeypatch, Script(), files={**FILES, "node_modules/pkg/index.js": SHIM},
                  check=_writing_check("node_modules/.cache/babel.json"))
    assert replay.result.error == "", replay.result.error
    record = H.result_json(replay)["dependency_identity"]
    assert record["status"] == "unchanged" and record["exemptions"] == ["node_modules/.cache"]
    assert record["events"][0]["exempt_changed"] == ["node_modules/.cache", "node_modules/.cache/babel.json"]
    assert record["events"][0]["window"] == "during check (t1)"


def test_the_same_cache_write_without_a_declared_exemption_is_a_change(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(), files={**FILES, "node_modules/pkg/index.js": SHIM},
                  check=_writing_check("node_modules/.cache/babel.json"))
    _stopped(replay, "during check (t1)", "node_modules/.cache/babel.json")


@pytest.mark.requirements_ledger
def test_a_preview_that_writes_into_a_tree_is_stopped_in_the_preview_window(tmp_path, monkeypatch):
    from tests.lifecycle.test_harness_capture import AUDIT, FITTING_PAGE, REQS, _free_port, _no_edit
    from tests.lifecycle.test_harness_capture import _run as capture_run
    port = _free_port()
    serve = ("import pathlib, runpy, sys\n"
             "p = pathlib.Path('node_modules/.vite/dep.js')\n"
             "p.parent.mkdir(parents=True, exist_ok=True)\n"
             "p.write_text('1')\n"
             f"sys.argv = ['http.server', '{port}', '--bind', '127.0.0.1', '--directory', 'templates']\n"
             "runpy.run_module('http.server', run_name='__main__')\n")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(dict(preview=[sys.executable, "serve.py"], origin=f"http://127.0.0.1:{port}",
                                       ready_timeout=15, capture_timeout=90)))
    replay = capture_run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _no_edit}, profile=profile,
                         files={**_design_files(), "templates/index.html": FITTING_PAGE, "serve.py": serve})
    _stopped(replay, "during preview (t1)", "node_modules/.vite/dep.js")


def test_a_project_shim_never_meets_the_check_it_would_masquerade_for(tmp_path, monkeypatch):
    """Module resolution: the check imports ``probe``, frozen outside the
    project on NODE_PATH. A project ``node_modules/probe`` would be resolved
    first. The lead that places it is stopped, and the check never runs."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    frozen = tmp_path / "frozen" / "node_modules" / "probe"
    frozen.mkdir(parents=True)
    (frozen / "index.js").write_text(SHIM)
    marker = tmp_path / "resolved-from.txt"
    monkeypatch.setenv("NODE_PATH", str(frozen.parent))
    monkeypatch.setenv("PROBE_MARKER", str(marker))
    check_js = ("const fs = require('fs');\n"
                "fs.writeFileSync(process.env.PROBE_MARKER, require.resolve('probe'));\n"
                "process.exit(require('probe') === 'real' ? 0 : 1);\n")
    files = {**FILES_OK, "check.js": check_js}
    real_which = shutil.which
    lead = _lead_writes({"node_modules/probe/index.js": SHIM})
    replay = _run(tmp_path, monkeypatch, Script(lead=lead), files=files, check=shlex.join([node, "check.js"]))
    monkeypatch.setattr("shutil.which", real_which)
    _stopped(replay, "during lead (t1)", "node_modules/probe/index.js")
    assert not marker.exists(), "the check never executed against the shim"


def test_the_same_check_resolves_the_frozen_module_when_no_shim_is_placed(tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    frozen = tmp_path / "frozen" / "node_modules" / "probe"
    frozen.mkdir(parents=True)
    (frozen / "index.js").write_text(SHIM)
    marker = tmp_path / "resolved-from.txt"
    monkeypatch.setenv("NODE_PATH", str(frozen.parent))
    monkeypatch.setenv("PROBE_MARKER", str(marker))
    check_js = ("const fs = require('fs');\n"
                "fs.writeFileSync(process.env.PROBE_MARKER, require.resolve('probe'));\n"
                "process.exit(require('probe') === 'real' ? 0 : 1);\n")
    replay = _run(tmp_path, monkeypatch, Script(), files={**FILES, "check.js": check_js},
                  check=shlex.join([node, "check.js"]))
    assert replay.result.error == "" and H.gate_results(replay) == ["PASSED"]
    assert marker.read_text() == str(frozen / "index.js")


def test_a_source_only_change_with_an_unchanged_tree_proceeds(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(), files={**FILES, "node_modules/pkg/index.js": SHIM})
    assert replay.result.error == "" and H.gate_results(replay) == ["PASSED"]
    record = H.result_json(replay)["dependency_identity"]
    assert record["status"] == "unchanged" and record["roots"] == ["node_modules"] and record["events"] == []


def test_a_project_with_no_trees_proceeds_and_absence_is_its_identity(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(), files=FILES)
    assert replay.result.error == ""
    record = H.result_json(replay)["dependency_identity"]
    assert record["status"] == "unchanged" and record["roots"] == [] and record["entries"] == 0


def test_over_the_bound_at_run_start_stops_before_any_call(tmp_path, monkeypatch):
    from quadratus import deptree
    monkeypatch.setattr(deptree.DependencyGuard.__init__, "__kwdefaults__",
                        dict(deptree.DependencyGuard.__init__.__kwdefaults__, max_entries=5))
    files = {**FILES_OK, **{f"node_modules/f{i}.js": "1" for i in range(10)}}
    replay = _run(tmp_path, monkeypatch, Script(), files=files)
    assert replay.result.error.startswith("DependencyIdentityUnavailable") and "more than 5 entries" in replay.result.error
    assert replay.calls == [], "no vendor call"
    assert H.result_json(replay)["dependency_identity"]["status"] == "unverified"


def test_a_tree_changed_between_calls_stops_before_the_next_vendor_call(tmp_path, monkeypatch):
    """A change no editing call made (here, the orchestrator's own turn)
    is caught before the next editing call reaches a vendor."""
    def orchestrator(call, replay):
        (replay.project / "node_modules").mkdir(exist_ok=True)
        (replay.project / "node_modules" / "late.js").write_text(SHIM)
        return DECL_T1
    replay = _run(tmp_path, monkeypatch, Script(orchestrator=orchestrator))
    _stopped(replay, "before lead (t1)", "node_modules/late.js")
    assert not replay.of("lead")


def test_a_capped_revision_that_touched_a_tree_is_named_when_the_run_ends_at_its_task_cap(tmp_path, monkeypatch):
    """The capped task ends without another check; the run's end is one."""
    def revision(call, replay):
        H.write(call, {"node_modules/late.js": SHIM})
        return H.claude_cap("Still revising.")
    replay = _run(tmp_path, monkeypatch, Script(revision=revision), files=FILES)
    assert H.result_json(replay)["turn_limited_tasks"] == ["t1"], "the cap is still the task's outcome"
    error = replay.result.error
    assert error.startswith("DependencyTreeChanged") and "at the end of the run" in error, error
    record = H.result_json(replay)["dependency_identity"]
    assert record["events"][0]["window"] == "during revision (t1)" and not replay.result.completed


@pytest.mark.parametrize("turns", [CAP, 20], ids=["at", "above"])
@pytest.mark.parametrize("edits", [False, True], ids=["unchanged", "changed"])
def test_a_grok_gate_fix_cancelled_at_the_cap_is_the_cap(tmp_path, monkeypatch, turns, edits):
    """Codex's 90cc5d9 baseline: without a cap on the fix role, a cancelled
    Grok envelope at 14 or 20 turns was a ProviderError."""
    def gate_fix(call, replay):
        if edits:
            H.write(call, {"app.py": FIXED})
        return H.grok_ok("Fixing add next.", stop="cancelled", num_turns=turns)
    replay = _run(tmp_path, monkeypatch, Script(lead=_looked, **{"gate-fix": gate_fix}), files=FILES,
                  lead="grok:default")
    (fix,) = replay.of("gate-fix")
    assert _turns_flag(fix) == str(CAP)
    assert replay.artifacts("capped-fix"), replay.result.error
    assert H.gate_results(replay) == (["PASSED"] if edits else ["FAILED"])
    assert replay.result.error == "" if edits else not replay.result.completed


def test_a_grok_revision_cancelled_at_the_cap_takes_the_capped_task_path(tmp_path, monkeypatch):
    def revision(call, replay):
        return H.grok_ok("Still revising.", stop="cancelled", num_turns=CAP)
    replay = _run(tmp_path, monkeypatch, Script(revision=revision), files=FILES, lead="grok:default")
    assert H.result_json(replay)["turn_limited_tasks"] == ["t1"] and not replay.of("closeout")
