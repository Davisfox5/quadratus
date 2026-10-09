"""Every rule the harness enforces on a reply is a rule the reply's prompt
states (phase-4 rerun on a6c9576 and run 9, 2026-09-28: two live runs ended
on rules their orchestrator had never been shown).

An audit of every enforcement site against the prompt text found the gaps
fixed in this batch. These tests pin each statement to the constant the
parser enforces, so a bound cannot move in one place and not the other, and
they drive the real prompt builders where a statement is conditional.
"""


import pytest

from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.design_evidence import MAX_SELECTOR_CHARS, MAX_STEPS
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec
from quadratus.task_kinds import MAX_TASK_LINES
from quadratus.taskmeta import MAX_PREFACE_LINES, MAX_REQUEST_PREFACE_LINES


def _session(tmp_path, replies=("",), **config):
    it = iter(replies)
    return Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: next(it),
                   config=SessionConfig(**config))


def _orchestrator_prompt(tmp_path, **config):
    seen = []
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"),
                      lambda key, prompt, **kw: seen.append(prompt) or "DONE",
                      config=SessionConfig(**config))
    assert session.next_task() is None
    return seen[-1]


# -- orchestrator ---------------------------------------------------------------------

def test_the_kind_window_and_control_verbs_are_stated_from_the_parser_constants():
    assert f"first {MAX_PREFACE_LINES} lines" in S._KIND_REQUEST
    for verb in ("ASK:", "FETCH:", "CONSULT", "WORKER", "DONE"):
        assert verb in S._KIND_REQUEST


def test_the_scope_line_form_and_path_rules_are_stated():
    text = S._SCOPE_REQUEST
    assert "exactly one line" in text and "left margin" in text
    assert "no description after its labels is refused" in text
    for rule in ("backslashes", "leading ~", ".quadratus path", "surrounding whitespace"):
        assert rule in text
    assert f"no greater than {MAX_TASK_LINES}" in text and '"max_lines": ' + str(MAX_TASK_LINES) in text
    assert "one correction" in text and "not DONE, ASK or FETCH" in text
    assert "20,000 bytes" in text


def test_the_capture_rules_are_stated_from_the_validator_constants():
    text = S._CAPTURE_SCOPE_REQUEST
    assert f"at most {MAX_STEPS} steps" in text
    assert f"at most {MAX_SELECTOR_CHARS} characters" in text
    assert "exactly the keys path and steps" in text and "starting with /" in text
    for marker in ("KIND is frontend", "templates, static, components, pages, views, style or styles",
                   ".html, .css, .scss, .sass, .less, .jsx, .tsx, .vue or .svelte"):
        assert marker in text, marker


def test_the_ui_path_rule_the_prompt_states_is_the_one_the_harness_applies():
    """The stated directories and extensions are what _UI_PATH matches."""
    for path in ("templates/x.txt", "static/a", "components/b", "pages/c", "views/d", "style/e", "styles/f",
                 "a.html", "a.htm", "a.css", "a.scss", "a.sass", "a.less", "a.jsx", "a.tsx", "a.vue", "a.svelte"):
        assert S._UI_PATH.search(path), path
    for path in ("app.py", "docs/readme.md", "src/util.js"):
        assert not S._UI_PATH.search(path), path


def test_the_orchestrator_is_told_its_next_task_id_with_the_capture_rules(tmp_path):
    from tests.test_capture_scope_guidance import _session as capture_session
    session, seen = capture_session(tmp_path, iter(["DONE"]))
    assert session.next_task() is None
    assert "This task's id will be t1." in seen[-1]


def test_the_ask_limit_and_decide_scope_are_stated():
    assert f"At most {S._MAX_ASKS_PER_DECISION} ASKs per decision" in S._ASK_SPARINGLY
    assert "flagged AMBIGUOUS" in S._ASK_SPARINGLY


def test_the_parallel_request_carries_the_configured_limit_and_the_exclusions(tmp_path):
    text = S._parallel_request(4)
    assert "two to 4 tasks" in text
    assert "RESOLVES a finding, and a task the harness captures, are named alone" in text
    assert "its first block runs alone" in text
    import inspect
    assert "_parallel_request(self.config.max_parallel_tasks)" in inspect.getsource(S.Session.next_task)


def test_the_first_decomposition_asks_for_covers_with_the_requirements(tmp_path):
    prompt = _orchestrator_prompt(tmp_path, requirements_ledger=True)
    assert "REQUIREMENTS:" in prompt and "COVERS: R2, R5" in prompt
    assert "with its own COVERS line" in prompt


def test_map_notes_placement_is_stated():
    assert "MAP NOTES is the last section" in S._ORIENT_REQUEST
    assert "KIND, SCOPE, NEEDS, COVERS, RESOLVES and CONTINUES lines placed after it are lost" in S._ORIENT_REQUEST


def test_the_needs_catch_all_is_stated():
    assert "Any line beginning NEEDS: is read as this declaration" in S._NEEDS_REQUEST


def test_the_repeat_task_rule_is_stated_once_a_task_has_run(tmp_path):
    session = _session(tmp_path, ("DONE",))
    seen = []
    session.invoke = lambda key, prompt, **kw: seen.append(prompt) or "DONE"
    session.next_task()
    assert "identical to the one you named last round" not in seen[-1], "nothing to repeat yet"
    from quadratus.memory import TaskSummary
    session.history.append(TaskSummary(task_id="t1", author="x", summary="s", reasoning="", dead_ends=[]))
    session.next_task()
    assert "identical to the one you named last round is refused" in seen[-1]


def test_operator_path_limits_and_policy_denials_reach_the_orchestrator(tmp_path):
    from quadratus.policy import load_policy
    root = tmp_path / "project"
    root.mkdir()
    seen = []
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"),
                      lambda key, prompt, **kw: seen.append(prompt) or "DONE",
                      config=SessionConfig(project=root, allow_writes=True, requirements_ledger=False,
                                           default_scope=TaskScope(["src/**"], max_lines=50),
                                           repository_policy=load_policy(root)))
    session.next_task()
    assert "must fall under the operator's limits: src/**" in seen[-1]
    assert "The repository policy refuses writes under: .git/**, .env" in seen[-1]


def test_the_capped_summary_states_the_continues_line_form():
    """The CONTINUES regex takes the line alone; the summary says so."""
    assert S._CONTINUES.search("CONTINUES: t1") and not S._CONTINUES.search("CONTINUES: t1 and more")
    import inspect
    source = inspect.getsource(S.Session._close_turn_limited)
    assert "on its own line" in source and "with nothing else on it" in source


def test_the_findings_prompt_states_the_resolves_rules(tmp_path):
    session = _session(tmp_path)
    session.findings.append(dict(id="F1", task="t1", requirements=["R1"], view="mobile", target="/p",
                                 width=500, viewport=390, status="open", steps=[], resolution=None))
    text = session._findings_prompt()
    assert "one RESOLVES line, ids from this list only, each once" in text
    assert "UI task named alone" in text


# -- leads ----------------------------------------------------------------------------

def _lead_prompt(tmp_path, **kw):
    session = _session(tmp_path)
    spec = TaskSpec("t1", "do it", scope=TaskScope(["a.py"], acceptance=["ok"]))
    return session._lead_prompt(spec, **kw)


def test_the_lead_is_told_the_request_form_the_preface_limit_and_that_ask_is_not_a_channel(tmp_path):
    text = _lead_prompt(tmp_path, lead="claude:opus")
    assert f"at most {MAX_REQUEST_PREFACE_LINES} lines of preface" in text
    assert "carries a CHANGED line is a delivery, never a request" in text
    assert "There is no ASK channel for you" in text and "fails this task" in text


def test_the_lead_is_told_what_a_valid_worker_request_is(tmp_path):
    text = _lead_prompt(tmp_path, lead="claude:opus")
    assert "demanding is a boolean flag, not an errand" in text
    assert "nests another helper" in text and "write:true without an edit grant fails" in text


def test_a_security_lead_is_told_consults_are_unavailable(tmp_path):
    assert "Consults are not available in this task" in _lead_prompt(tmp_path, lead=None)
    assert "Consults are not available in this task" not in _lead_prompt(tmp_path, lead="claude:opus")


def test_revision_fix_and_gate_fix_prompts_say_requests_are_not_served(tmp_path):
    session = _session(tmp_path)
    spec = TaskSpec("t1", "do it", scope=TaskScope(["a.py"], acceptance=["ok"]))
    assert S._NO_REQUESTS_IN_FIX in session._revision_prompt(spec, "draft", ["[Reviewer A]\nBLOCKING: x"])
    assert S._NO_REQUESTS_IN_FIX in session._fix_prompt(spec, "draft", [("Reviewer A", "UNRESOLVED: x")])


def test_the_gate_fix_prompt_carries_the_changed_rule_and_no_requests(tmp_path):
    """The real gate: a check that fails until the fix call writes a marker."""
    from quadratus.integration import GateCommand, GateSuite
    from quadratus.memory import TaskMemory
    from tests.test_gate_attribution import DECLARED
    root = tmp_path / "project"
    root.mkdir()
    (root / "test_app.py").write_text("def test_a():\n    assert 1 == 2\n")
    prompts = []

    def invoke(key, prompt, allow_writes=False):
        prompts.append(prompt)
        (root / "test_app.py").write_text("def test_a():\n    assert 1 == 1\n")
        return 'Fixed.\nCHANGED: ["test_app.py"]'
    gate = GateSuite([GateCommand("unit", argv=tuple(DECLARED))], cwd=root)
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), invoke,
                      config=SessionConfig(project=root, allow_writes=True, integration_gate=gate))
    spec = TaskSpec("t1", "do it", scope=TaskScope(["test_app.py"], acceptance=["ok"]))
    session._active_spec = spec
    session._task_before = {}
    session._run_integration_gate("claude:opus", spec, TaskMemory("t1", "claude:opus", session.store))
    fix = next(p for p in prompts if "integration check failed" in p)
    assert "lists only files this call itself" in fix and S._NO_REQUESTS_IN_FIX in fix
    assert session.checks[-1]["passed"]


# -- judges ---------------------------------------------------------------------------

def test_the_verifier_is_told_capitalised_markers_count_in_prose(tmp_path):
    session = _session(tmp_path)
    text = session._verifier_prompt(TaskSpec("t1", "do it"), "draft", "claude:opus")
    assert "in capitals are read as findings wherever they appear" in text
    assert "'BLOCKING: none' is read as no finding" in text
    assert S._has_security_finding("This defect is BLOCKING.") and not S._has_security_finding("nothing blocking here")


def test_the_json_verdict_instruction_states_every_structured_rule(tmp_path):
    from quadratus.memory import TaskMemory
    session = _session(tmp_path, security_verdict_json=True)
    prompts = []
    session._invoke_model = lambda key, prompt, **kw: prompts.append(prompt) or "not json"
    spec = TaskSpec("t1", "harden it", scope=TaskScope(["a.py"], acceptance=["no secrets logged"]))
    session._verify_security_json(spec, TaskMemory("t1", "openai:gpt-5.6-sol", session.store), "draft",
                                  "openai:gpt-5.6-sol", "claude:opus")
    text = prompts[0]
    for rule in ("This JSON replaces the BLOCKING: lines", "echoes the hash below exactly", "copied verbatim",
                 "A reject must name a blocking finding or a failed criterion", "no duplicate keys",
                 "no second try"):
        assert rule in text, rule


def test_the_auditor_is_told_which_citations_resolve():
    import inspect
    source = inspect.getsource(S.Session._audit_requirements) if hasattr(S.Session, "_audit_requirements") \
        else inspect.getsource(S.Session)
    assert "test function named test_* defined in a file" in source


def test_the_blocked_report_rule_states_what_the_harness_does():
    assert "is read as unverified by whoever checks the work" in S._BLOCKED_REPORT_RULE
    assert "is rejected" not in S._BLOCKED_REPORT_RULE


def test_the_capture_command_limits_are_stated_from_the_constants():
    assert f"At most {MAX_STEPS} steps" in S._DESIGN_RENDER_SHOWS
    assert f"at most {MAX_SELECTOR_CHARS} characters" in S._DESIGN_RENDER_SHOWS
    assert "--file takes SELECTOR=PATH" in S._DESIGN_RENDER_SHOWS


def test_the_validator_and_the_capture_command_share_one_selector_bound():
    from quadratus.preview import validate_capture
    long = "a" * (MAX_SELECTOR_CHARS + 1)
    with pytest.raises(ValueError):
        validate_capture({"path": "/p", "steps": [{"action": "click", "selector": long}]})
    with pytest.raises(ValueError, match=f"at most {MAX_STEPS} steps"):
        validate_capture({"path": "/p", "steps": [{"action": "click", "selector": "a"}] * (MAX_STEPS + 1)})


# -- workers --------------------------------------------------------------------------

def test_workers_are_told_they_cannot_hire_consult_fetch_or_continue():
    from quadratus.workers import capability_preamble
    for key, writes in (("claude:haiku", False), ("openai:luna", True), ("grok:worker", False)):
        text = capability_preamble(writes, key)
        assert "You cannot hire, consult or fetch" in text
        assert "one NEED TOOL per errand" in text
        assert "CONTINUE: is refused" in text


def test_the_bounded_editor_role_does_not_offer_lead_channels():
    import inspect

    from quadratus import runtime
    source = inspect.getsource(runtime.Fleet._generate_in_copy) if hasattr(runtime.Fleet, "_generate_in_copy") \
        else inspect.getsource(runtime)
    assert "may be returned alone before the patch" not in source
    assert "cannot hire, consult or fetch" in source
