"""Conformance audit of the merged tree, 2026-11-12: the development line's
child processes and failure handling held to the review batch's rules.

children: a vendor binary asked for its version or its model listing gets
its environment from ``child_env`` under the vendor limit.
transport: decisions read the typed facts set where a CLI's own envelope or
stderr is parsed, and the ledger's failure class reads the same text.

Fake CLIs only; no model call.
"""
import json
import os
from pathlib import Path

import pytest

import tests.test_review1110_transport as T
from quadratus import catalog
from quadratus import cli_providers as CP
from quadratus.cli_providers import ClaudeCLIProvider, CodexCLIProvider
from quadratus.config import Settings
from quadratus.providers import PermissionBlocked, ProviderError, TransientOverload
from quadratus.run_budget import FinishReserved, RunBudget, RunLimits
from quadratus.runtime import Fleet, WindowExhausted
from tests.test_review1110_transport import _no_backoff  # noqa: F401 -- autouse fixture

PLANTED = ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY",
           "XAI_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "XAI_BASE_URL")


def _fake_grok(tmp_path):
    """A stand-in ``grok`` that answers ``--version`` and ``models`` and
    writes the names in its environment to ``env_<first argument>.txt``."""
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "grok"
    script.write_text(
        "#!/bin/sh\n"
        f'env | cut -d= -f1 | sort > "{tmp_path}/env_$1.txt"\n'
        'if [ "$1" = "--version" ]; then echo "grok 9.9.9"; exit 0; fi\n'
        'echo "grok-4.6 (default)"\necho "grok-4.5"\n')
    script.chmod(0o755)
    return script


def _seen(tmp_path, call):
    return set((tmp_path / f"env_{call}.txt").read_text().split())


# -- children:1: `grok models` -------------------------------------------------

def test_the_grok_listing_is_not_started_with_credentials(tmp_path, monkeypatch):
    script = _fake_grok(tmp_path)
    for name in PLANTED:
        monkeypatch.setenv(name, "planted")
    listing = catalog.grok_catalog(str(script))
    assert listing.status == "ok" and listing.default == "grok-4.6"
    seen = _seen(tmp_path, "models")
    assert not [name for name in PLANTED if name in seen]
    assert "PATH" in seen and "HOME" in seen     # the sign-in files under HOME still apply


def test_the_status_display_reads_the_listing_without_credentials(tmp_path, monkeypatch):
    script = _fake_grok(tmp_path)
    for name in PLANTED:
        monkeypatch.setenv(name, "planted")
    monkeypatch.setenv("QUADRATUS_GROK_MODELS", "1")
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_GROK", str(script))
    lines = Fleet(Settings(backend="cli")).status_lines()
    assert any("grok models: ok" in line for line in lines), lines
    for call in ("--version", "models"):
        seen = _seen(tmp_path, call)
        assert not [name for name in PLANTED if name in seen], call


# -- children:2: the version probe --------------------------------------------

def test_the_version_probe_keeps_the_vendor_limit_whatever_is_passed(tmp_path, monkeypatch):
    """A key the operator passed for the project's tests is not handed to a
    vendor binary, on a real call or on ``--version``; another name is."""
    script = _fake_grok(tmp_path)
    for name in PLANTED:
        monkeypatch.setenv(name, "planted")
    monkeypatch.setenv("QUADRATUS_ENV_PASS", "OPENAI_API_KEY,CLAUDE_CODE_OAUTH_TOKEN,GITHUB_TOKEN")
    assert CP.client_version(str(script), ["--version"]) == "9.9.9"
    seen = _seen(tmp_path, "--version")
    assert "OPENAI_API_KEY" not in seen and "CLAUDE_CODE_OAUTH_TOKEN" not in seen
    assert "GITHUB_TOKEN" in seen
    assert not [name for name in PLANTED if name in seen and name != "GITHUB_TOKEN"]


# -- transport:2: a permission line does not hide a spent window --------------

GROK = "grok:default"
LINE = "permission required: Fetch: file:///tmp/x/README.md was not run, as a headless run cannot ask for approval"
SPENT = {"error": {"message": "You have reached your usage limit. Upgrade to continue.", "status": 429},
         "sessionId": "s-1", "usage": {"input_tokens": 10, "output_tokens": 0}}


@pytest.mark.parametrize("stdout", [f"print(json.dumps({SPENT!r}))\n", ""], ids=["envelope", "stderr only"])
def test_a_spent_window_beside_a_permission_line_marks_the_seat(tmp_path, monkeypatch, stdout):
    body = stdout + f"sys.stderr.write({LINE!r} + '\\n')\n"
    if not stdout:
        body += "sys.stderr.write('Error: You have reached your usage limit. Upgrade to continue.\\n')\n"
    script, _ = T._fake_cli(tmp_path, "grok", body + "sys.exit(1)\n")
    fleet, ledger = T._fleet(monkeypatch, "grok", script)
    with pytest.raises(WindowExhausted):
        fleet.invoke(GROK, "hello")
    assert not fleet.available(GROK) and GROK in fleet.exhausted
    # The block is still on the call's record.
    assert any("permission_block" in (event.diagnostics or {}) for event in ledger.events)


def test_a_permission_block_alone_is_still_the_typed_denial(tmp_path, monkeypatch):
    cancelled = {"stopReason": "cancelled", "text": "I'll fetch the file.", "sessionId": "s"}
    body = f"print(json.dumps({cancelled!r}))\nsys.stderr.write({LINE!r} + '\\n')\nsys.exit(1)\n"
    script, _ = T._fake_cli(tmp_path, "grok", body)
    fleet, _ = T._fleet(monkeypatch, "grok", script)
    with pytest.raises(PermissionBlocked):
        fleet.invoke(GROK, "hello")
    assert fleet.available(GROK)


# -- transport:3: a status stated as a field is a typed fact ------------------

def _turn_failed(error):
    events = [{"type": "thread.started", "thread_id": "t-1"}, {"type": "turn.started"},
              {"type": "turn.failed", "error": error}]
    return "".join(f"print({json.dumps(json.dumps(e))})\n" for e in events) + "sys.exit(1)\n"


@pytest.mark.parametrize("error, status", [
    ({"message": "exceeded retry limit, last status: 503 Service Unavailable"}, 503),
    ({"status": 503}, 503),
    ({"status": 503, "message": "Service Unavailable"}, 503),
    ({"status": 429, "message": "Too Many Requests"}, 429),
])
def test_a_codex_status_field_sets_the_same_facts_as_the_message(tmp_path, monkeypatch, error, status):
    script, runs = T._fake_cli(tmp_path, "codex", _turn_failed(error))
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_OPENAI", str(script))
    provider = CodexCLIProvider("gpt-6.1-sol", workdir=str(tmp_path / "w"), max_retries=4, retry_base_delay=0.0)
    with pytest.raises(ProviderError) as caught:
        provider.generate("review this")
    assert getattr(caught.value, "transport_status", None) == status
    assert getattr(caught.value, "retryable", None) is True
    assert T._runs(runs) == 4                # a call that cannot write is re-sent
    assert (provider.last_diagnostics or {}).get("status") == status


def test_a_codex_recovery_forgets_the_failed_turns_status():
    stream = "\n".join(json.dumps(e) for e in (
        {"type": "turn.failed", "error": {"status": 503}},
        {"type": "turn.completed"},
        {"type": "turn.failed", "error": {"message": "the model stopped"}}))
    with pytest.raises(ProviderError) as caught:
        CP._extract_codex_result(stream)
    assert getattr(caught.value, "transport_status", None) is None
    assert getattr(caught.value, "retryable", None) is None


@pytest.mark.parametrize("envelope", [
    {"error": {"message": "Service Unavailable", "status": 503}},
    {"error": {"message": "Service Unavailable", "statusCode": 503}},
    {"error": {"message": "Service Unavailable", "code": 503}},
    {"error": "Service Unavailable", "status": 503},
])
def test_grok_typed_status_and_ledger_status_come_from_one_reader(envelope):
    stdout = json.dumps(envelope)
    with pytest.raises(ProviderError) as caught:
        CP._extract_grok_result(stdout)
    assert getattr(caught.value, "transport_status", None) == 503
    assert getattr(caught.value, "retryable", None) is True
    assert CP._extract_grok_diagnostics(stdout).get("status") == 503


# -- transport:4: a 529 keeps its type whatever stderr holds ------------------

def _overloaded(stderr):
    envelope = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
                "api_error_status": 529, "result": "API Error: 529 Overloaded.", "session_id": "s",
                "usage": {"input_tokens": 0, "output_tokens": 0}}
    return (f"print(json.dumps({envelope!r}))\n"
            + (f"sys.stderr.write({stderr!r})\n" if stderr else "") + "sys.exit(1)\n")


@pytest.mark.parametrize("stderr", ["", "Error: request failed\n"], ids=["no stderr", "stderr"])
def test_a_529_takes_the_bounded_road(tmp_path, monkeypatch, stderr):
    script, runs = T._fake_cli(tmp_path, "claude", _overloaded(stderr))
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    provider = ClaudeCLIProvider("opus", workdir=str(tmp_path / "w"), max_retries=4, retry_base_delay=0.0)
    with pytest.raises(TransientOverload) as caught:
        provider.generate("review this")
    assert caught.value.status == 529 and getattr(caught.value, "transport_status", None) == 529
    assert T._runs(runs) == 3                # the call and at most two re-sends


@pytest.mark.parametrize("stderr", ["", "Error: request failed\n"], ids=["no stderr", "stderr"])
def test_a_529_with_no_attempt_left_is_not_re_sent(tmp_path, monkeypatch, stderr):
    script, runs = T._fake_cli(tmp_path, "claude", _overloaded(stderr))
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    provider = ClaudeCLIProvider("opus", workdir=str(tmp_path / "w"), max_retries=4, retry_base_delay=0.0)
    budget = RunBudget(RunLimits(max_calls=1, max_reported_tokens=1_000_000, wall_seconds=600))
    provider.run_budget = budget
    with pytest.raises(TransientOverload):
        provider.generate("review this")
    assert T._runs(runs) == 1
    assert not budget.snapshot().get("stop_reason")      # the overload is the stop, not call_limit


# -- transport:5: a refused re-send is not "no call was made" -----------------

def test_a_re_send_refused_by_the_finish_hold_ends_in_the_calls_own_failure(tmp_path, monkeypatch):
    envelope = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "api_error_status": 503,
                "result": "API Error: 503 Service Unavailable", "session_id": "s",
                "usage": {"input_tokens": 0, "output_tokens": 0}}
    script, runs = T._fake_cli(tmp_path, "claude", f"print(json.dumps({envelope!r}))\nsys.exit(1)\n")
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    provider = ClaudeCLIProvider("opus", workdir=str(tmp_path / "w"), max_retries=4, retry_base_delay=0.0)
    budget = RunBudget(RunLimits(max_calls=3, max_reported_tokens=1_000_000, wall_seconds=600,
                                 finish_wall_seconds=0))
    budget.protect_finish(["goal", "audit"])
    provider.run_budget = budget
    with pytest.raises(ProviderError) as caught:
        provider.generate("review this")
    assert not isinstance(caught.value, FinishReserved)
    assert getattr(caught.value, "transport_status", None) == 503
    assert getattr(caught.value, "resend_refused", "")
    assert "not sent again" in str(caught.value)
    assert T._runs(runs) == 1
    assert not budget.snapshot().get("stop_reason")      # the hold is not a latched stop


def test_a_first_call_refused_by_the_finish_hold_is_still_finish_reserved(tmp_path, monkeypatch):
    script, runs = T._fake_cli(tmp_path, "claude", "sys.exit(1)\n")
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    provider = ClaudeCLIProvider("opus", workdir=str(tmp_path / "w"), max_retries=4, retry_base_delay=0.0)
    budget = RunBudget(RunLimits(max_calls=2, max_reported_tokens=1_000_000, wall_seconds=600,
                                 finish_wall_seconds=0))
    budget.protect_finish(["goal", "audit"])
    provider.run_budget = budget
    with pytest.raises(FinishReserved):
        provider.generate("review this")
    assert T._runs(runs) == 0


# -- transport:6: the ledger's class reads what the typed fact reads ----------

def _claude_error(result, *, status=None, output=300):
    payload = {"type": "result", "is_error": True, "subtype": "error_during_execution", "result": result,
               "usage": {"input_tokens": 900, "output_tokens": output}}
    if status is not None:
        payload["api_error_status"] = status
    return json.dumps(payload)


@pytest.mark.parametrize("stdout", [
    _claude_error("I looked at the plan limit code. HTTP 429 handling says: usage limit reached"),
    _claude_error("cannot write status: 503 into the log"),
    _claude_error("the test says: not signed in, HTTP 401"),
])
def test_an_error_envelope_around_model_output_is_classified_by_nothing(stdout):
    with pytest.raises(ProviderError) as caught:
        CP._extract_claude_result(stdout)
    assert all(getattr(caught.value, name, None) is None
               for name in ("window_exhausted", "retryable", "auth_invalid", "transport_status"))
    recorded = CP._extract_claude_diagnostics(stdout)
    assert "failure_class" not in recorded and "status" not in recorded
    assert recorded.get("stop_reason") == "error_during_execution"


@pytest.mark.parametrize("stdout", [
    _claude_error("You've reached your usage limit for this plan.", status=429),
    _claude_error("You've reached your usage limit for this plan.", output=0),
    _claude_error("API Error: 429 temporary capacity", status=429),
    _claude_error("API Error: 503 Service Unavailable", status=503),
    _claude_error("usage limit reached while the service was down", status=503),
    _claude_error("I read the usage limit code and it is fine"),
])
def test_the_ledger_says_cap_exactly_when_the_window_is_typed_as_spent(stdout):
    with pytest.raises(ProviderError) as caught:
        CP._extract_claude_result(stdout)
    spent = getattr(caught.value, "window_exhausted", None) is True
    recorded = CP._extract_claude_diagnostics(stdout)
    assert (recorded.get("failure_class") == "cap") is spent
    assert recorded.get("status") == getattr(caught.value, "transport_status", None)


# -- transport:7: the text states the rule in force ---------------------------

def test_no_text_in_these_files_says_the_engine_never_re_sends():
    root = Path(os.path.dirname(os.path.abspath(CP.__file__)))
    assert "The engine runs no outer retry" not in (root / "cli_providers.py").read_text()
    assert "cannot drift apart" not in (root / "cli_providers.py").read_text()
    assert "The engine adds no retry of its own" not in (
        root.parent / "tests" / "test_client_version_preflight.py").read_text()
