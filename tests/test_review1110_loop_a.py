"""Regression tests for the whole-repo review of 2026-11-10, workstream loop_a:
the Session's per-call plumbing, the lead channels and ``next_task``.

One section per finding, named by report and item. Offline: every model is a
scripted reply.
"""

import json
import re
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.delegation import invocation_context
from quadratus.memory import TaskMemory
from quadratus.providers import TurnLimitReached
from quadratus.scope import TaskScope
from quadratus.session import (
    Complexity,
    RunStalled,
    Session,
    SessionConfig,
    TaskFailed,
    TaskListInvalid,
    TaskSpec,
)
from quadratus.workers import WorkerBudget
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FIXED, T2, Script
from tests.test_session import Recorder

OPUS = "claude:opus"
README_TASK = "KIND: docs simple\nSCOPE: " + json.dumps(T2) + "\nDocument add in README.md."


def _worker_origin() -> bool:
    return (invocation_context.get() or {}).get("origin") == "worker"


# -- session first half 1: every WORKER request field is read before dispatch ---------

def _channel(tmp_path, *, worker="worker evidence", **config):
    seen = []

    def invoke(key, prompt, **kw):
        seen.append(prompt)
        return worker
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), invoke, config=SessionConfig(**config))
    task = TaskMemory("t1", OPUS, session.store)
    state = dict(failures=0, failed_errands={"read-1"}, closed=None, handoffs=[])
    return session, task, state, seen


def _serve(session, task, state, request):
    return session._serve_worker("WORKER " + json.dumps(request), OPUS, TaskSpec("t1", "task"), task, state)


READ = {"errand": "read", "instruction": "Where is add defined?"}
HELPER = {"errand": "check", "instruction": "Is add tested?"}


@pytest.mark.parametrize("extra", [
    {"steps": "2"}, {"steps": 1.5}, {"steps": True}, {"steps": [2]},
    {"token_limit": "many"}, {"token_limit": 2.5}, {"token_limit": False},
    {"retry_of": ["read-1"]}, {"retry_of": {"label": "read-1"}}, {"retry_of": 7},
    {"needs": "patch"}, {"needs": [1]}, {"errand": ["read"]}, {"errand": {"a": 1}},
    {"retry_of": ["read-1"], "helper": HELPER},
    {"retry_of": "read-1", "helper": {**HELPER, "errand": ["check"]}},
    {"retry_of": "read-1", "helper": {**HELPER, "needs": "patch"}},
    {"retry_of": "read-1", "helper": {**HELPER, "steps": "2"}},
    {"retry_of": "read-1", "helper": {**HELPER, "token_limit": [9]}},
    {"retry_of": "read-1", "helper": [HELPER]},
])
def test_a_mistyped_worker_field_fails_the_task_and_never_leaves_as_a_bare_error(tmp_path, extra):
    """Each of these reached the pool unread and came back as a ValueError or
    TypeError, which is an unknown failure and stops the whole run."""
    session, task, state, seen = _channel(tmp_path)
    with pytest.raises(TaskFailed) as stopped:
        _serve(session, task, state, {**READ, **extra})
    assert stopped.value.cause == "channel"
    assert seen == [] and session.workers.spawned("t1") == 0, "refused before any worker call"


@pytest.mark.parametrize("extra, said", [
    ({"instruction": "\ud800 look at app.py"}, "instruction holds an unpaired"),
    ({"instruction": "look\x00 at app.py"}, "instruction holds a NUL"),
    ({"needs": ["\udfff"]}, "needs holds an unpaired"),
    ({"needs": ["pat\x00ch"]}, "needs holds a NUL"),
    ({"retry_of": "read-\ud800"}, "retry_of holds an unpaired"),
    ({"retry_of": "read-1", "helper": {**HELPER, "instruction": "\ud800 is add tested?"}}, "sibling helper"),
    ({"retry_of": "read-1", "helper": {**HELPER, "instruction": "is add tested?\x00"}}, "sibling helper"),
])
def test_text_no_prompt_can_carry_fails_the_task_before_the_pool_reads_it(tmp_path, extra, said):
    """Found in review of the fix: a JSON "\\ud800" escape decodes to a lone
    surrogate, and the pool's hash of the instruction raised
    UnicodeEncodeError, a ValueError that left the channel and stopped the
    run. A NUL cannot be passed in argv, which is how a worker seat gets its
    prompt. The message names the field and never echoes the text."""
    session, task, state, seen = _channel(tmp_path)
    with pytest.raises(TaskFailed) as stopped:
        _serve(session, task, state, {**READ, **extra})
    assert stopped.value.cause == "channel" and said in str(stopped.value)
    str(stopped.value).encode("utf-8")
    assert seen == [] and session.workers.spawned("t1") == 0, "refused before any worker call"


def test_json_nested_too_deep_to_read_is_a_malformed_request(tmp_path):
    session, task, state, seen = _channel(tmp_path)
    with pytest.raises(TaskFailed) as stopped:
        session._serve_worker("WORKER " + "[" * 100_000, OPUS, TaskSpec("t1", "task"), task, state)
    assert stopped.value.cause == "channel" and seen == []


@pytest.mark.parametrize("extra, said", [
    ({"steps": 5}, "steps must be 1 to 3 and token_limit 1 to 50000"),
    ({"steps": 0}, "steps must be 1 to 3"),
    ({"steps": -1}, "steps must be 1 to 3"),
    ({"token_limit": 50001}, "token_limit 1 to 50000"),
    ({"token_limit": 0}, "token_limit 1 to 50000"),
    ({"needs": ["telepathy"]}, "unknown need(s): telepathy"),
    ({"retry_of": "read-1", "helper": {**HELPER, "steps": 9}}, "steps must be 1 to 3"),
    ({"retry_of": "read-1", "helper": {**HELPER, "needs": ["telepathy"]}}, "unknown need(s): telepathy"),
])
def test_a_limit_or_label_the_pool_would_not_serve_is_a_refusal_the_lead_can_act_on(tmp_path, extra, said):
    session, task, state, seen = _channel(tmp_path)
    out = _serve(session, task, state, {**READ, **extra})
    assert len(out) == 1 and "was refused" in out[0] and said in out[0], out
    assert state["failures"] == 1, "it counts as a failed errand, so repeating it is bounded"
    assert seen == [] and session.workers.spawned("t1") == 0, "refused before any attempt is reserved"


def test_the_refusal_follows_the_configured_worker_budget(tmp_path):
    session, task, state, seen = _channel(tmp_path, worker_budget=WorkerBudget(max_worker_steps=1,
                                                                             max_worker_tokens=900))
    out = _serve(session, task, state, {**READ, "steps": 2})
    assert "steps must be 1 to 1 and token_limit 1 to 900" in out[0] and seen == []
    served = _serve(session, task, state, {**READ, "instruction": "Read app.py.", "steps": 1, "token_limit": 900})
    assert "worker evidence" in served[0] and len(seen) == 1


def test_fields_inside_the_allowance_and_null_limits_are_served(tmp_path):
    session, task, state, seen = _channel(tmp_path)
    out = _serve(session, task, state, {**READ, "steps": 2, "token_limit": 1000, "needs": []})
    assert "worker evidence" in out[0] and state["failures"] == 0
    out = _serve(session, task, state, {**READ, "instruction": "Read the tests.", "steps": None,
                                        "token_limit": None, "retry_of": None})
    assert "worker evidence" in out[0] and session.workers.spawned("t1") == 2


def test_a_refused_limit_reaches_the_lead_which_then_drafts(tmp_path):
    prompts = []

    def invoke(key, prompt, **kw):
        if _worker_origin():
            pytest.fail("no worker runs for a refused request")
        prompts.append(prompt)
        if len(prompts) == 1:
            return "WORKER " + json.dumps({**READ, "steps": 5})
        return "complete draft"
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), invoke)
    task = TaskMemory("t1", OPUS, session.store)
    assert session._draft_with_channels(OPUS, TaskSpec("t1", "task"), task) == "complete draft"
    assert "was refused: steps must be 1 to 3" in prompts[1]


def test_the_lead_prompt_states_the_field_rules_the_worker_channel_enforces(tmp_path):
    """Prompt parity: the types that fail the task, and the bounds that are
    refused, quoted from the budget the pool enforces."""
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(worker_budget=WorkerBudget(max_worker_steps=2, max_worker_tokens=7000)))
    text = session._lead_prompt(TaskSpec("t1", "do it", scope=TaskScope(["a.py"], acceptance=["ok"])), lead=OPUS)
    assert "steps and token_limit, when given, are whole numbers and retry_of is a string" in text
    assert "no string holds a NUL or an unpaired \\u escape" in text
    assert "Optional steps (1 to 2) and token_limit (1 to 7000 reported tokens)" in text
    assert "is refused before any call and counts as a failed errand" in text
    assert "the configured budget may be stricter" not in text, "the bound is stated, not hinted at"


# -- session first half 4: a capped verifier or security fix on the JSON path ---------

def _json_verdict(snapshot, decision="accept"):
    return json.dumps(dict(
        schema_version=1, verdict=decision, snapshot_hash=snapshot,
        acceptance_results=[dict(criterion="tenant isolation", status="passed",
                                 evidence="fixture test excludes the other tenant")],
        blocking_findings=["missing filter"] if decision == "reject" else [], limitations=[]))


def _security(tmp_path, verifier, *, json_verdict=True, fix=None):
    """A security task whose verifier and security-fix replies are scripted;
    either may be a TurnLimitReached to raise."""
    (tmp_path / "app.py").write_text("x = 1\n")
    calls, verdicts = [], list(verifier)

    def invoke(model, prompt, **kw):
        if "verifying security work" in prompt:
            calls.append("verifier")
            answer = verdicts.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return _json_verdict(re.search(r"Snapshot hash: ([0-9a-f]+)", prompt)[1], answer) \
                if json_verdict else answer
        if "These blocking findings remain unresolved" in prompt:
            calls.append("fix")
            if isinstance(fix, Exception):
                raise fix
            return fix or "Fixed the filter."
        return "SUMMARY: reviewed\nREASONING: fixture\nDEAD ENDS: none"
    session = Session("check isolation", ArtifactStore(tmp_path / ".quadratus" / "artifacts"), invoke,
                      config=SessionConfig(project=tmp_path, security_verdict_json=json_verdict))
    summary = session.run_task(TaskSpec("security-1", "Review tenant isolation", kind="security",
                                        scope=TaskScope(acceptance=["tenant isolation"])))
    return session, calls, summary


def _cap(text="Still reading the middleware."):
    return TurnLimitReached("stopped at its turn limit", partial_text=text, turns=20)


@pytest.mark.parametrize("json_verdict", [True, False])
def test_a_capped_verifier_gives_no_verdict_on_either_protocol_and_the_run_goes_on(tmp_path, json_verdict):
    """The JSON path let the cap leave run_task, which ends the run; the prose
    path recorded it. One helper asks the verifier for both now."""
    session, calls, summary = _security(tmp_path, [_cap()], json_verdict=json_verdict)
    assert summary is not None and summary.task_id == "security-1", "the task closed; the run was not stopped"
    outcome = session.task_outcomes[-1]
    assert "verification" in outcome.stages and "verification" not in outcome.edges, "no verdict: edge unset"
    assert outcome.closed_as == "closed"
    capped = [f for f in session.open_findings if "did not finish within its turn cap" in f]
    assert len(capped) == 1 and "verification by" in capped[0]
    assert calls.count("verifier") == 1 and "fix" not in calls, "a cap buys no second ask and no fix"
    kept = [session.store.get(ref) for ref in session.store.ids()]
    assert any(text.startswith("UNFINISHED: the verification stopped") for text in kept)
    assert not any("Still reading the middleware" in f for f in session.open_findings), \
        "the capped narration is never read as a finding"


def test_a_capped_security_fix_is_one_round_spent_and_the_verifier_still_decides(tmp_path):
    session, calls, summary = _security(tmp_path, ["reject", "accept"], fix=_cap("Editing the filter."))
    assert calls == ["verifier", "fix", "verifier"], calls
    assert session.task_outcomes[-1].edges["verification"] is True
    assert session.open_findings == [] and session._gate_fixes_used == 1
    kept = [session.store.get(ref) for ref in session.store.ids()]
    assert any("security-fix stopped at the lead turn limit" in text for text in kept)


def test_a_capped_security_fix_does_not_turn_a_second_reject_into_a_pass(tmp_path):
    session, calls, _ = _security(tmp_path, ["reject", "reject"], fix=_cap())
    assert session.task_outcomes[-1].edges["verification"] is False
    assert any(f.startswith("Security verification reject") for f in session.open_findings)


def test_a_reject_whose_fix_was_not_verified_again_stays_on_the_record(tmp_path):
    """Round two capped: no verdict on the fixed work, so the edge is unset
    and the reject the fix answered is kept as the specific unverified defect."""
    session, calls, summary = _security(tmp_path, ["reject", _cap()])
    assert calls == ["verifier", "fix", "verifier"] and summary is not None
    outcome = session.task_outcomes[-1]
    assert "verification" not in outcome.edges
    standing = [f for f in session.open_findings if "not verified again after the fix (verifier capped)" in f]
    assert len(standing) == 1 and "missing filter" in standing[0]
    assert any("did not finish within its turn cap" in f for f in session.open_findings)


# -- session first half 10: the pre-launch list check is the per-task parse -----------

def _listing(tmp_path, *, editing=True, **config):
    root = tmp_path / "project"
    root.mkdir(parents=True)

    def invoke(*a, **k):
        raise AssertionError("the list check makes no model call")
    return Session("goal", ArtifactStore(tmp_path / "artifacts"), invoke,
                   config=SessionConfig(project=root if editing else None, allow_writes=editing, **config))


def test_a_bad_needs_line_in_a_later_text_is_found_before_anything_runs(tmp_path):
    session = _listing(tmp_path)
    problems = session._task_list_problems([README_TASK, README_TASK + "\nNEEDS: telepathy",
                                            README_TASK + '\nNEEDS: ["telepathy"]'])
    assert [index for index, _ in problems] == [2, 3]
    # The verdict-reader change names the offending line before the rule.
    assert problems[0][1].startswith("invalid task requirements: ")
    assert "NEEDS must be a JSON list" in problems[0][1]
    assert "invalid task requirements: NEEDS allows only execute, patch, and direct-write" in problems[1][1]


def test_an_empty_description_and_a_parallel_block_are_found_before_anything_runs(tmp_path):
    session = _listing(tmp_path, editing=False)
    session._parallel_enabled = lambda: True
    batch = "PARALLEL\nKIND: docs simple\nDocument add.\n---\nKIND: docs simple\nDocument sub."
    problems = session._task_list_problems(["KIND: docs simple\nDocument add.", "KIND: docs simple\n",
                                            "KIND: docs simple\nMAP NOTES:\n- layout: app.py holds add", batch])
    assert problems == [(2, "no task description"), (3, "no task description"),
                        (4, "a parallel batch is not a listed task; list each task on its own")]


LISTED = [
    README_TASK,
    "DONE",
    "KIND: docs simple\nKIND: testing complex\nSCOPE: " + json.dumps(T2) + "\nDocument add.",
    "KIND: docs simple\nSCOPE: {not json}\nDocument add.",
    "KIND: docs simple\nDocument add.",
    README_TASK + "\nNEEDS: telepathy",
    README_TASK + '\nNEEDS: ["execute"]',
    "REQUIREMENTS:\nR1: add is documented\n" + README_TASK,
    "DECIDE: R1 - the wider reading\n" + README_TASK,
    README_TASK + "\nMAP NOTES:\n- docs: README is the only doc",
]


@pytest.mark.parametrize("text", LISTED)
def test_the_dry_run_names_exactly_what_the_task_turn_would_refuse(tmp_path, text):
    """One parse for both: the check cannot pass a text its own turn refuses,
    or refuse one its turn accepts."""
    checking = _listing(tmp_path / "a")
    found = checking._task_list_problems([text])
    running = _listing(tmp_path / "b")
    running._explicit_tasks, running.explicit = [text], dict(listed=1, ran=[], invalid=None)
    try:
        spec = running._next_explicit_task()
    except TaskListInvalid as exc:
        assert found == [(1, str(exc).removeprefix("listed task 1: "))]
    else:
        assert found == [] and spec.description
        dry, problem = checking._read_listed(checking._split_requirements(text)[1], "t1", dry_run=True)
        assert problem == "" and (dry.description, dry.kind, dry.needs, dry.scope) == (
            spec.description, spec.kind, spec.needs, spec.scope)


@pytest.mark.requirements_ledger
def test_the_dry_run_absorbs_nothing_and_asks_nobody(tmp_path):
    from quadratus.codebase_map import CodebaseMap

    class Decider:
        def decide(self, decision):
            raise AssertionError("the list check must not ask the decider")
    notes = CodebaseMap(tmp_path / "map.jsonl")
    session = _listing(tmp_path, codebase_map=notes, decider=Decider())
    said = []
    session._note = said.append
    text = ("REQUIREMENTS:\nR1: add is documented\nSCOPE: " + json.dumps(T2)
            + "\nDocument add in README.md.\nCOVERS: R1\nMAP NOTES:\n- docs: README is the only doc")
    assert session._task_list_problems([text]) == []
    assert len(notes) == 0 and session.memory.ledger.requirements == {}
    assert session.decisions == [] and said == []


@pytest.mark.parametrize("reply", [
    "", "Document add.", "REQUIREMENTS:\nR1: one\nR2: two\nKIND: docs simple\nDo it.",
    "DECIDE: R1 - the wider reading", "DECIDE: R1 - wide\nKIND: docs simple\nDo it.",
    "REQUIREMENTS:\nnothing numbered\nKIND: docs simple\nDo it.",
    "KIND: docs simple\nDo it.\nREQUIREMENTS:\nR1: late block\n",
])
def test_the_requirements_split_is_the_one_absorb_applies(tmp_path, reply):
    session = _listing(tmp_path)
    assert session._split_requirements(reply)[1] == session._absorb_requirements(reply)


def test_a_bad_later_text_stops_a_listed_run_before_any_call(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, max_tasks=4, record_complete=False,
                   tasks=[README_TASK, README_TASK.replace("Document add", "Describe add") + "\nNEEDS: telepathy"])
    assert replay.result.error.startswith("TaskListInvalid: listed task 2: invalid task requirements"), \
        replay.result.error
    assert replay.calls == [], "the first task must not run before the second is refused"


# -- session first half 11: a reply the scope check stops is still kept ---------------

NOT_RUN = "NOT RUN: tests/test_app.py - no interpreter in this sandbox"


def test_a_reply_stopped_by_the_scope_check_keeps_its_not_run_and_changed_record(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"app.py": FIXED, "README.md": "# rewritten\n"})
        return f'Did both.\n{NOT_RUN}\nCHANGED: ["app.py"]'

    def orchestrator(call, replay):
        return DECL_T1 if len(replay.of("orchestrator")) == 1 else "DONE"
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), files=FILES, max_tasks=3)
    task = next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")
    assert task["closed_as"] == "failed", "the scope overrun still fails the task"
    assert any(NOT_RUN in text for text in replay.artifact_texts("monitor-reply")), "the reply is kept"
    reported = H.result_json(replay)["unexecuted_acceptance"]
    assert [(e["task"], e["role"], e["item"]) for e in reported] == [("t1", "lead", "tests/test_app.py")]
    assert any(NOT_RUN in text for text in replay.artifact_texts("changed-report-mismatch"))
    assert any(f["kind"] == "unverified" and "CHANGED line" in f["detail"] for f in task["facts"])


def test_a_bounded_editor_stopped_by_the_scope_check_keeps_its_reply_but_reports_nothing(tmp_path):
    """A worker's reply is kept for the monitor; NOT RUN lines are read from
    the lead's own editing calls only, as on the path that returns."""
    root = tmp_path / "project"
    root.mkdir(parents=True)
    (root / "app.py").write_text("x = 1\n")

    def invoke(key, prompt, allow_writes=False):
        (root / "other.py").write_text("y = 2\n")
        return f"patched\n{NOT_RUN}"
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), invoke,
                      config=SessionConfig(project=root, allow_writes=True))
    spec = TaskSpec("t1", "edit app", scope=TaskScope(["app.py"], max_lines=10))
    session._active_spec, session._task_before = spec, session._capture_source()
    session._task_memory = TaskMemory("t1", OPUS, session.store)
    from quadratus.delegation import invocation
    with invocation("t1", "worker:code-1", "worker"), pytest.raises(TaskFailed):
        session._invoke_model_call("claude:haiku", "write it", allow_writes=True)
    replies = [session.store.get(ref) for ref in session.store.ids()
               if any(r.kind == "monitor-reply" for r in session.store.refs(ref))]
    assert replies == [f"patched\n{NOT_RUN}"]
    assert session.unexecuted_acceptance == []


def test_a_reply_stopped_by_the_repeated_environment_failure_keeps_its_not_run_lines(tmp_path):
    """The same order one level up: the editing call returned, then the
    environment-repeat stop failed the task before its reply was read."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True))
    session._active_spec = TaskSpec("t1", "edit app", scope=TaskScope(["app.py"], max_lines=10))
    replies = iter(["NOT RUN: tests/a.py - pytest is missing", "NOT RUN: tests/b.py - pytest is still missing"])

    def call(key, prompt, allow_writes=False):
        session._last_tool_failures = [dict(command="pytest", exit_code=127)]
        return next(replies)
    session._invoke_model = call
    session._edit(OPUS, "fix it", role="revision")
    with pytest.raises(TaskFailed) as stopped:
        session._edit(OPUS, "fix it again", role="revision")
    assert stopped.value.cause == "environment"
    assert [e["item"] for e in session.unexecuted_acceptance] == ["tests/a.py", "tests/b.py"]


# -- session first half 15: one path matcher for policy denials ----------------------

def _direct(tmp_path, policy):
    from quadratus.integration import IntegrationGate
    root = tmp_path / "unit"
    root.mkdir(parents=True, exist_ok=True)
    return Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                   config=SessionConfig(project=root, allow_writes=True, direct_tier=True,
                                        integration_gate=IntegrationGate(["true"], cwd=root),
                                        repository_policy=policy))


def test_policy_denials_are_read_by_the_scope_matcher_and_honour_forbid(tmp_path):
    policy = SimpleNamespace(forbid=("docs/private.md", "vault"), document={"capability_policy": {
        "deny_write": ["secrets", "**/.env", ".git/**"], "sensitive": [{"paths": ["config/keys"]}]}})
    session = _direct(tmp_path, policy)
    for path in ("secrets/key.pem", "secrets", ".env", "deploy/.env", ".git/hooks/pre-commit",
                 "config/keys/prod.pem", "docs/private.md", "vault/a/b.txt", "./secrets/key.pem"):
        assert session._policy_denies(path), path
    for path in ("app.py", "secretsauce.py", "docs/public.md", "env", "config/keysmith.py"):
        assert not session._policy_denies(path), path
    scope = TaskScope(forbidden_paths=("secrets", "**/.env", ".git/**", "config/keys", "docs/private.md", "vault"))
    for path in ("secrets/key.pem", ".env", "app.py", "env", "vault/x", "docs/public.md"):
        assert session._policy_denies(path) == (not scope.permits(path)), path


def test_a_loaded_policy_denies_exactly_what_its_own_scope_forbids(tmp_path):
    from quadratus.policy import load_policy
    root = tmp_path / "unit"
    root.mkdir()
    policy = load_policy(root, forbid=("notes", "docs/private.md"))
    session = _direct(tmp_path, policy)
    paths = ["app.py", ".env", ".env.local", ".git/config", ".quadratus/runs/x", "notes/todo.md",
             "docs/private.md", "docs/public.md", "notesy.py"]
    assert [session._policy_denies(p) for p in paths] == [not policy.scope(None).permits(p) for p in paths]
    assert session._policy_denies("notes/todo.md") and session._policy_denies("docs/private.md")
    assert not session._policy_denies("app.py")


def test_direct_admission_refuses_a_path_the_scope_check_would_refuse(tmp_path):
    policy = SimpleNamespace(document={"capability_policy": {"deny_write": ["secrets", "**/.env"],
                                                             "sensitive": []}})
    session = _direct(tmp_path, policy)

    def refusal(*paths):
        return session._direct_refusal(TaskSpec("t1", "Rotate the key.", tier="direct",
                                                scope=TaskScope(list(paths), max_lines=10)))
    assert refusal("app.py") == ""
    assert refusal("app.py", "secrets/key.pem") == "secrets/key.pem is a policy-denied or sensitive path"
    assert refusal(".env") == ".env is a policy-denied or sensitive path"


# -- session first half 16: the review edge needs a complete review -------------------

def _reviewed(store, review):
    def invoke(model, prompt, system=None):
        if "contributing an independent read" in prompt:
            if isinstance(review, Exception):
                raise review
            return review
        if "The task is finished" in prompt:
            return "SUMMARY: built it\nREASONING: simplest\nDEAD ENDS: none"
        return "RESOLVED" if "Check only your BLOCKING findings" in prompt else f"[{model}] output"
    session = Session("Build a parser", store, invoke)
    session.run_task(TaskSpec("t1", "normal thing", complexity=Complexity.STANDARD))
    return session, session.task_outcomes[-1]


@pytest.mark.parametrize("review, edge", [
    ("NO FINDINGS", True),
    ("BLOCKING: the parser drops escapes", True),
    ("Looks plausible to me.\nREVIEW: COMPLETE", True),
    ("I read half of it and it seems fine so far.", False),
    (TurnLimitReached("capped", partial_text="BLOCKING: I was about to", turns=20), False),
])
def test_the_review_edge_is_true_only_for_a_complete_review(tmp_path, review, edge):
    session, outcome = _reviewed(ArtifactStore(tmp_path / "artifacts"), review)
    assert outcome.edges["review"] is edge
    if not edge:
        # The record changes; what blocks completion does not.
        assert any("without a verdict" in f or "did not finish within its turn cap" in f
                   for f in session.open_findings)
        assert outcome.primary == "unverified" and "review" not in outcome.unsatisfied()


def test_a_task_that_draws_no_peer_keeps_a_not_applicable_review_edge(tmp_path):
    session = Session("Build a parser", ArtifactStore(tmp_path / "artifacts"), Recorder())
    session.run_task(TaskSpec("t1", "trivial thing", complexity=Complexity.SIMPLE))
    assert session.task_outcomes[-1].edges["review"] is None


# -- session second half 7: the scope-correction retry is read as a first reply -------

BAD_SCOPE = "KIND: docs simple\nSCOPE: {not json}\nDocument add."


def _planning(tmp_path, replies, **config):
    root = tmp_path / "project"
    root.mkdir(parents=True)
    prompts, script = [], list(replies)

    def invoke(key, prompt, **kw):
        if "Compare the list with the goal" in prompt:
            return "COMPLETE"
        prompts.append(prompt)
        return script.pop(0)
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), invoke,
                      config=SessionConfig(project=root, allow_writes=True, **config))
    return session, prompts


def test_an_unroutable_scope_correction_is_a_stall_not_a_raw_parser_error(tmp_path):
    session, prompts = _planning(tmp_path, [
        BAD_SCOPE, "KIND: docs simple\nKIND: testing complex\nSCOPE: " + json.dumps(T2) + "\nDocument add."])
    with pytest.raises(RunStalled, match="could not label this task even after a correction"):
        session.next_task()
    assert len(prompts) == 2 and "CORRECTION REQUIRED" in prompts[1]


@pytest.mark.requirements_ledger
def test_a_scope_correction_carrying_requirements_and_a_decision_is_absorbed(tmp_path):
    corrected = ("REQUIREMENTS:\nR1: add is documented\nR2: add is tested\n"
                 "DECIDE: R1 - the README is the document\n" + README_TASK + "\nCOVERS: R1")
    session, prompts = _planning(tmp_path, [BAD_SCOPE, corrected], requirements_ledger=True)
    spec = session.next_task()
    assert session.memory.ledger.requirements == {"R1": "add is documented", "R2": "add is tested"}
    assert "REQUIREMENTS" not in spec.description and "DECIDE" not in spec.description
    assert "R2: add is tested" not in spec.description, "the block is not left in the lead's task text"
    assert spec.description.startswith("Document add in README.md.")


def test_a_valid_scope_correction_still_becomes_the_task(tmp_path):
    session, prompts = _planning(tmp_path, [BAD_SCOPE, README_TASK])
    spec = session.next_task()
    assert spec.description == "Document add in README.md." and spec.scope.permitted_paths == ["README.md"]
    with pytest.raises(RunStalled, match="not a control reply"):
        _planning(tmp_path / "again", [BAD_SCOPE, "DONE"])[0].next_task()


# -- session second half 13: the operator is never asked an unusable question ---------

def _asking(tmp_path, replies):
    rec, asked = Recorder(next_tasks=replies), []

    def answer(question):
        asked.append(question)
        return f"answer-{len(asked)}"
    session = Session("Build a parser", ArtifactStore(tmp_path / "artifacts"), rec,
                      config=SessionConfig(ask_operator=answer))
    return session, rec, asked


def test_the_third_answer_is_read_by_a_further_seat_call(tmp_path):
    session, rec, asked = _asking(tmp_path, ["ASK: q1?", "ASK: q2?", "ASK: q3?", "use the third answer"])
    spec = session.next_task()
    assert spec.description == "use the third answer"
    assert asked == ["q1?", "q2?", "q3?"] and len(session.memory.ledger.rulings) == 3
    assert "answer-3" in rec.calls[-1]["prompt"], "the call that named the task had the last ruling in hand"


def test_a_question_past_the_limit_stalls_without_being_put_to_the_operator(tmp_path):
    session, rec, asked = _asking(tmp_path, ["ASK: q1?", "ASK: q2?", "ASK: q3?", "ASK: q4?"])
    with pytest.raises(RunStalled, match="asked the operator 3 questions"):
        session.next_task()
    assert asked == ["q1?", "q2?", "q3?"], "q4 would have had no call left to use its answer"
    assert len(rec.calls) == 4 and "answer-3" in rec.calls[3]["prompt"], "every answer given was shown to a call"


def test_an_ask_with_no_question_still_counts_toward_the_limit(tmp_path):
    session, rec, asked = _asking(tmp_path, ["ASK:", "ASK:", "ASK:", "ASK: q?"])
    with pytest.raises(RunStalled, match="interrogating"):
        session.next_task()
    assert asked == [] and len(rec.calls) == 4


# -- session second half 19: the direct close-out reads the stored decision -----------

def _direct_summary(tmp_path, record):
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "")
    session.design_checks = [dict(task="t1", **record)] if record is not None else []
    spec = TaskSpec("t1", "Add a favicon.")
    return session._mechanical_close_out(OPUS, spec, TaskMemory("t1", OPUS, session.store))[0]


def _review(verdict, blocking=()):
    return dict(reviewer="openai:gpt-5.6-sol", verdict=verdict, approved=not blocking, blocking=list(blocking))


def test_an_approval_stored_with_trailing_whitespace_reads_as_reviewed(tmp_path):
    text = _direct_summary(tmp_path, dict(verified=True, problem="", final_review=_review("APPROVED\n")))
    assert "design evidence verified and reviewed." in text and "not verified" not in text


def test_the_flag_decides_never_the_verdict_text(tmp_path):
    """A stored APPROVED text beside a blocking decision is not an approval."""
    blocked = dict(_review("APPROVED", ["BLOCKING: the header overlaps the search field"]))
    text = _direct_summary(tmp_path, dict(verified=True, problem="", final_review=blocked))
    assert "verified and reviewed" not in text
    assert "design evidence verified, design review not approved: BLOCKING: the header overlaps" in text
    missing = dict(reviewer="x", verdict="APPROVED")
    assert "verified and reviewed" not in _direct_summary(tmp_path / "b", dict(verified=True, final_review=missing))


def test_the_other_design_outcomes_keep_their_own_words(tmp_path):
    assert "no design evidence required" in _direct_summary(tmp_path / "a", None)
    unverified = _direct_summary(tmp_path / "b", dict(verified=False, problem="the capture exited 1"))
    assert "design evidence not verified: the capture exited 1" in unverified
    unreviewed = _direct_summary(tmp_path / "c", dict(verified=True, problem=""))
    assert "design evidence verified, with no design review on record" in unreviewed
    assert "verified and reviewed" not in unreviewed and "not verified" not in unreviewed


# -- session second half 20: one recheck after a design fix, for both capture modes ---

OVERFLOW = [dict(kind="product.overflow", message="the mobile render is 500px wide", view="mobile",
                 target="http://127.0.0.1:5000/", width=500, viewport=390)]


def _rechecking(tmp_path, monkeypatch, records, *, capture_failure=None, refusal="", **config):
    from quadratus import design_evidence
    root = tmp_path / "project"
    root.mkdir(parents=True)
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True, **config))
    steps = []
    session._run_integration_gate = lambda lead, spec, task, **kw: steps.append("gate") or ""
    session._harness_capture = lambda spec: steps.append("capture") or capture_failure
    session._hand_off_preview = lambda spec, task, record, failure: steps.append("handoff")
    session._evidence_set_problem = lambda task_id: refusal
    ok = not records
    monkeypatch.setattr(design_evidence, "check_records", lambda *a, **k: (
        steps.append("read") or ok, "" if ok else "; ".join(r["message"] for r in records), ["shot.png"], list(records)))
    return session, steps


def _recheck(session, harness, spec=None):
    spec = spec or TaskSpec("t1", "Fix the header.", scope=TaskScope(["static/app.css"], max_lines=20))
    record = dict(task=spec.task_id)
    return session._recheck_after_fix(spec, OPUS, TaskMemory(spec.task_id, OPUS, session.store), record,
                                      harness), record


@pytest.mark.parametrize("refusal", ["", "the evidence set is incomplete (missing summary.json)"])
def test_both_capture_modes_read_the_post_fix_evidence_the_same_way(tmp_path, monkeypatch, refusal):
    results = {}
    for harness in (True, False):
        session, steps = _rechecking(tmp_path / str(harness), monkeypatch, OVERFLOW, refusal=refusal)
        results[harness], _ = _recheck(session, harness)
        assert steps == (["gate", "capture", "read"] if harness else ["gate", "read"])
    assert results[True] == results[False]
    ok, problem, _shots, records = results[True]
    assert ok is False and (refusal in problem)
    assert [r["kind"] for r in records] == ["product.overflow"] + (["integrity"] if refusal else [])


def test_a_failed_harness_recapture_is_the_problem_as_found(tmp_path, monkeypatch):
    session, steps = _rechecking(tmp_path, monkeypatch, [], capture_failure="the preview exited 2")
    result, _ = _recheck(session, True)
    assert result == (False, "the preview exited 2", [], [dict(kind="integrity", message="the preview exited 2")])
    assert steps == ["gate", "capture", "handoff"], "nothing is read from a capture that did not happen"


def test_clean_evidence_after_the_fix_verifies_in_both_modes(tmp_path, monkeypatch):
    for harness in (True, False):
        session, _ = _rechecking(tmp_path / str(harness), monkeypatch, [])
        assert _recheck(session, harness)[0] == (True, "", ["shot.png"], [])


@pytest.mark.requirements_ledger
def test_audit_debt_after_the_fix_is_filed_once_and_only_for_an_audit(tmp_path, monkeypatch):
    audit = TaskSpec("t1", "Audit the header.", scope=TaskScope(["static/app.css"], review_only=True))
    session, _ = _rechecking(tmp_path / "audit", monkeypatch, OVERFLOW, requirements_ledger=True)
    session.memory.ledger.requirements["R1"] = "the header fits"
    session._current_covers = ["R1"]
    session._capture_state = lambda task_id: ("http://127.0.0.1:5000/", [], {})
    result, record = _recheck(session, False, audit)
    assert result is None and record["verified"] is False and record["findings"] == ["F1"]
    assert session.design_checks == [record] and session.findings[0]["kind"] == "product.overflow"
    # An editing task's overflow is never audit debt, in either mode.
    for harness in (True, False):
        session, _ = _rechecking(tmp_path / f"edit-{harness}", monkeypatch, OVERFLOW, requirements_ledger=True)
        session._current_covers = ["R1"]
        result, record = _recheck(session, harness)
        assert result is not None and result[0] is False and session.findings == []


def test_both_fix_branches_of_the_design_check_use_the_one_recheck():
    import inspect
    source = inspect.getsource(Session._check_design)
    assert source.count("self._recheck_after_fix(spec, lead, task, record, harness)") >= 2
    assert "_qualify_debt(spec, ok, problem, records)" in inspect.getsource(Session._recheck_after_fix)
