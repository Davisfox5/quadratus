"""Whole-run lifecycle replays, one independent case per boundary.

Each case drives ``run_project`` end to end through ``tests.lifecycle.harness``,
which fakes only the vendor CLI launch. Everything the live runs exercised is
real: argv, envelope extraction, Fleet's CHANGED check, the session lifecycle,
workers, the integration gate (real pytest on a tiny project), the design
check and the close-out. Cases are separate tests so one failure never hides
another. Fixture-neutral: a two-function app, no GameTape content.

Controller determinism, not live reliability.
"""

import json
from pathlib import Path

import pytest

from quadratus.config import Settings
from tests.lifecycle import harness as H

FILES = {
    "app.py": "def add(a, b):\n    return 0\n",
    "tests/test_app.py": "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    "README.md": "# app\n",
}
FIXED = "def add(a, b):\n    return a + b\n"
#: A passing baseline for cases whose task does not implement add, so their
#: gate result is about the case, not about a broken fixture.
FILES_OK = {**FILES, "app.py": FIXED}
T1 = dict(permitted_paths=["app.py", "tests/test_app.py"], intended_result="add works",
          acceptance=["add(1, 2) == 3"], max_lines=40)
T2 = dict(permitted_paths=["README.md"], intended_result="README documents add",
          acceptance=["README names add"], max_lines=20)
DECL_T1 = "KIND: architect complex\nSCOPE: " + json.dumps(T1) + "\nImplement add in app.py."
DECL_T2 = "KIND: docs simple\nSCOPE: " + json.dumps(T2) + "\nDocument add in README.md."
CLOSEOUT = "SUMMARY: done\nDECISIONS: none recorded\nDEAD ENDS: none"
READ = {"errand": "read", "instruction": "Where is add defined and tested?"}


def _worker_request(style):
    line = json.dumps(READ)
    return {
        "same_line": "WORKER " + line,
        "newline_json": "I will ask a worker first.\nWORKER\n" + line,
        "pretty_json": "I will ask a worker first.\nWORKER\n" + json.dumps(READ, indent=2),
        "glued": "I will ask a worker first.  WORKER " + line,
    }[style]


def _fetch_request(style, artifact):
    return {
        "same_line": "FETCH: " + artifact,
        "newline_json": "Reading the worker's full answer.\nFETCH: " + artifact,
        "pretty_json": "Reading the worker's full answer.\nFETCH: " + artifact,
        "glued": "I'll read the worker's full answer before editing.FETCH: " + artifact,
    }[style]


class Script:
    """Default replies by role; a case overrides the parts it is about."""

    def __init__(self, **overrides):
        self.overrides = overrides

    def __call__(self, call, replay):
        handler = self.overrides.get(call.role) or getattr(self, "_" + call.role.split(":")[0].replace("-", "_"),
                                                           None)
        if handler is None:
            return "No blocking findings."
        return handler(call, replay)

    def _orchestrator(self, call, replay):
        n = len(replay.of("orchestrator"))
        return DECL_T1 if n == 1 else DECL_T2

    def _lead(self, call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def _revision(self, call, replay):
        return "Nothing to change after review.\nCHANGED: []"

    def _gate_fix(self, call, replay):
        return "Nothing to change.\nCHANGED: []"

    def _recheck(self, call, replay):
        return "RESOLVED"

    def _closeout(self, call, replay):
        return CLOSEOUT

    def _worker(self, call, replay):
        return "add is defined in app.py and tested in tests/test_app.py."


def _run(tmp_path, monkeypatch, script, files=FILES, **kw):
    return H.run(tmp_path, monkeypatch, script, files=files, **kw)


def _gate_passed(replay):
    """Every recorded integration check passed, and at least one ran."""
    results = H.gate_results(replay)
    return bool(results) and set(results) == {"PASSED"}


def _read(replay, name):
    return (replay.project / name).read_text()


# -- 1. worker -> artifact FETCH -> edits -> review -> revision -> gate -> closeout -> next task

@pytest.mark.parametrize("style", ["same_line", "newline_json", "pretty_json", "glued"])
def test_the_whole_task_lifecycle_in_every_request_layout(tmp_path, monkeypatch, style):
    def lead(call, replay):
        if call.task != "t1":
            return Script()._lead(call, replay)
        n = len([c for c in replay.of("lead") if c.task == "t1"])
        if n == 1:
            return _worker_request(style)
        if n == 2:
            ids = replay.artifacts("worker:")
            assert len(ids) == 1, ids
            return _fetch_request(style, ids[0])
        assert "tested in tests/test_app.py" in call.prompt, "the fetched artifact reached the lead"
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        if call.vendor == "codex":
            return "BLOCKING: nothing tests negative numbers."
        return "No blocking findings."

    def revision(call, replay):
        old = Path(call.cwd, "tests/test_app.py").read_text()
        H.write(call, {"tests/test_app.py": old + "\n\ndef test_negative():\n    assert add(-1, -2) == -3\n"})
        return 'Added a negative-number test.\nCHANGED: ["tests/test_app.py"]'

    replay = _run(tmp_path, monkeypatch, Script(lead=lead, collaborator=collaborator, revision=revision),
                  max_tasks=2)
    assert replay.result.error == "" and _gate_passed(replay)
    assert _read(replay, "app.py") == FIXED and "test_negative" in _read(replay, "tests/test_app.py")
    assert "returns the sum" in _read(replay, "README.md")
    assert len(replay.of("worker")) == 1
    assert [c.task for c in replay.of("closeout")] == ["t1", "t2"]
    assert len(replay.of("recheck")) == 1
    assert not replay.result.completed, "a task cap is an incomplete outcome, never completion"


def test_a_quoted_request_example_in_a_delivery_is_a_draft(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task != "t1":
            return Script()._lead(call, replay)
        H.write(call, {"app.py": FIXED})
        return ('Implemented add. To read an artifact, a lead writes "Plan.FETCH: 0123456789ab".\n'
                'CHANGED: ["app.py"]')

    replay = _run(tmp_path, monkeypatch, Script(lead=lead))
    assert replay.result.error == "" and _gate_passed(replay) and _read(replay, "app.py") == FIXED
    assert len([c for c in replay.of("lead") if c.task == "t1"]) == 1


# -- 2. inherited task changes versus this call's changes ----------------------------

def test_a_revision_that_repeats_earlier_files_is_rejected(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(
        revision=lambda call, replay: 'Reviewed; nothing new.\nCHANGED: ["app.py"]'))
    assert "CHANGED report does not match" in replay.result.error
    assert _read(replay, "app.py") == FIXED, "the lead's work is preserved"
    assert H.gate_results(replay) == [], "stopped before the gate"


def test_a_revision_with_no_edits_and_an_empty_declaration_proceeds(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script())
    assert replay.result.error == "" and _gate_passed(replay)
    assert [c.task for c in replay.of("closeout")] == ["t1"]


# -- 3. design work: stale renders corrected with no new source edits ----------------

DESIGN = dict(permitted_paths=["templates/index.html", "static/style.css"], intended_result="an import button",
              acceptance=["the page shows an Import button"], max_lines=40)
DECL_DESIGN = "KIND: frontend standard\nSCOPE: " + json.dumps(DESIGN) + "\nAdd an Import button to the page."


def _design_script(fix_reply, review="APPROVED"):
    def orchestrator(call, replay):
        return DECL_DESIGN if len(replay.of("orchestrator")) == 1 else DECL_T2

    def lead(call, replay):
        if call.task != "t1":
            return Script()._lead(call, replay)
        H.write(call, {"templates/index.html": "<button id=import>Import</button>\n"})
        H.evidence(Path(call.cwd), "t1", age=H.STALE)   # the draft's renders; the revision outdates them
        return 'Added the button and captured it.\nCHANGED: ["templates/index.html"]'

    def revision(call, replay):
        H.write(call, {"static/style.css": "#import { padding: 8px; }\n"})
        return 'Styled the button.\nCHANGED: ["static/style.css"]'   # the draft's renders are now stale

    def design_fix(call, replay):
        assert "Files this task has already changed" in call.prompt
        H.evidence(Path(call.cwd), "t1", age=H.FRESH)   # fresh renders, no source edits
        return fix_reply

    return Script(orchestrator=orchestrator, lead=lead, revision=revision,
                  **{"design-fix": design_fix, "design-review": lambda call, replay: review})


def _design_files():
    return {**FILES_OK, "templates/index.html": "<p>projects</p>\n", "static/style.css": ""}


def test_stale_renders_are_recaptured_without_source_edits(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _design_script("Renders refreshed.\nCHANGED: []"),
                   files=_design_files())
    assert replay.result.error == "" and _gate_passed(replay)
    assert len(replay.of("design-fix")) == 1 and len(replay.of("design-review")) == 1
    record = json.loads(replay.artifact_texts("design-evidence")[0])
    assert record["verified"] is True and record["final_review"]["verdict"] == "APPROVED"


def test_a_design_fix_that_repeats_the_tasks_files_is_rejected(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch,
                   _design_script('Scaffold already present.\nCHANGED: ["templates/index.html", "static/style.css"]'),
                   files=_design_files())
    assert "CHANGED report does not match" in replay.result.error
    assert not replay.of("design-review")


def test_renders_of_an_unrelated_page_are_an_open_finding(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch,
                   _design_script("Renders refreshed.\nCHANGED: []",
                                  review="BLOCKING: the renders do not show the changed interface"),
                   files=_design_files())
    assert replay.result.error == "" and _gate_passed(replay) and not replay.result.completed
    record = json.loads(replay.artifact_texts("design-evidence")[0])
    # The verdict is scripted: this proves a BLOCKING verdict becomes a finding
    # and the prompt asks for the feature's state, not that a model can tell
    # an unrelated image apart. Live browser acceptance still decides that.
    assert "do not show the changed interface" in record["final_review"]["verdict"]


# -- 4. success, error and turn-cap envelopes ---------------------------------------

def _continuing(first):
    """The orchestrator names ``first``, then the continuation of a capped t1."""
    def orchestrator(call, replay):
        if len(replay.of("orchestrator")) == 1:
            return first
        return ("KIND: docs simple\nSCOPE: " + json.dumps({**T1, "permitted_paths": T1["permitted_paths"]
                                                           + ["README.md"]})
                + "\nFinish the capped work.\nCONTINUES: t1")
    return orchestrator


def _finish(call, replay):
    H.write(call, {"README.md": "# app\n\nfinished\n"})
    return 'Finished.\nCHANGED: ["README.md"]'


def test_a_claude_lead_at_its_cap_hands_back_partial_work(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return H.claude_cap("Wrote add; tests not run yet.", num_turns=14)
        return _finish(call, replay)

    replay = _run(tmp_path, monkeypatch, Script(orchestrator=_continuing(DECL_T1), lead=lead), max_tasks=2,
                  settings=Settings(backend="cli", lead_max_turns=14))
    assert replay.result.error == "" and _gate_passed(replay)
    assert _read(replay, "app.py") == FIXED, "the capped lead's edit is kept"
    assert "--max-turns" in replay.of("lead")[0].argv
    assert "CONTINUES: t1" in replay.of("orchestrator")[1].prompt or "t1" in replay.of("orchestrator")[1].prompt
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]


def test_a_claude_failure_past_the_turn_count_is_not_read_as_a_cap(tmp_path, monkeypatch):
    """An unrelated error with num_turns above the cap stays an error (818ecd0)."""
    def lead(call, replay):
        if call.task == "t1" and call.vendor == "claude":
            return H.claude_error("API Error: overloaded", num_turns=20)
        return Script()._lead(call, replay)

    replay = _run(tmp_path, monkeypatch, Script(lead=lead),
                  settings=Settings(backend="cli", lead_max_turns=14))
    assert replay.result.error.startswith("ProviderError: claude reported an error")
    assert "API Error: overloaded" in replay.result.error, "the envelope's own error text is kept"
    assert [c.role for c in replay.calls] == ["orchestrator", "lead"], "not handed back as a capped task"


def test_a_grok_lead_cancelled_at_the_cap_is_the_cap(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"README.md": "# app\n\npartial\n"})
            return H.grok_ok("I'll finish the README next", stop="cancelled", num_turns=14)
        return _finish(call, replay)

    replay = _run(tmp_path, monkeypatch, Script(orchestrator=_continuing(DECL_T2), lead=lead), max_tasks=2,
                  files=FILES_OK, settings=Settings(backend="cli", lead_max_turns=14))
    assert replay.of("lead")[0].vendor == "grok"
    assert replay.result.error == "" and _gate_passed(replay)
    assert "finished" in _read(replay, "README.md")
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]


def test_two_caps_in_a_row_stop_with_a_named_breaker_and_the_work_kept(tmp_path, monkeypatch):
    """Run 14: the breaker stopped the run and result.error was blank."""
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"README.md": "# app\n\npartial\n"})
            return H.grok_ok("I'll finish the README next", stop="cancelled", num_turns=14)
        # A zero-write discovery cap, in whichever vendor's envelope drew t2.
        if call.vendor == "grok":
            return H.grok_ok("Still reading the tests.", stop="cancelled", num_turns=14)
        return H.claude_cap("Still reading the tests.", num_turns=15)

    replay = _run(tmp_path, monkeypatch, Script(orchestrator=_continuing(DECL_T2), lead=lead), max_tasks=3,
                  files=FILES_OK, settings=Settings(backend="cli", lead_max_turns=14))
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "no third lead"
    assert not replay.result.completed
    assert replay.result.error.startswith("TurnLimitBreaker: the lead turn limit was reached 2 times in a row "
                                          "(t1, t2)")
    assert "partial" in _read(replay, "README.md"), "every capped task's edits stay in place"
    in_flight = json.loads((replay.result.run_dir / "in-flight.json").read_text())
    assert in_flight["changed"] == ["README.md"]
    assert [(r["task"], r["changed"]) for r in in_flight["turn_limited"]] == [("t1", ["README.md"]), ("t2", [])]
    result = json.loads((replay.result.run_dir / "result.json").read_text())
    assert result["error"] == replay.result.error and result["completed"] is False


def test_a_grok_error_at_the_cap_count_is_a_failure_not_a_continuation(tmp_path, monkeypatch):
    """Grok review of #33: an error field at the count was read as the cap and replanned."""
    def lead(call, replay):
        H.write(call, {"README.md": "# app\n\npartial\n"})
        return H.grok_ok("I'll finish the README next", stop="cancelled", num_turns=14, error="overloaded")

    replay = _run(tmp_path, monkeypatch, Script(orchestrator=_continuing(DECL_T2), lead=lead), max_tasks=2,
                  files=FILES_OK, settings=Settings(backend="cli", lead_max_turns=14))
    assert [c.task for c in replay.of("lead")] == ["t1"], "one lead, no continuation, no recovery"
    assert "overloaded" in replay.result.error
    assert "partial" in _read(replay, "README.md"), "the written work is preserved"


def test_a_grok_cancel_before_the_cap_is_a_failure_recovered_once_on_an_unchanged_tree(tmp_path, monkeypatch):
    """Not a cap: the unchanged tree lets another lead take the task, once."""
    def lead(call, replay):
        if call.vendor == "grok":
            return H.grok_ok("I'll create it", stop="cancelled", num_turns=3)
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    replay = _run(tmp_path, monkeypatch, Script(orchestrator=lambda c, r: DECL_T2, lead=lead),
                  files=FILES_OK, settings=Settings(backend="cli", lead_max_turns=14))
    leads = replay.of("lead")
    assert leads[0].vendor == "grok" and len(leads) == 2 and leads[1].vendor != "grok"
    assert replay.result.error == "" and _gate_passed(replay) and "returns the sum" in _read(replay, "README.md")
    assert any("lead-recovery" in k for k in replay._kinds_all())


# -- 5. vendor refusal is preserved, never retried or rerouted ------------------------

def test_a_refused_closeout_keeps_a_harness_record_and_the_run_continues(tmp_path, monkeypatch):
    def closeout(call, replay):
        return H.claude_refusal() if call.task == "t1" else CLOSEOUT

    replay = _run(tmp_path, monkeypatch, Script(closeout=closeout), max_tasks=2)
    assert replay.result.error == "" and _gate_passed(replay)
    assert [c.task for c in replay.of("closeout")] == ["t1", "t2"], "one close-out call per task, no retry"
    refused = replay.artifact_texts("closeout-refused")
    assert len(refused) == 1 and "declined" in refused[0]


def test_a_refused_lead_is_not_retried_or_rerouted(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task == "t1" and call.vendor == "claude":
            return H.claude_refusal("cyber")
        return Script()._lead(call, replay)

    replay = _run(tmp_path, monkeypatch, Script(lead=lead))
    assert replay.result.error.startswith("ProviderRefusal: claude declined the request [cyber]")
    assert [c.role for c in replay.calls] == ["orchestrator", "lead"], "no retry and no other model"
    assert _read(replay, "app.py") == FILES["app.py"]


@pytest.mark.parametrize("wrote", [False, True], ids=["unchanged", "changed"])
@pytest.mark.parametrize("turns", [5, 14, 20])
@pytest.mark.parametrize("stop", ["refusal", "content_filter"])
def test_a_grok_decline_is_one_lead_at_any_count_on_any_tree(tmp_path, monkeypatch, stop, turns, wrote):
    """Codex review of f7548a2: a decline on an unchanged tree was handed to another lead."""
    def lead(call, replay):
        if wrote:
            H.write(call, {"README.md": "# app\n\npartial\n"})
        return H.grok_ok("I can't help with that", stop=stop, num_turns=turns)

    replay = _run(tmp_path, monkeypatch, Script(orchestrator=_continuing(DECL_T2), lead=lead), max_tasks=2,
                  files=FILES_OK, settings=Settings(backend="cli", lead_max_turns=14))
    leads = replay.of("lead")
    assert [(c.vendor, c.task) for c in leads] == [("grok", "t1")], "no replacement, retry or continuation"
    assert replay.result.error.startswith(f"ProviderRefusal: grok declined the request (stopReason {stop!r})")
    assert ("partial" in _read(replay, "README.md")) is wrote, "the tree is left exactly as the call left it"


# -- 6. the freshness boundary itself, with set timestamps -----------------------------

def test_render_freshness_is_decided_by_timestamp_not_write_order(tmp_path):
    """Codex review: the matrix raced the write clock. The production check is
    unchanged; this pins its boundary with explicit mtimes."""
    import os

    from quadratus.design_evidence import check, evidence_dir
    H.evidence(tmp_path, "t1", age=0)
    since = 1_000_000.0
    for name in ("desktop", "mobile"):
        os.utime(evidence_dir(tmp_path, "t1") / name / "page.png", (since, since))
    assert check(tmp_path, "t1", since)[0], "a render at the edit's start counts"
    assert not check(tmp_path, "t1", since + 1)[0], "a render before the edit is stale"
    H.evidence(tmp_path, "t2", age=H.STALE)
    H.evidence(tmp_path, "t3", age=H.FRESH)
    import time
    now = time.time()
    assert not check(tmp_path, "t2", now)[0] and check(tmp_path, "t3", now)[0]


# -- 7. continuation sizing and test setup cost (Run 13, 2026-09-26) --------------------

def _setup_and_cases(cases):
    """A new test file: a fixed setup block, then ``cases`` one-line tests."""
    setup = ["import types", "", "", "class FakeClock:", "    def __init__(self):", "        self.now = 0",
             "", "    def tick(self, seconds):", "        self.now += seconds", "", "",
             "def fixture_env():", "    env = types.SimpleNamespace(clock=FakeClock(), log=[])",
             "    env.log.append('ready')", "    return env", "", ""]
    body = []
    for i in range(cases):
        body += [f"def test_case_{i}():", "    env = fixture_env()", f"    env.clock.tick({i})",
                 f"    assert env.clock.now == {i}", "", ""]
    return "\n".join(["from app import add", ""] + setup + body) + "\n"


def _continuation_run(tmp_path, monkeypatch, cases, estimate=60):
    extra = dict(permitted_paths=["tests/test_clock.py"], intended_result="clock cases",
                 acceptance=["tests/test_clock.py passes"], max_lines=estimate)

    def orchestrator(call, replay):
        if len(replay.of("orchestrator")) == 1:
            return DECL_T1
        return ("KIND: testing standard\nSCOPE: " + json.dumps(extra)
                + "\nFinish the capped work: the clock test cases.\nCONTINUES: t1")

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return H.claude_cap("Fixed add; clock tests not written yet.", num_turns=14)
        H.write(call, {"tests/test_clock.py": _setup_and_cases(cases)})
        return 'Added the clock tests.\nCHANGED: ["tests/test_clock.py"]'

    return _run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), max_tasks=2,
                settings=Settings(backend="cli", lead_max_turns=14))


def test_the_planner_is_told_to_count_test_setup_and_split_heavy_setup(tmp_path, monkeypatch):
    replay = _continuation_run(tmp_path, monkeypatch, cases=6)
    first, second = replay.of("orchestrator")[:2]
    assert "fixed setup cost" in first.prompt and "its own earlier" in first.prompt
    assert "count every test not yet written in full, including its" in second.prompt


def test_a_continuation_sized_for_its_tests_and_setup_proceeds(tmp_path, monkeypatch):
    replay = _continuation_run(tmp_path, monkeypatch, cases=6)       # ~55 lines against ~60
    assert replay.result.error == "" and _gate_passed(replay)
    assert "test_case_5" in _read(replay, "tests/test_clock.py")


def test_a_continuation_whose_tests_overrun_still_stops_with_work_preserved(tmp_path, monkeypatch):
    """The ceiling is unchanged: 1.5 x 60 = 90 lines, and the tests are not trimmed to fit."""
    replay = _continuation_run(tmp_path, monkeypatch, cases=14)      # ~103 lines
    assert "Task exceeded its declared scope" in replay.result.error
    assert "stopped past 90" in replay.result.error and "in tests" in replay.result.error
    assert "test_case_13" in _read(replay, "tests/test_clock.py"), "the work is preserved"


# -- 8. an interactive capture whose step failed (Codex review, Run 13) ---------------

def test_a_capture_whose_interaction_step_failed_leaves_the_design_unverified(tmp_path, monkeypatch):
    """Fresh, clean renders do not count when the step that reaches the change failed."""
    from quadratus.design_evidence import evidence_dir

    def failed_capture(call, replay):
        root = Path(call.cwd)
        H.evidence(root, "t1", age=H.FRESH)
        summary_path = evidence_dir(root, "t1") / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["steps"] = [dict(action="click", selector="#import"), dict(action="wait", selector="dialog[open]")]
        for view in summary["views"].values():
            view["steps"] = [dict(n=1, action="click", selector="#import", ok=True),
                             dict(n=2, action="wait", selector="dialog[open]", ok=False,
                                  error="Timeout 5000ms exceeded.")]
        summary_path.write_text(json.dumps(summary))
        return "Renders refreshed.\nCHANGED: []"

    script = _design_script("unused")
    script.overrides["design-fix"] = failed_capture
    replay = H.run(tmp_path, monkeypatch, script, files=_design_files())
    assert replay.result.error == "" and not replay.result.completed
    record = json.loads(replay.artifact_texts("design-evidence")[0])
    assert record["verified"] is False and "step 2 (wait dialog[open]) failed" in record["problem"]
    assert not replay.of("design-review"), "no final review of unverified renders"


def test_a_clean_render_without_steps_still_verifies(tmp_path, monkeypatch):
    """Static design tasks stay legal, so the check does not require steps.

    This is why the failed-step case above is not Run 13 coverage: Run 13
    captured no steps, and a clean no-step render still verifies. Only the
    prompt asks for steps; the cross-vendor final review and the independent
    grader decide whether the render shows the change (Grok review of #33).
    """
    replay = H.run(tmp_path, monkeypatch, _design_script("Renders refreshed.\nCHANGED: []"),
                   files=_design_files())
    record = json.loads(replay.artifact_texts("design-evidence")[0])
    summary = json.loads((Path(replay.project) / ".quadratus" / "design-evidence" / "t1"
                          / "summary.json").read_text())
    assert "steps" not in summary and not any("steps" in v for v in summary["views"].values())
    assert record["verified"] is True and len(replay.of("design-review")) == 1


# -- 9. discovery before writing (Run 14, 2026-09-26) ---------------------------------

def test_a_capped_lead_is_told_its_round_budget(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(), max_tasks=2, settings=Settings(backend="cli", lead_max_turns=14))
    for call in replay.of("lead"):
        assert "This call has at most 14 tool rounds" in call.prompt, call.vendor
        assert "finish reading by about round 4" in call.prompt and "written by about round 9" in call.prompt


def test_no_round_budget_is_stated_without_a_cap(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(), max_tasks=2)
    assert replay.of("lead") and not any("tool rounds" in c.prompt for c in replay.of("lead"))
