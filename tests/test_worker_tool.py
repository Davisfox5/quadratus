"""The in-session commission_worker tool: server, bridge, CLI forms, session."""

import json
import os
import subprocess
import sys

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import ClaudeCLIProvider, CodexCLIProvider, GrokCLIProvider
from quadratus.config import Settings
from quadratus.delegation import invocation, invocation_context
from quadratus.memory import TaskMemory
from quadratus.runtime import Fleet
from quadratus.session import RunStalled, Session, SessionConfig, TaskSpec
from quadratus.worker_bridge import WorkerBridge, input_schema
from quadratus.workers import WORKER_TREE, WorkerPool

OPUS = "claude:opus"


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda _: '/unused/cli')
    for vendor in ('OPENAI', 'CLAUDE', 'GROK'):
        monkeypatch.delenv(f'QUADRATUS_CLI_ARGS_{vendor}', raising=False)
    monkeypatch.delenv('QUADRATUS_CONTAINED', raising=False)
    monkeypatch.setenv('QUADRATUS_NATIVE_DELEGATION', 'off')


def _mcp(spec, messages):
    """Drive the real stdio server as a CLI would, one JSON-RPC line each."""
    env = {**os.environ, **spec["env"]}
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    out = subprocess.run([spec["command"], *spec["args"]], input=stdin, env=env,
                         capture_output=True, text=True, timeout=30).stdout
    return {r["id"]: r for r in map(json.loads, out.splitlines())}


def test_server_lists_a_typed_tool_and_forwards_calls_to_the_session():
    seen = []

    def handler(arguments):
        seen.append(arguments)
        return f"answer for {arguments['errand']}", False

    with WorkerBridge(handler) as bridge:
        spec = bridge.spec()
        replies = _mcp(spec, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "commission_worker", "arguments": {"errand": "read", "instruction": "x"}}},
        ])
    tool = replies[2]["result"]["tools"][0]
    assert tool["name"] == "commission_worker"
    assert tool["inputSchema"]["properties"]["errand"]["enum"] == sorted(WORKER_TREE)
    # Nothing a worker could be refused for is offered: no write, no needs.
    assert set(tool["inputSchema"]["properties"]) == {"errand", "instruction", "demanding"}
    assert replies[3]["result"] == {"content": [{"type": "text", "text": "answer for read"}], "isError": False}
    assert seen == [{"errand": "read", "instruction": "x"}]


def test_a_call_without_the_bridge_token_is_refused():
    with WorkerBridge(lambda a: ("ran", False)) as bridge:
        spec = bridge.spec()
        spec["env"] = dict(spec["env"], QUADRATUS_WORKER_TOKEN="wrong")
        replies = _mcp(spec, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "commission_worker", "arguments": {}}}])
    assert replies[1]["result"]["isError"] is True
    assert "token" in replies[1]["result"]["content"][0]["text"]
    assert not os.path.exists(spec["env"]["QUADRATUS_WORKER_SOCKET"])


def _tool():
    return dict(name="quadratus", command="/py", args=["/srv.py"], env={"K": "v"}, timeout=900)


def test_claude_attaches_only_its_own_server_and_lifts_only_the_mcp_denial():
    view = ClaudeCLIProvider(model="opus")
    view.worker_tool = _tool()
    argv = view._build_argv("p", "")
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert config["mcpServers"]["quadratus"]["env"] == {"K": "v"}
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == "mcp__quadratus__commission_worker"
    denied = argv[argv.index("--disallowed-tools") + 1].split()
    assert "mcp__*" not in denied and "Task" in denied and "Agent" in denied
    # Without the tool, the denial is whole.
    plain = ClaudeCLIProvider(model="opus")._build_argv("p", "")
    assert "mcp__*" in plain[plain.index("--disallowed-tools") + 1].split()
    assert "--mcp-config" not in plain


def test_codex_attaches_with_per_server_approval_and_a_real_timeout():
    view = CodexCLIProvider("gpt-5.6-sol")
    view.worker_tool = _tool()
    argv = view._build_argv("p", "")
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert 'mcp_servers.quadratus.command="/py"' in overrides
    assert 'mcp_servers.quadratus.default_tools_approval_mode="approve"' in overrides
    assert "mcp_servers.quadratus.tool_timeout_sec=900" in overrides
    assert 'mcp_servers.quadratus.env={K = "v"}' in overrides


def test_grok_gets_no_tool_outside_the_container(tmp_path):
    view = GrokCLIProvider(model="", workdir=str(tmp_path))
    view.worker_tool = _tool()
    argv = view._build_argv("p", "")
    assert "--trust" not in argv and not (tmp_path / ".grok").exists()
    assert "use_tool" in argv[argv.index("--disallowed-tools") + 1]
    assert view.worker_tool_attached is False


def test_grok_in_the_container_writes_a_scoped_config_and_removes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_CONTAINED", "1")
    view = GrokCLIProvider(model="", workdir=str(tmp_path))
    view.worker_tool = _tool()
    argv = view._build_argv("p", "")
    assert "--trust" in argv
    denied = argv[argv.index("--disallowed-tools") + 1].split(",")
    assert "use_tool" not in denied and "search_tool" not in denied and "spawn_subagent" in denied
    config = (tmp_path / ".grok" / "config.toml").read_text()
    assert "[mcp_servers.quadratus]" in config and "tool_timeout_sec = 900" in config
    view._remove_worker_tool_files()
    assert not (tmp_path / ".grok").exists()


def test_grok_never_overwrites_a_project_grok_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_CONTAINED", "1")
    (tmp_path / ".grok").mkdir()
    (tmp_path / ".grok" / "config.toml").write_text("mine")
    view = GrokCLIProvider(model="", workdir=str(tmp_path))
    view.worker_tool = _tool()
    argv = view._build_argv("p", "")
    assert "--trust" not in argv and view.worker_tool_attached is False
    view._remove_worker_tool_files()
    assert (tmp_path / ".grok" / "config.toml").read_text() == "mine"


def test_fleet_attaches_the_tool_to_the_lead_view_only(monkeypatch):
    fleet = Fleet(Settings())
    provider = ClaudeCLIProvider(model="opus")
    monkeypatch.setattr(fleet, "provider_for", lambda key: provider)
    seen = {}

    def fake_generate(key, view, prompt, system, **kw):
        seen[invocation_context.get()["role"]] = getattr(view, "worker_tool", None)
        return "ok"

    monkeypatch.setattr(fleet, "_generate", fake_generate)
    for role in ("lead", "verifier", "worker:read-1"):
        with invocation("t1", role, worker_tool=_tool()):
            fleet._invoke(OPUS, "p")
    assert seen["lead"] == _tool()
    assert seen["verifier"] is None and seen["worker:read-1"] is None
    assert provider.worker_tool is None  # never set on the shared provider


# -- the session serves the tool without ending the lead's call --------------


def _memory(store):
    return TaskMemory("t1", OPUS, store)


def _call_tool(arguments):
    """What the MCP server does, from inside the fake lead call."""
    from quadratus import worker_mcp
    spec = invocation_context.get()["worker_tool"]
    os.environ.update(spec["env"])
    try:
        return worker_mcp._forward(arguments)
    finally:
        for key in spec["env"]:
            os.environ.pop(key, None)


def test_a_lead_delegates_mid_call_and_keeps_its_session(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    worker_prompts = []

    def run(model, prompt, allow_writes=False):
        worker_prompts.append((model, prompt))
        return "helpers live in util.py"

    lead_calls = []

    def invoke(model, prompt, system=None, allow_writes=False):
        lead_calls.append(prompt)
        answer, failed = _call_tool({"errand": "read", "instruction": "where are helpers?"})
        assert not failed and "helpers live in util.py" in answer
        return "Done: used util.py as the worker said."

    session = Session("goal", store, invoke, config=SessionConfig())
    session.workers = WorkerPool(store=store, run=run)
    with invocation("t1", "lead"):
        draft = session._draft_with_channels(OPUS, TaskSpec("t1", "do it"), _memory(store))
    assert draft.startswith("Done")
    assert len(lead_calls) == 1  # delegating did not end the lead's call
    assert len(worker_prompts) == 1 and session.workers.spawned("t1") == 1
    assert "commission_worker tool" in lead_calls[0]
    assert session._worker_tool is None  # the channel closes with the loop


def test_the_tool_refuses_write_errands_and_bad_errands_for_free(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    answers = []

    def invoke(model, prompt, system=None, allow_writes=False):
        answers.append(_call_tool({"errand": "code", "instruction": "edit x", "write": True}))
        answers.append(_call_tool({"errand": "nonsense", "instruction": "x"}))
        return "Done myself."

    session = Session("goal", store, invoke, config=SessionConfig(max_worker_failures=5))
    session.workers = WorkerPool(store=store, run=lambda *a, **k: pytest.fail("no worker should run"))
    with invocation("t1", "lead"):
        session._draft_with_channels(OPUS, TaskSpec("t1", "do it"), _memory(store))
    assert answers[0][1] is True and "Write errands" in answers[0][0]
    assert answers[1][1] is True and session.workers.spawned("t1") == 0


def test_a_stall_inside_the_tool_closes_the_channel_and_ends_the_draft(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    answers = []

    def fails(model, prompt, allow_writes=False):
        raise RuntimeError("worker broke")

    def invoke(model, prompt, system=None, allow_writes=False):
        answers.append(_call_tool({"errand": "read", "instruction": "one"}))
        answers.append(_call_tool({"errand": "read", "instruction": "two"}))
        return "Finished anyway."

    session = Session("goal", store, invoke, config=SessionConfig(max_worker_failures=1))
    session.workers = WorkerPool(store=store, run=fails)
    with invocation("t1", "lead"), pytest.raises(RunStalled):
        session._draft_with_channels(OPUS, TaskSpec("t1", "do it"), _memory(store))
    assert "channel closed" in answers[0][0]
    assert "channel is closed" in answers[1][0]


def test_the_tool_is_offered_only_when_enabled(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    seen = []

    def invoke(model, prompt, system=None, allow_writes=False):
        seen.append((invocation_context.get() or {}).get("worker_tool"))
        return "Done."

    session = Session("goal", store, invoke, config=SessionConfig(in_session_workers=False))
    with invocation("t1", "lead"):
        session._draft_with_channels(OPUS, TaskSpec("t1", "do it"), _memory(store))
    assert seen == [None]


def test_input_schema_is_json_serialisable_and_matches_the_tree():
    schema = json.loads(json.dumps(input_schema()))
    assert schema["required"] == ["errand", "instruction"]
    assert set(schema["properties"]["errand"]["enum"]) == set(WORKER_TREE)
    assert sys.executable  # the server is started with the session's interpreter


def test_the_tool_does_not_read_a_write_request_into_a_read_errand(tmp_path):
    """GameTape, 2026-09-25: grok's first tool call, verbatim in substance,
    was refused as a patch because it named the helper it meant to insert."""
    store = ArtifactStore(tmp_path / "artifacts")
    asked = []

    def run(model, prompt, allow_writes=False):
        asked.append(allow_writes)
        return "app.py imports csv and io at lines 3-4"

    instruction = ("Report only what is needed to insert a pure helper _read_clip_manifest into "
                   "/work/app.py and tests into /work/tests/test_basic.py. Quote the first 40 lines "
                   "of app.py with line numbers.")
    answers = []

    def invoke(model, prompt, system=None, allow_writes=False):
        answers.append(_call_tool({"errand": "read", "instruction": instruction}))
        return "Done."

    session = Session("goal", store, invoke, config=SessionConfig())
    session.workers = WorkerPool(store=store, run=run)
    with invocation("t1", "lead"):
        session._draft_with_channels(OPUS, TaskSpec("t1", "do it"), _memory(store))
    assert answers[0][1] is False and "csv and io" in answers[0][0]
    assert asked == [False]  # still no write grant


def test_the_reply_channel_still_infers_needs_from_the_text(tmp_path):
    from quadratus.workers import check_errand_fit
    text = "insert a helper into app.py"
    assert check_errand_fit(text) is not None
    assert check_errand_fit(text, answer_only=True) is None


def test_repeated_refused_tool_calls_close_the_channel(tmp_path):
    """Codex review of #25: refusals were free and unbounded within one call."""
    store = ArtifactStore(tmp_path / "artifacts")
    answers = []

    def invoke(model, prompt, system=None, allow_writes=False):
        for _ in range(4):
            answers.append(_call_tool({"errand": "nonsense", "instruction": "x"}))
        return "Done myself."

    session = Session("goal", store, invoke, config=SessionConfig(max_worker_failures=3))
    session.workers = WorkerPool(store=store, run=lambda *a, **k: pytest.fail("no worker should run"))
    with invocation("t1", "lead"), pytest.raises(RunStalled, match="not converging"):
        session._draft_with_channels(OPUS, TaskSpec("t1", "do it"), _memory(store))
    assert "now closed" in answers[2][0] and "channel is closed" in answers[3][0]


@pytest.mark.parametrize("request_reply,worker", [
    ('WORKER {"errand":"check","instruction":"review t.py","write":false,"needs":[]}', "ok"),
    ('WORKER {"errand":"check","instruction":"review t.py","write":false,"needs":[]}', "fail"),
    ('WORKER {"errand":"check","instruction":"run pytest -q on t.py","write":false,"needs":["execute"]}', "ok"),
    ("CONSULT claude:opus: is t.py right?", "ok"),
    ("FETCH: missing-artifact", "ok"),
])
def test_every_re_ask_after_edits_tells_the_lead_what_it_changed(tmp_path, request_reply, worker):
    """GameTape run 5, then Codex: after a served, failed or refused worker,
    a consult or a fetch, the re-asked lead is told what its task changed."""
    import dataclasses
    store = ArtifactStore(tmp_path / "artifacts")
    project = tmp_path / "p"
    project.mkdir()
    (project / "t.py").write_text("x = 1\n")
    prompts = []

    def invoke(model, prompt, system=None, allow_writes=False):
        prompts.append(prompt)
        if "colleague leading this task asks you" in prompt:
            return "Looks right."
        if len(prompts) == 1:
            (project / "t.py").write_text("x = 2\n")
            return request_reply
        return "Done."

    def run(*args, **kwargs):
        if worker == "fail":
            raise RuntimeError("worker broke")
        return "t.py looks right"

    session = Session("goal", store, invoke, config=SessionConfig(max_worker_failures=5))
    session.project = project
    session.config = dataclasses.replace(session.config, allow_writes=True)
    session._task_before = session._capture_source()
    session.workers = WorkerPool(store=store, run=run)
    with invocation("t1", "lead"):
        session._draft_with_channels(OPUS, TaskSpec("t1", "do it"), _memory(store))
    lead_prompts = [p for p in prompts if "colleague leading this task asks you" not in p]
    assert len(lead_prompts) >= 2
    assert "already changed (kept" in lead_prompts[1] and "t.py" in lead_prompts[1]
