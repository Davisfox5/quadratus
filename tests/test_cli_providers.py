"""Tests for the subscription-backed CLI providers.

Every test fakes ``cli_providers._launch``, the single seam where this
package executes a vendor CLI; nothing here shells out to a real vendor
CLI or touches the network.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from quadratus import cli_providers
from quadratus.cli_providers import (
    ClaudeCLIProvider,
    CodexCLIProvider,
    _extract_codex_result,
    _extract_grok_result,
    _extract_grok_usage,
)
from quadratus.config import Settings
from quadratus.providers import ProviderError, Turn, build_provider


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _claude_envelope(result, is_error=False):
    return json.dumps({"result": result, "is_error": is_error, "type": "result"})


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    return ClaudeCLIProvider(model="opus")


def test_available_when_binary_resolves(claude):
    assert claude.available()


def test_unavailable_when_binary_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    p = ClaudeCLIProvider(model="opus")
    assert not p.available()
    with pytest.raises(ProviderError, match="not available"):
        p.generate("hi")


def test_extracts_result_from_json_envelope(claude, monkeypatch):
    monkeypatch.setattr(
        cli_providers, "_launch", lambda *a, **k: _FakeCompleted(_claude_envelope("the answer"))
    )
    assert claude.generate("question") == "the answer"


def test_error_envelope_is_not_retried(claude, monkeypatch):
    calls = []

    def fake_run(*a, **k):
        calls.append(1)
        return _FakeCompleted(_claude_envelope("refused to run", is_error=True))

    monkeypatch.setattr(cli_providers, "_launch", fake_run)
    with pytest.raises(ProviderError):
        claude.generate("question")
    # ProviderError is terminal: one attempt, no backoff loop.
    assert len(calls) == 1


def test_system_prompt_is_passed_as_a_flag_not_inlined(claude, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return _FakeCompleted(_claude_envelope("ok"))

    monkeypatch.setattr(cli_providers, "_launch", fake_run)
    claude.generate("do the thing", system="You are ARBITER.")

    argv = seen["argv"]
    assert "--system-prompt" in argv
    assert argv[argv.index("--system-prompt") + 1] == "You are ARBITER."
    # The role must not also be folded into the user prompt, or it is stated twice.
    assert "Your role and instructions" not in seen["input"]


def test_readonly_tools_denied_by_default(claude, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli_providers, "_launch",
        lambda argv, **k: (seen.update(argv=argv), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    claude.generate("review this")
    assert "--disallowed-tools" in seen["argv"]


def test_allow_writes_opts_out_of_the_sandbox(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    seen = {}
    monkeypatch.setattr(
        cli_providers, "_launch",
        lambda argv, **k: (seen.update(argv=argv), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    ClaudeCLIProvider(model="opus", allow_writes=True).generate("build it")
    assert "--disallowed-tools" not in seen["argv"]


def test_runs_in_scratch_dir_not_cwd(claude, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli_providers, "_launch",
        lambda argv, **k: (seen.update(cwd=k.get("cwd")), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    claude.generate("review this")
    assert seen["cwd"] and seen["cwd"] != "."
    claude.cleanup()


def test_bound_seats_share_owner_scratch_and_cleanup(claude):
    first = claude.for_seat('sonnet')
    second = claude.for_seat('haiku', restricted=True)
    assert first.workdir == second.workdir == claude.workdir
    directory = first.workdir
    claude.cleanup()
    assert not os.path.exists(directory)


def test_api_keys_are_stripped_from_child_env(claude, monkeypatch):
    """An inherited key would silently divert the run onto billed transport."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    seen = {}
    monkeypatch.setattr(
        cli_providers, "_launch",
        lambda argv, **k: (seen.update(env=k.get("env")), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    claude.generate("question")
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def test_every_vendors_key_is_stripped_not_just_anthropics(claude, monkeypatch):
    """The Grok keys were missed originally; all four vendors bill the same way."""
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.setenv(var, "sk-should-not-leak")
    seen = {}
    monkeypatch.setattr(
        cli_providers, "_launch",
        lambda argv, **k: (seen.update(env=k.get("env")), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    claude.generate("question")
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                "XAI_API_KEY", "GROK_API_KEY"):
        assert var not in seen["env"], var


def test_history_is_rendered_into_the_prompt(claude, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli_providers, "_launch",
        lambda argv, **k: (seen.update(stdin=k.get("input")), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    claude.generate("continue", history=[Turn("user", "first"), Turn("assistant", "second")])
    assert "first" in seen["stdin"] and "second" in seen["stdin"]


def test_timeout_is_retried_then_surfaces(claude, monkeypatch):
    claude.retry_base_delay = 0.0
    claude.max_retries = 2
    attempts = []

    def fake_run(*a, **k):
        attempts.append(1)
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(cli_providers, "_launch", fake_run)
    with pytest.raises(ProviderError):
        claude.generate("question")
    assert len(attempts) == 2


def test_rate_limit_text_is_retryable(claude):
    assert claude._retryable(RuntimeError("exited 1: usage limit reached"))
    assert not claude._retryable(RuntimeError("exited 1: unknown flag --nope"))


def test_for_model_rebinds_without_rebuilding(claude, monkeypatch):
    cheap = claude.for_model("haiku")
    assert cheap.model == "haiku"
    assert claude.model == "opus"
    seen = {}
    monkeypatch.setattr(
        cli_providers, "_launch",
        lambda argv, **k: (seen.update(argv=argv), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    cheap.generate("cheap task")
    assert seen["argv"][seen["argv"].index("--model") + 1] == "haiku"


def test_codex_prompt_goes_on_stdin(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return _FakeCompleted(
            '{"type":"item.completed","item":'
            '{"id":"item_1","type":"agent_message","text":"codex answer"}}'
        )

    monkeypatch.setattr(cli_providers, "_launch", fake_run)
    out = CodexCLIProvider(model="gpt-5.5-codex").generate("task", system="You are LEAD.")

    assert out == "codex answer"
    # No --system-prompt flag exists, so the role must be folded into the prompt.
    assert "You are LEAD." in seen["input"]
    assert "task" not in seen["argv"]


def test_prompt_never_trails_a_variadic_flag(claude, monkeypatch):
    """Regression: --disallowed-tools is variadic and swallowed a positional prompt.

    The live CLI failed with "Input must be provided either through stdin or as
    a prompt argument", so the prompt must go on stdin rather than argv.
    """
    seen = {}
    monkeypatch.setattr(
        cli_providers, "_launch",
        lambda argv, **k: (
            seen.update(argv=argv, stdin=k.get("input")),
            _FakeCompleted(_claude_envelope("ok")),
        )[1],
    )
    claude.generate("the actual prompt")
    assert seen["stdin"] and "the actual prompt" in seen["stdin"]
    assert "the actual prompt" not in seen["argv"]


#: A real ``codex exec --json`` stream, captured from codex-cli 0.154.0 on
#: 2026-09-12. The warning item is not incidental: it shows up on a perfectly
#: good run, so an error item cannot be treated as fatal on its own.
CODEX_STREAM = "\n".join([
    '{"type":"thread.started","thread_id":"01a097a6-82bf-7ca2-ab48-a6afd5800585"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"error",'
    '"message":"Under-development features enabled: chronicle."}}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"OK"}}',
    '{"type":"turn.completed","usage":{"input_tokens":20114,'
    '"cached_input_tokens":12928,"cache_write_input_tokens":0,'
    '"output_tokens":5,"reasoning_output_tokens":0}}',
])


def test_codex_extractor_reads_the_agent_message_from_the_item():
    """The text is nested in item.text, not on the event. Scanning the event's
    own keys -- the earlier guess -- found nothing at all."""
    assert _extract_codex_result(CODEX_STREAM) == "OK"


def test_codex_extractor_takes_the_last_message():
    stream = CODEX_STREAM + "\n" + (
        '{"type":"item.completed","item":'
        '{"id":"item_2","type":"agent_message","text":"final answer"}}'
    )
    assert _extract_codex_result(stream) == "final answer"


def test_a_routine_warning_item_does_not_fail_a_good_run():
    """codex emits an "under-development features" error item on success."""
    assert _extract_codex_result(CODEX_STREAM) == "OK"


def test_an_error_item_with_no_answer_behind_it_is_an_error():
    stream = "\n".join([
        '{"type":"thread.started","thread_id":"x"}',
        '{"type":"item.completed","item":{"id":"i","type":"error","message":"boom"}}',
    ])
    with pytest.raises(ProviderError, match="boom"):
        _extract_codex_result(stream)


def test_a_stream_with_no_answer_fails_instead_of_returning_itself():
    """The old fallback returned raw JSONL as if it were the reply, so a run
    that produced no answer -- a model the CLI does not serve starts a turn
    and ends it empty -- was indistinguishable from a success."""
    stream = '{"type":"thread.started","thread_id":"x"}\n{"type":"turn.started"}'
    with pytest.raises(ProviderError, match="no assistant message"):
        _extract_codex_result(stream)
    assert "thread.started" not in str(
        pytest.raises(ProviderError, _extract_codex_result, stream).value
    )


def test_unparseable_output_is_an_error_not_an_answer():
    with pytest.raises(ProviderError):
        _extract_codex_result("plain text reply")


# -- settings integration ----------------------------------------------------


def test_cli_backend_selected_by_default(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    p = build_provider("claude", Settings(backend="cli"))
    assert isinstance(p, ClaudeCLIProvider)


def test_api_backend_when_requested():
    s = Settings(backend="api", anthropic_api_key=None)
    p = build_provider("claude", s)
    assert not isinstance(p, ClaudeCLIProvider)


def test_per_provider_override_allows_mixing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    s = Settings(backend="cli", backend_overrides={"grok": "api"})
    assert isinstance(build_provider("claude", s), ClaudeCLIProvider)
    assert s.backend_for("grok") == "api"


def test_uses_cli_reports_subscription_transport():
    assert Settings(backend="cli").uses_cli()
    assert not Settings(backend="api", backend_overrides={}).uses_cli()


def test_model_for_returns_tiered_cli_alias():
    s = Settings(backend="cli")
    assert s.model_for("claude", "high") == "opus"
    assert s.model_for("claude", "low") == "haiku"


# -- public-sharing guard ----------------------------------------------------


def test_share_refused_on_cli_transport_even_when_opted_in(monkeypatch):
    from quadratus.gui import resolve_share

    monkeypatch.setenv("QUADRATUS_ALLOW_SHARE", "1")
    assert resolve_share(Settings(backend="cli")) is False


def test_project_gui_share_refused_even_on_pure_api_transport(monkeypatch):
    from quadratus.gui import resolve_share

    monkeypatch.setenv("QUADRATUS_ALLOW_SHARE", "1")
    assert resolve_share(Settings(backend="api", backend_overrides={})) is False


def test_share_off_by_default(monkeypatch):
    from quadratus.gui import resolve_share

    monkeypatch.delenv("QUADRATUS_ALLOW_SHARE", raising=False)
    assert resolve_share(Settings(backend="api", backend_overrides={})) is False


# -- one spec per vendor, and the operator can correct any of them -----------

from quadratus.cli_providers import (  # noqa: E402
    CLI_SPECS,
    _extract_codex_usage,
    cli_provider_classes,
)
from quadratus.registry import VENDORS  # noqa: E402


def test_there_is_exactly_one_cli_spec_per_vendor_in_the_lineup():
    assert set(CLI_SPECS) == set(VENDORS)
    assert set(cli_provider_classes()) == set(VENDORS)


def test_every_spec_knows_which_vendor_it_belongs_to():
    """Without it, a spec cannot find its own environment overrides."""
    for vendor, spec in CLI_SPECS.items():
        assert spec.vendor == vendor


def test_the_binary_can_be_renamed_without_editing_python(monkeypatch):
    """Two of the three specs are written from documentation rather than from
    a binary, so a wrong name should be a config line, not a patch."""
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_OPENAI", "codex-nightly")
    assert CLI_SPECS["openai"].resolved_binary() == "codex-nightly"
    assert CLI_SPECS["claude"].resolved_binary() == "claude"


def test_extra_args_are_appended_to_every_invocation(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", "--skip-git-repo-check")
    provider = CodexCLIProvider(model="gpt-6-astra")
    argv = provider._build_argv("prompt", "system")
    assert argv[-1] == "--skip-git-repo-check"


def test_extra_args_are_shell_quoted(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", '--profile "my profile"')
    argv = CodexCLIProvider(model="x")._build_argv("prompt", "system")
    assert argv[-2:] == ["--profile", "my profile"]


def test_unparseable_extra_args_are_ignored_rather_than_fatal(monkeypatch):
    """A stray quote in .env should not take the vendor out of the run."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", 'unbalanced "quote')
    assert CodexCLIProvider(model="x")._build_argv("p", "s")[-1] != 'unbalanced "quote'


def test_the_overrides_land_after_the_specs_own_flags(monkeypatch):
    """So an override can also correct something the spec got wrong: most CLIs
    let the later flag win."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", "--sandbox danger-full-access")
    argv = CodexCLIProvider(model="x")._build_argv("p", "s")
    assert argv.index("read-only") < argv.index("danger-full-access")


# -- codex token counts ------------------------------------------------------


def test_codex_usage_comes_from_the_turn_completed_event():
    assert _extract_codex_usage(CODEX_STREAM) == {
        "input_tokens": 20114, "output_tokens": 5,
    }


def test_cached_input_is_not_added_to_the_input_total():
    """codex reports cached_input_tokens as a subset of input_tokens, unlike
    the Claude envelope where cache reads are separate and are folded in.
    Guessing wrong in the additive direction would inflate the bill."""
    usage = _extract_codex_usage(CODEX_STREAM)
    assert usage["input_tokens"] == 20114  # not 20114 + 12928


def test_codex_usage_is_none_when_the_stream_says_nothing_about_tokens():
    """A metering guess that looked measured would be worse than no number."""
    assert _extract_codex_usage('{"type": "item.completed"}') is None
    assert _extract_codex_usage("not json at all") is None


# -- specs corrected against the real binaries (2026-09-12) ------------------

from quadratus.cli_providers import GrokCLIProvider  # noqa: E402


def test_codex_always_skips_the_git_repo_check(monkeypatch):
    """Every provider runs in a scratch directory, codex refuses to run
    outside a git repo without this, and no configuration here wants the
    check. Not an operator's job to remember."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    argv = CodexCLIProvider(model="gpt-6-astra")._build_argv("p", "s")
    assert "--skip-git-repo-check" in argv


def test_codex_keeps_skipping_it_when_writes_are_granted(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    argv = CodexCLIProvider(model="x", allow_writes=True)._build_argv("p", "s")
    assert "--skip-git-repo-check" in argv
    assert "read-only" not in argv


def test_grok_never_pairs_a_value_taking_flag_with_a_separate_prompt(monkeypatch):
    """grok's -p is `--single <PROMPT>`, not a print-mode switch: it consumes
    the next argument. The old spec would have fed it whichever flag followed."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    argv = GrokCLIProvider(model="grok-4.6")._build_argv("the prompt", "the role")
    assert "-p" not in argv


def test_grok_passes_the_prompt_as_a_file(monkeypatch, tmp_path):
    """argv has a hard length limit and these prompts carry whole code
    artifacts, so a path beats both argv and stdin where a CLI offers one."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    provider = GrokCLIProvider(model="grok-4.6", workdir=str(tmp_path))
    argv = provider._build_argv("the prompt", "the role")
    path = argv[argv.index("--prompt-file") + 1]
    assert open(path, encoding="utf-8").read() == "the prompt"


def test_prompt_files_are_unique_outside_source_and_cleaned(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    provider = GrokCLIProvider(model="x", workdir=str(tmp_path))
    first = provider._build_argv("one", "")[-1]
    second = provider._build_argv("two", "")[-1]
    assert first != second
    assert str(tmp_path) not in first
    assert open(first, encoding="utf-8").read() == "one"
    assert open(second, encoding="utf-8").read() == "two"
    provider.cleanup()
    assert not os.path.exists(first) and not os.path.exists(second)
    assert tmp_path.is_dir()


def test_grok_sends_the_role_as_a_system_prompt_not_inlined(monkeypatch, tmp_path):
    """It has --system-prompt-override, so an assigned role no longer has to
    compete with the coding-agent persona inside the user prompt."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    provider = GrokCLIProvider(model="x", workdir=str(tmp_path))
    argv = provider._build_argv("the prompt", "You are a reviewer.")
    assert argv[argv.index("--system-prompt-override") + 1] == "You are a reviewer."
    assert "You are a reviewer." not in provider._compose_prompt("the prompt", "You are a reviewer.", [])


def test_grok_granted_edit_does_not_cancel_on_conflicting_permission_mode(monkeypatch, tmp_path):
    """Reproduce the live CLI's acceptEdits + always-approve cancellation."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    target = tmp_path / "proof.txt"
    target.write_text("before")

    def fake_run(argv, **kwargs):
        if "--permission-mode" in argv or "--always-approve" not in argv:
            return _FakeCompleted(json.dumps({"stopReason": "cancelled", "text": "I'll edit."}))
        assert kwargs["cwd"] == str(tmp_path)
        target.write_text("after")
        return _FakeCompleted(json.dumps({"stopReason": "end_turn", "text": "DONE"}))

    monkeypatch.setattr(cli_providers, "_launch", fake_run)
    provider = GrokCLIProvider(model="x", workdir=str(tmp_path), allow_writes=True)
    assert provider.generate("Make the authorized edit.") == "DONE"
    provider.cleanup()
    assert target.read_text() == "after"


# -- the grok envelope -------------------------------------------------------
#
# Captured from grok 1.0.30 on 2026-09-12, signed in. Before --output-format
# json was sent, the CLI streamed an agent's narration and the provider
# returned whatever it was mid-sentence about: a live session closed a task on
# "I'll implement add(a, b)... checking the workspace", 119 characters with no
# function in them, and reported success. These tests encode the real shapes so
# that cannot come back.

_GROK_OK = json.dumps({
    "text": "```python\ndef add(a, b):\n    \"\"\"Return the sum.\"\"\"\n    return a + b\n```",
    "stopReason": "end_turn",
    "sessionId": "01a098d8-910b-79d2-945d-925d2842b5e8",
    "usage": {
        "input_tokens": 6009,
        "cache_read_input_tokens": 10496,
        "cache_creation_input_tokens": 0,
        "output_tokens": 72,
    },
})

#: What a denied write actually returns: a completed-looking payload whose
#: text is the preamble to work that never happened.
_GROK_CANCELLED = json.dumps({
    "text": "I'll create `proof.txt` with the word HELLO now.",
    "stopReason": "cancelled",
})


def test_the_grok_answer_comes_from_the_envelope():
    assert "def add(a, b)" in _extract_grok_result(_GROK_OK)


def test_a_turn_that_did_not_finish_is_an_error_even_with_text_in_it():
    """The failure this extractor exists for. Denying the write tools ends the
    turn as 'cancelled' with the narration still in `text`; returning that
    would hand a lead a confident sentence in place of the work."""
    with pytest.raises(ProviderError) as caught:
        _extract_grok_result(_GROK_CANCELLED)
    assert "cancelled" in str(caught.value)
    assert "proof.txt" in str(caught.value), "say what it was doing when it stopped"


def test_an_unrecognised_stop_reason_is_not_assumed_benign():
    with pytest.raises(ProviderError):
        _extract_grok_result(json.dumps({"text": "something", "stopReason": "max_turns"}))


def test_a_completed_turn_with_no_text_is_an_error():
    with pytest.raises(ProviderError):
        _extract_grok_result(json.dumps({"text": "  ", "stopReason": "end_turn"}))


def test_narration_instead_of_json_fails_loudly():
    """What the CLI prints without --output-format json. It must not be
    mistaken for an answer, and the operator needs to see it."""
    with pytest.raises(ProviderError) as caught:
        _extract_grok_result("I'll implement `add(a, b)`. Checking the workspace.")
    assert "add(a, b)" in str(caught.value)


def test_grok_reports_real_token_counts():
    """Cache reads fold into input for the same reason they do on the claude
    side: the meter asks what this would have cost on API keys."""
    assert _extract_grok_usage(_GROK_OK) == {
        "input_tokens": 6009 + 10496,
        "output_tokens": 72,
    }


def test_missing_grok_usage_is_absent_rather_than_zero():
    assert _extract_grok_usage(json.dumps({"text": "hi", "stopReason": "end_turn"})) is None
    assert _extract_grok_usage("not json") is None


def test_grok_approves_tools_on_every_call_including_read_only(monkeypatch):
    """Operator decision, 2026-09-12. --permission-mode acceptEdits does not
    cover this: without --always-approve the CLI cancels the turn the first
    time a tool is called, silently and headlessly, which would leave the ROTE
    rung and the lookup errand -- both grok's -- failing on most tasks."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    for granted in (False, True):
        argv = GrokCLIProvider(model="x", allow_writes=granted)._build_argv("p", "")
        assert "--always-approve" in argv


def test_grok_edit_view_does_not_redirect_its_readonly_owner(monkeypatch, tmp_path):
    """Permission remains per call; a granted view cannot mutate owner cwd."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    owner = GrokCLIProvider(model="x")
    scratch = owner.workdir
    granted = owner.in_directory(tmp_path, allow_writes=True)
    assert granted.workdir == str(tmp_path)
    assert granted._allow_writes is True
    assert owner.workdir == scratch
    assert owner._allow_writes is False
    owner.cleanup()
    assert tmp_path.is_dir()


def test_grok_asserts_no_read_only_protection_it_does_not_have():
    """--always-approve overrides --disallowed-tools: asked to write a denied
    file under both, grok wrote it and ran a shell check on it. Passing denial
    flags would encode a guarantee experiment disproved, so readonly_args is
    empty on purpose and the scratch directory is the containment."""
    from quadratus.cli_providers import GROK_SPEC
    assert GROK_SPEC.readonly_args == []


def test_grok_asks_for_the_json_envelope():
    """Without this flag the provider parses narration. Nothing else in the
    grok path can tell a preamble from an answer."""
    from quadratus.cli_providers import GROK_SPEC
    assert GROK_SPEC.output_args == ["--output-format", "json"]


# -- the seat axis -----------------------------------------------------------
#
# A seat is not only a model. The grok CLI is a coding agent: asked for a
# one-line function it explored the tree, wrote files, spawned subagents and
# re-sent the conversation every turn -- 120K tokens, against 22K for the
# orchestrator supervising it. The same line called as a bounded worker did it
# in ~6K. All of these were measured against grok 1.0.30 on 2026-09-13.

def _grok_argv(monkeypatch, **kw):
    from quadratus.registry import resolve
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    key = kw.pop("key", None)
    provider = GrokCLIProvider(model="", **kw)
    if key:
        spec = resolve(key)
        provider = provider.for_seat("", effort=spec.effort, restricted=spec.restricted)
    return provider._build_argv("the prompt", "the role")


def test_a_worker_seat_gets_a_bounded_call_not_an_agent(monkeypatch):
    argv = _grok_argv(monkeypatch, key="grok:worker")
    assert argv[argv.index("--tools") + 1] == "read_file,grep,list_dir,web_search,web_fetch"
    assert argv[argv.index("--disallowed-tools") + 1] == "Agent"
    assert argv[argv.index("--effort") + 1] == "low"
    # The write tools are absent, so there is nothing to auto-approve; sending
    # the approval flag anyway would re-admit the agent loop.
    assert "--always-approve" not in argv


def test_a_brain_trust_seat_keeps_the_whole_agent(monkeypatch):
    """The one place the loop earns its keep: a peer that can read the tree
    forms an opinion worth having."""
    argv = _grok_argv(monkeypatch, key="grok:default")
    assert "--tools" not in argv
    assert "--always-approve" in argv
    assert argv[argv.index("--effort") + 1] == "high"


def test_escalation_is_a_reasoning_step_not_a_different_model(monkeypatch):
    """xAI serves one model line on a subscription, so there is no second
    model to bump to. Effort is the ladder."""
    worker = _grok_argv(monkeypatch, key="grok:worker")
    expert = _grok_argv(monkeypatch, key="grok:expert")
    assert "--model" not in worker and "--model" not in expert
    assert worker[worker.index("--effort") + 1] == "low"
    assert expert[expert.index("--effort") + 1] == "high"


def test_a_restricted_call_delivers_its_prompt_in_argv(monkeypatch):
    """Not a preference. --tools is honoured only in headless mode; under
    --prompt-file it is ignored in silence and the call comes back a cancelled
    agent turn, which is how the restriction was first thought impossible."""
    argv = _grok_argv(monkeypatch, key="grok:worker")
    assert argv[argv.index("-p") + 1] == "the prompt"
    assert "--prompt-file" not in argv


def test_an_oversized_restricted_prompt_is_refused_not_downgraded(monkeypatch):
    """Falling back to a prompt file would drop the tool restrictions with it,
    turning a bounded worker into a full agent with writes -- silently."""
    from quadratus.cli_providers import MAX_ARGV_PROMPT
    from quadratus.registry import resolve
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    spec = resolve("grok:worker")
    provider = GrokCLIProvider(model="").for_seat(
        "", effort=spec.effort, restricted=spec.restricted
    )
    with pytest.raises(ProviderError) as caught:
        provider._build_argv("x" * (MAX_ARGV_PROMPT + 1), "role")
    assert "unrestricted seat" in str(caught.value)


def test_seats_sharing_a_model_are_not_the_same_provider(monkeypatch):
    """for_model alone would return the same object for all three grok seats:
    they resolve to the same empty alias, differing only in how they are
    called."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    base = GrokCLIProvider(model="")
    worker = base.for_seat("", effort="low", restricted=True)
    expert = base.for_seat("", effort="high", restricted=True)
    assert worker is not expert
    assert base.for_model("") is base


def test_no_grok_seat_names_a_release():
    """Operator directive: never tied to an iteration. Both IDs that broke
    this run -- grok-4-1-fast and grok-build -- were pinned names."""
    from quadratus.registry import ROSTER
    for spec in (m for m in ROSTER if m.provider == "grok"):
        assert spec.vendor_default, spec.key


# -- the seat axis across all three vendors -----------------------------------

def _seat_argv(monkeypatch, cls, key):
    from quadratus.latest import alias_for
    from quadratus.registry import resolve
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/x")
    spec = resolve(key)
    alias = alias_for(key, env={}, cache={})
    provider = cls(model=alias).for_seat(
        alias, effort=spec.effort, restricted=spec.restricted
    )
    return provider._build_argv("the prompt", "the role")


def test_every_vendor_can_express_a_bounded_worker(monkeypatch):
    """The axis the spec was missing. All three CLIs are agents by default,
    and a worker bee running a full agent loop is the same waste everywhere --
    grok only made it visible by reporting 120K tokens for a one-line
    function."""
    claude = _seat_argv(monkeypatch, ClaudeCLIProvider, "claude:haiku")
    codex = _seat_argv(monkeypatch, CodexCLIProvider, "openai:gpt-5.6-luna")
    grok = _seat_argv(monkeypatch, GrokCLIProvider, "grok:worker")
    # Each vendor spells the dial differently; the roster row says only "low".
    assert claude[claude.index("--effort") + 1] == "low"
    assert "model_reasoning_effort=low" in codex
    assert grok[grok.index("--effort") + 1] == "low"


def test_a_restricted_claude_also_blocks_subagents(monkeypatch):
    """Task is the expensive tool: each subagent is a fresh context window
    billed against the same subscription."""
    argv = _seat_argv(monkeypatch, ClaudeCLIProvider, "claude:haiku")
    assert "Task" in argv[argv.index("--disallowed-tools") + 1]
    lead = _seat_argv(monkeypatch, ClaudeCLIProvider, "claude:opus")
    assert "Task" not in lead[lead.index("--disallowed-tools") + 1]


def test_a_restricted_codex_keeps_its_mandatory_flag(monkeypatch):
    """--skip-git-repo-check is not optional -- every provider runs in a
    scratch directory that is not a repository. always_args is unconditional
    for exactly this reason, which is why grok's --always-approve had to move
    out of it to agentic_args rather than be skipped along with it."""
    argv = _seat_argv(monkeypatch, CodexCLIProvider, "openai:gpt-5.6-luna")
    assert "--skip-git-repo-check" in argv


def test_an_agentic_grok_seat_still_gets_its_approval_flag(monkeypatch):
    argv = _seat_argv(monkeypatch, GrokCLIProvider, "grok:default")
    assert "--always-approve" in argv
    worker = _seat_argv(monkeypatch, GrokCLIProvider, "grok:worker")
    assert "--always-approve" not in worker


def test_the_senior_seats_are_never_restricted():
    """Leads write code and brain-trust peers read the tree to form an
    opinion. Bounding those is not a saving, it is a different job."""
    from quadratus.registry import MODE_ROSTERS, ORCHESTRATOR_CHAIN, resolve
    for key in list(ORCHESTRATOR_CHAIN) + MODE_ROSTERS["adversarial"]["peers"]:
        assert not resolve(key).restricted, key


def test_every_worker_tree_seat_is_bounded():
    """The point of the exercise: no errand runs a full agent loop."""
    from quadratus.registry import resolve
    from quadratus.workers import DEFAULT_WORKER, WORKER_ESCALATION, WORKER_TREE
    for key in set(WORKER_TREE.values()) | set(WORKER_ESCALATION.values()) | {DEFAULT_WORKER}:
        assert resolve(key).restricted, key


def test_codex_session_record_is_a_floor_for_diagnostics_never_a_measurement(tmp_path):
    """Codex review of #25: null token_count info is not zero, reasoning is
    already inside output_tokens, and a floor never clears unknown usage."""
    import json as _json

    from quadratus.cli_providers import _codex_rollout_usage
    sid = "01a0dab8-8947-72f1-9283-8b4b15408d54"
    folder = tmp_path / "2026" / "09" / "25"
    folder.mkdir(parents=True)
    rollout = folder / f"rollout-2026-09-25T22-38-41-{sid}.jsonl"
    empty = {"type": "event_msg", "payload": {"type": "token_count", "info": None}}
    failed = {"type": "event_msg", "payload": {"type": "error", "message": "401 Unauthorized"}}
    rollout.write_text("\n".join(_json.dumps(e) for e in [empty] * 6 + [failed]) + "\n")
    assert _codex_rollout_usage(tmp_path, sid) is None, "null info is not zero usage"
    used = {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": 900, "cached_input_tokens": 400, "output_tokens": 1285, "reasoning_output_tokens": 91}}}}
    rollout.write_text(_json.dumps(empty) + "\n" + _json.dumps(used) + "\n")
    floor = _codex_rollout_usage(tmp_path, sid)
    assert floor["output_tokens"] == 1285, "reasoning tokens are already counted"
    assert "lower bound" in floor["usage_source"]
    assert _codex_rollout_usage(tmp_path, "../../etc") is None


def test_a_codex_401_is_a_named_vendor_wide_sign_in_failure():
    """GameTape run 6: the turn failed 401 after the websocket dropped."""
    import json as _json

    import pytest

    from quadratus.cli_providers import _extract_codex_result
    from quadratus.providers import ProviderError
    stream = "\n".join(_json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "x"},
        {"type": "error", "message": "Reconnecting... 5/5 (unexpected status 401 Unauthorized: ...)"},
        {"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized: Incorrect API key"}},
    ])
    with pytest.raises(ProviderError) as caught:
        _extract_codex_result(stream)
    assert getattr(caught.value, "auth_invalid", False) and "sign-in was rejected" in str(caught.value)
