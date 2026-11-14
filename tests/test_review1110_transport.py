"""Regression tests for the transport items of the 2026-11-10 review.

The provider layer no longer decides retry, replay or window exhaustion by
searching text a model can write; an editing call is replayed only after the
project is measured unchanged; and a CLI call cleans up after itself.

The vendor CLIs are faked by small scripts (a real process, a real exit
status, a real pipe), so the launch path is the one a run uses. Nothing here
reaches a vendor or the network.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quadratus import cli_providers, isolated_run, native_sessions, runtime
from quadratus.cli_providers import (
    MAX_ARGV_PROMPT,
    ClaudeCLIProvider,
    CLIOutputTooLarge,
    CodexCLIProvider,
    GrokCLIProvider,
    _extract_claude_result,
    _extract_codex_result,
    _extract_grok_result,
    _launch,
    _own_error_facts,
)
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger
from quadratus.project import Project
from quadratus.providers import (
    PartialWorkSuspected,
    ProviderError,
    carry_transport_facts,
    scratch_root,
)
from quadratus.runtime import Fleet, WindowExhausted, _looks_exhausted
from quadratus.worker_bridge import WorkerBridge
from tests._process_liveness import process_stopped

POSIX = pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups")

OPUS = "claude:opus"
FABLE = "claude:fable"
GROK = "grok:default"


# -- fakes -------------------------------------------------------------------

def _fake_cli(tmp_path, name, body):
    """An executable that stands in for a vendor CLI. Each run appends a line
    to ``<name>.runs`` first, so a test can count how often it was started.
    The ``--version`` probe a provider makes once when it is built is not a
    call: it is answered before the line is written, with no version, so the
    count stays the number of calls."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    runs = tmp_path / f"{name}.runs"
    script = folder / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    sys.exit(0)\n"
        f"open({str(runs)!r}, 'a').write('run\\n')\n"
        "sys.stdin.read()\n"
        + textwrap.dedent(body))
    script.chmod(0o755)
    return script, runs


def _runs(path):
    return len(path.read_text().splitlines()) if path.exists() else 0


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Retries are counted here, not timed: no jitter on top of a zero base
    delay. Only the provider module's own ``random`` is replaced; the real
    ``time.sleep`` stays, since the process cleanup under test relies on it."""
    import types
    monkeypatch.setattr("quadratus.providers.random", types.SimpleNamespace(uniform=lambda low, high: 0.0))
    for vendor in ("CLAUDE", "OPENAI", "GROK"):
        monkeypatch.delenv(f"QUADRATUS_CLI_ARGS_{vendor}", raising=False)
    monkeypatch.delenv("QUADRATUS_CONTAINED", raising=False)
    monkeypatch.delenv("QUADRATUS_NATIVE_DELEGATION", raising=False)


def _project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("print('before')\n")
    return root


def _fleet(monkeypatch, vendor, script, root=None, **settings):
    monkeypatch.setenv(f"QUADRATUS_CLI_BINARY_{vendor.upper()}", str(script))
    conf = Settings(backend="cli", retry_base_delay=0.0, **{"max_retries": 4, "cli_timeout": 30.0, **settings})
    ledger = DelegationLedger()
    fleet = Fleet(conf, project=root, allow_writes=root is not None, delegation_ledger=ledger)
    return fleet, ledger


_CONNECTION_ERROR = "sys.stderr.write('API Error: Connection error (ECONNRESET)\\n')\nsys.exit(1)\n"
_OK = "print(json.dumps({'type': 'result', 'result': 'done\\nCHANGED: []'}))\n"


# -- item 4: a writing call is never replayed blind ----------------------------

def test_a_writing_call_that_edits_and_then_fails_in_transport_runs_once(tmp_path, monkeypatch):
    """The review's repro: the fake edits, prints a connection error and
    exits 1. It ran four times with a write grant; it runs once now."""
    work = tmp_path / "work"
    work.mkdir()
    script, runs = _fake_cli(tmp_path, "claude", "open('edited.txt', 'a').write('edit\\n')\n" + _CONNECTION_ERROR)
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    provider = ClaudeCLIProvider("opus", workdir=str(work), allow_writes=True, max_retries=4,
                                 retry_base_delay=0.0)
    with pytest.raises(PartialWorkSuspected) as caught:
        provider.generate("edit the module")
    assert _runs(runs) == 1
    assert (work / "edited.txt").read_text() == "edit\n", "one edit, not four"
    # The failure passes, and says so as a typed fact a measuring caller may act on.
    assert caught.value.retryable is True and caught.value.interrupted is False


def test_a_read_only_call_keeps_its_retries_on_a_typed_transport_failure(tmp_path, monkeypatch):
    script, runs = _fake_cli(tmp_path, "claude", _CONNECTION_ERROR)
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    provider = ClaudeCLIProvider("opus", workdir=str(tmp_path / "w"), max_retries=3, retry_base_delay=0.0)
    with pytest.raises(ProviderError) as caught:
        provider.generate("review this")
    assert _runs(runs) == 3
    assert not isinstance(caught.value, PartialWorkSuspected)
    assert caught.value.retryable is True, "the wrapper keeps the fact its cause carried"


@pytest.mark.parametrize("stderr_line", [
    "the connection to the 500 widgets was fine, try again tomorrow",   # the old loose words
    "note: I saw 'API Error: Connection error' quoted in a README",     # the shape, not as the whole line
    "rate limit overloaded temporarily 503 502 timeout",
])
def test_loose_words_in_a_failure_no_longer_buy_a_retry(tmp_path, monkeypatch, stderr_line):
    script, runs = _fake_cli(tmp_path, "claude", f"sys.stderr.write({stderr_line!r} + '\\n')\nsys.exit(1)\n")
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    provider = ClaudeCLIProvider("opus", workdir=str(tmp_path / "w"), max_retries=4, retry_base_delay=0.0)
    with pytest.raises(ProviderError):
        provider.generate("review this")
    assert _runs(runs) == 1


def test_model_text_on_stdout_is_never_read_for_a_retry(tmp_path, monkeypatch):
    """A nonzero exit with a normal answer: the answer says "API Error: 529"
    and "connection", and it is the model's. Only stderr is the CLI's voice."""
    body = ("print(json.dumps({'type': 'result', 'result': 'API Error: 529 overloaded, connection reset'}))\n"
            "sys.exit(1)\n")
    script, runs = _fake_cli(tmp_path, "claude", body)
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    provider = ClaudeCLIProvider("opus", workdir=str(tmp_path / "w"), max_retries=4, retry_base_delay=0.0)
    with pytest.raises(ProviderError, match="exited 1"):
        provider.generate("review this")
    assert _runs(runs) == 1


def test_fleet_replays_a_failed_edit_once_when_the_project_is_measured_unchanged(tmp_path, monkeypatch):
    root = _project(tmp_path)
    body = (f"first = len(open({str(tmp_path / 'claude.runs')!r}).read().splitlines()) == 1\n"
            "if first:\n"
            "    sys.stderr.write('API Error: Connection error (ECONNRESET)\\n')\n"
            "    sys.exit(1)\n" + _OK)
    script, runs = _fake_cli(tmp_path, "claude", body)
    fleet, ledger = _fleet(monkeypatch, "claude", script, root)
    said = []
    fleet.progress = said.append
    assert fleet.invoke(OPUS, "edit", allow_writes=True).startswith("done")
    assert _runs(runs) == 2
    assert [event.attempt for event in ledger.events] == [1, 2]
    assert [event.outcome for event in ledger.events] == ["RuntimeError", "ok"]
    assert any("measured unchanged" in line for line in said)


def test_fleet_does_not_replay_a_failed_edit_that_changed_the_project(tmp_path, monkeypatch):
    root = _project(tmp_path)
    script, runs = _fake_cli(tmp_path, "claude", "open('app.py', 'a').write('# half an edit\\n')\n" + _CONNECTION_ERROR)
    fleet, _ = _fleet(monkeypatch, "claude", script, root)
    with pytest.raises(PartialWorkSuspected):
        fleet.invoke(OPUS, "edit", allow_writes=True)
    assert _runs(runs) == 1
    assert (root / "app.py").read_text().count("# half an edit") == 1, "kept, and not applied twice"


def test_fleet_stops_after_one_replay(tmp_path, monkeypatch):
    """Still failing, still unchanged: an ordinary failed call that left
    nothing behind, so the session may recover it as one. Two runs, not four."""
    root = _project(tmp_path)
    script, runs = _fake_cli(tmp_path, "claude", _CONNECTION_ERROR)
    fleet, _ = _fleet(monkeypatch, "claude", script, root)
    with pytest.raises(ProviderError) as caught:
        fleet.invoke(OPUS, "edit", allow_writes=True)
    assert _runs(runs) == 2
    assert not isinstance(caught.value, PartialWorkSuspected)
    assert "measured unchanged" in str(caught.value)


def test_fleet_does_not_replay_an_untyped_failure_and_reports_what_it_measured(tmp_path, monkeypatch):
    root = _project(tmp_path)
    script, runs = _fake_cli(tmp_path, "claude", "sys.stderr.write('error: unknown option --nope\\n')\nsys.exit(2)\n")
    fleet, _ = _fleet(monkeypatch, "claude", script, root)
    with pytest.raises(ProviderError) as caught:
        fleet.invoke(OPUS, "edit", allow_writes=True)
    assert _runs(runs) == 1
    assert not isinstance(caught.value, PartialWorkSuspected), "nothing was written, by measurement"
    # The same failure after an edit is the partial-work stop.
    script, runs = _fake_cli(tmp_path, "claude", "open('new.py', 'w').write('x = 1\\n')\n"
                             "sys.stderr.write('error: unknown option --nope\\n')\nsys.exit(2)\n")
    fleet, _ = _fleet(monkeypatch, "claude", script, root)
    with pytest.raises(PartialWorkSuspected):
        fleet.invoke(OPUS, "edit", allow_writes=True)


@POSIX
def test_fleet_never_replays_a_timed_out_edit_even_when_nothing_changed(tmp_path, monkeypatch):
    """A stopped call is not a failed one: what it did before the stop is
    unknown to the transport, and the session inspects before anything else."""
    root = _project(tmp_path)
    script, runs = _fake_cli(tmp_path, "claude", "time.sleep(60)\n")
    fleet, _ = _fleet(monkeypatch, "claude", script, root, cli_timeout=1.0)
    with pytest.raises(PartialWorkSuspected) as caught:
        fleet.invoke(OPUS, "edit", allow_writes=True)
    assert _runs(runs) == 1
    assert caught.value.interrupted is True


@POSIX
def test_a_cli_ended_by_a_signal_was_stopped_not_failed(tmp_path, monkeypatch):
    """Killed from outside, with nothing measurably changed: still not
    replayed and not downgraded to an ordinary failure."""
    root = _project(tmp_path)
    script, runs = _fake_cli(tmp_path, "claude", "import signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    fleet, _ = _fleet(monkeypatch, "claude", script, root)
    with pytest.raises(PartialWorkSuspected) as caught:
        fleet.invoke(OPUS, "edit", allow_writes=True)
    assert _runs(runs) == 1 and caught.value.interrupted is True
    assert "exited -9" in str(caught.value)


def test_an_error_envelope_with_a_retryable_status_follows_the_same_rules(tmp_path, monkeypatch):
    """claude's own envelope at status 529. Read-only: retried. With a write
    grant: never inside the provider, once by the fleet on an unchanged
    project, and the partial-work stop on a changed one."""
    overloaded = ("print(json.dumps({'type': 'result', 'is_error': True, 'api_error_status': 529, "
                  "'result': 'API Error: 529 Overloaded'}))\nsys.exit(1)\n")
    script, runs = _fake_cli(tmp_path, "claude", overloaded)
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    reader = ClaudeCLIProvider("opus", workdir=str(tmp_path / "w"), max_retries=3, retry_base_delay=0.0)
    with pytest.raises(ProviderError) as caught:
        reader.generate("review")
    assert _runs(runs) == 3 and caught.value.retryable is True and caught.value.transport_status == 529
    runs.unlink()
    writer = ClaudeCLIProvider("opus", workdir=str(tmp_path / "w"), allow_writes=True, max_retries=3,
                               retry_base_delay=0.0)
    with pytest.raises(ProviderError):
        writer.generate("edit")
    assert _runs(runs) == 1
    runs.unlink()
    root = _project(tmp_path)
    fleet, _ = _fleet(monkeypatch, "claude", script, root)
    with pytest.raises(ProviderError) as caught:
        fleet.invoke(OPUS, "edit", allow_writes=True)
    assert _runs(runs) == 2 and not isinstance(caught.value, PartialWorkSuspected)
    runs.unlink()
    script, runs = _fake_cli(tmp_path, "claude", "open('app.py', 'a').write('# edit\\n')\n" + overloaded)
    fleet, _ = _fleet(monkeypatch, "claude", script, root)
    with pytest.raises(PartialWorkSuspected):
        fleet.invoke(OPUS, "edit", allow_writes=True)
    assert _runs(runs) == 1


def test_a_replay_also_needs_the_callers_second_measurement(tmp_path, monkeypatch):
    """The measured source does not see .git, build output or links. A caller
    that lists those as well passes its answer in, and "unchanged" means
    both. A second answer that is not True is a changed project: the
    partial-work stop, never the ordinary failure a session's lead recovery
    (which inspects the source alone) would hand to another lead."""
    import types
    root = _project(tmp_path)
    script, _ = _fake_cli(tmp_path, "claude", _OK)
    fleet, _ = _fleet(monkeypatch, "claude", script, root)
    calls, asked = [], []

    def generate(key, view, prompt, system, **kwargs):
        calls.append(kwargs.get("first_attempt", 1))
        if calls[-1] == 1:
            error = ProviderError("claude reported an error: API Error: 529 Overloaded")
            error.retryable = True
            raise error
        return "done"

    monkeypatch.setattr(fleet, "_generate", generate)
    view = types.SimpleNamespace(retry_base_delay=0.0)
    before = fleet.project.contents()
    assert fleet._generate_editing(OPUS, view, "edit", before, "role") == "done"
    assert calls == [1, 2], "no second measurement: the source alone decides, as before"

    def yes():
        asked.append(1)
        return True

    calls.clear()
    assert fleet._generate_editing(OPUS, view, "edit", before, "role", unchanged=yes) == "done"
    assert calls == [1, 2] and asked == [1]
    for answer in (lambda: False, lambda: 1 / 0, lambda: "unchanged"):
        calls.clear()
        with pytest.raises(PartialWorkSuspected) as caught:
            fleet._generate_editing(OPUS, view, "edit", before, "role", unchanged=answer)
        assert calls == [1], "changed, unknown, or anything but True: not sent again"
        assert caught.value.interrupted is False and "outside the measured source" in str(caught.value)

    # The same for a failure that does not pass. With the source unchanged it
    # was downgraded to an ordinary failure "measured unchanged" without the
    # second measurement being asked at all.
    def fails_plainly(key, view, prompt, system, **kwargs):
        calls.append(kwargs.get("first_attempt", 1))
        raise PartialWorkSuspected("claude did not complete (exited 2: unknown option)",
                                   cause=RuntimeError("exited 2"), interrupted=False)

    monkeypatch.setattr(fleet, "_generate", fails_plainly)
    calls.clear()
    with pytest.raises(PartialWorkSuspected):
        fleet._generate_editing(OPUS, view, "edit", before, "role", unchanged=lambda: False)
    calls.clear()
    with pytest.raises(ProviderError) as caught:
        fleet._generate_editing(OPUS, view, "edit", before, "role", unchanged=lambda: True)
    assert calls == [1] and not isinstance(caught.value, PartialWorkSuspected)
    assert "measured unchanged" in str(caught.value)
    monkeypatch.setattr(fleet, "_generate", generate)
    # A changed source is the partial-work stop before anyone is asked more.
    calls.clear()
    asked.clear()
    (root / "app.py").write_text("print('half an edit')\n")
    with pytest.raises(PartialWorkSuspected):
        fleet._generate_editing(OPUS, view, "edit", before, "role", unchanged=yes)
    assert calls == [1] and asked == []


def test_a_partial_work_stop_is_not_replayable_unless_its_raiser_says_so():
    """The careful reading is the default for anyone who raises it bare."""
    assert PartialWorkSuspected("timed out after 900s").interrupted is True
    assert PartialWorkSuspected("timed out after 900s").retryable is False


# -- item 5: window exhaustion and "retryable" are typed facts ------------------

_LIMIT = "You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai"


def _claude_error(**fields):
    with pytest.raises(ProviderError) as caught:
        _extract_claude_result(json.dumps({"type": "result", "is_error": True, **fields}))
    return caught.value


def test_exhaustion_is_never_read_from_a_message():
    """The old test of an exhausted window was a substring of str(exc)."""
    assert not _looks_exhausted(ProviderError(_LIMIT))
    assert not _looks_exhausted(RuntimeError("usage limit reached; weekly limit; quota exceeded"))
    assert not _looks_exhausted("You've reached your Fable limit.")
    typed = ProviderError("anything at all")
    typed.window_exhausted = True
    assert _looks_exhausted(typed)
    typed.window_exhausted = "yes"        # exactly True, or it is not the fact
    assert not _looks_exhausted(typed)


def test_a_cancelled_turn_that_talks_about_limits_is_not_an_exhausted_window():
    """The review's scenario: narration is quoted in the failure for a person
    to read, and carries no typed fact for anything to act on."""
    envelope = json.dumps({"stopReason": "cancelled",
                           "text": "limit reached: you've reached your quota. API Error: 529. usage limit."})
    with pytest.raises(ProviderError) as caught:
        _extract_grok_result(envelope)
    error = caught.value
    assert "limit reached" in str(error), "the narration stays, for diagnostics"
    assert not _looks_exhausted(error)
    assert getattr(error, "retryable", False) is False
    assert not GrokCLIProvider._retryable(object.__new__(GrokCLIProvider), error)


def test_the_claude_limit_envelope_sets_the_typed_fact():
    seen = _claude_error(api_error_status=429, result=_LIMIT)
    assert seen.window_exhausted is True and seen.transport_status == 429
    assert getattr(seen, "retryable", False) is False, "a spent window is not retried"
    # No status, and the model wrote nothing: the text can only be the CLI's.
    silent = _claude_error(result="You've reached your Fable limit.",
                           usage={"input_tokens": 0, "output_tokens": 0})
    assert silent.window_exhausted is True


def test_a_transient_429_is_retryable_and_not_an_exhausted_window():
    seen = _claude_error(api_error_status=429, result="API Error: 429 rate_limit_error")
    assert seen.retryable is True and not _looks_exhausted(seen)
    assert _claude_error(api_error_status=529, result="Overloaded").retryable is True
    final = _claude_error(api_error_status=400, result="API Error: 400 usage limit reached, try again")
    assert not _looks_exhausted(final) and getattr(final, "retryable", False) is False


def test_an_error_envelope_around_model_output_says_nothing():
    """No status and the model produced output: the text may be the model's,
    so nothing is read from it."""
    for usage in ({"input_tokens": 9, "output_tokens": 40}, None, {"output_tokens": False}):
        seen = _claude_error(result=_LIMIT + " API Error: 529", **({"usage": usage} if usage else {}))
        assert not _looks_exhausted(seen)
        assert getattr(seen, "retryable", False) is False


@pytest.mark.parametrize("text, status, expected", [
    ("API Error: 529 {\"type\":\"overloaded_error\"}", None, {"retryable": True, "transport_status": 529}),
    ("Error: API Error: Connection error (ECONNRESET)", None, {"retryable": True}),
    ("TypeError: fetch failed", None, {"retryable": True}),
    ("read ECONNRESET", None, {"retryable": True}),
    ("getaddrinfo EAI_AGAIN api.example.com", None, {"retryable": True}),
    ("unexpected status 503 Service Unavailable", None, {"retryable": True, "transport_status": 503}),
    ("exceeded retry limit, last status: 429 Too Many Requests", None, {"retryable": True, "transport_status": 429}),
    ("stream disconnected before completion: idle timeout", None, {"retryable": True}),
    ("unexpected status 401 Unauthorized", None, {"transport_status": 401}),
    ("You've hit your usage limit. Upgrade to continue.", None, {"window_exhausted": True}),
    ("weekly limit reached", 429, {"window_exhausted": True, "transport_status": 429}),
    ("weekly limit reached", 500, {"retryable": True, "transport_status": 500}),
    ("anything", 502, {"retryable": True, "transport_status": 502}),
    # Sentences that only mention the old trigger words.
    ("I got a 500 from the connection, try again", None, {}),
    ("the test asserts on 'API Error: 529' text", None, {}),
    ("temporarily overloaded; rate limit", None, {}),
    ("", None, {}),
])
def test_the_cli_error_report_table(text, status, expected):
    assert _own_error_facts(text, status) == expected


def test_codex_turn_failures_are_typed_from_codex_own_event():
    def failed(message):
        stream = "\n".join(json.dumps(event) for event in [
            {"type": "thread.started", "thread_id": "x"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "usage limit, limit reached"}},
            {"type": "turn.failed", "error": {"message": message}}])
        with pytest.raises(ProviderError) as caught:
            _extract_codex_result(stream)
        return caught.value
    assert failed("exceeded retry limit, last status: 429 Too Many Requests").retryable is True
    assert failed("You've hit your usage limit. Try again in 2 days.").window_exhausted is True
    plain = failed("the sandbox denied the command")
    assert not _looks_exhausted(plain), "the model's own message above is never read"
    assert getattr(plain, "retryable", False) is False


def test_a_spent_claude_window_reaches_the_fleet_through_a_real_process(tmp_path, monkeypatch):
    body = ("print(json.dumps({'type': 'result', 'is_error': True, 'api_error_status': 429, "
            f"'result': {_LIMIT!r}, 'usage': {{'input_tokens': 0, 'output_tokens': 0}}}}))\n"
            "sys.stderr.write('exit after API error\\n')\nsys.exit(1)\n")
    script, runs = _fake_cli(tmp_path, "claude", body)
    fleet, _ = _fleet(monkeypatch, "claude", script)
    with pytest.raises(WindowExhausted) as caught:
        fleet.invoke(FABLE, "hello")
    assert caught.value.window_exhausted is True, "the attribute session.py reads"
    assert _runs(runs) == 1, "a spent window is not retried"
    assert not fleet.available(FABLE) and fleet.available(OPUS)


def test_narration_about_limits_does_not_take_a_model_off_the_roster(tmp_path, monkeypatch):
    body = ("print(json.dumps({'stopReason': 'cancelled', 'sessionId': 's-1', "
            "'text': 'limit reached. you have reached your usage limit. upgrade to continue.'}))\n")
    script, runs = _fake_cli(tmp_path, "grok", body)
    fleet, _ = _fleet(monkeypatch, "grok", script)
    with pytest.raises(ProviderError) as caught:
        fleet.invoke(GROK, "hello")
    assert not isinstance(caught.value, WindowExhausted)
    assert fleet.available(GROK) and fleet.exhausted == {}
    assert _runs(runs) == 1


def test_the_wrapper_keeps_the_facts_and_the_diagnostics(tmp_path, monkeypatch):
    """A nonzero exit with stderr wraps the envelope's error in a new one;
    the wrapper is what the caller sees."""
    body = ("print(json.dumps({'type': 'result', 'is_error': True, 'api_error_status': 429, 'num_turns': 1, "
            f"'result': {_LIMIT!r}, 'usage': {{'input_tokens': 0, 'output_tokens': 0}}}}))\n"
            "sys.stderr.write('some stderr\\n')\nsys.exit(1)\n")
    script, _ = _fake_cli(tmp_path, "claude", body)
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    provider = ClaudeCLIProvider("fable", workdir=str(tmp_path / "w"), max_retries=1)
    with pytest.raises(ProviderError) as caught:
        provider.generate("hello")
    assert "some stderr" in str(caught.value)
    assert caught.value.window_exhausted is True
    assert caught.value.diagnostics == provider.last_diagnostics


_STDERR_LIMIT = ("sys.stderr.write(\"Error: You've hit your usage limit. Upgrade to continue.\\n\")\n"
                 "sys.exit(1)\n")
SOL = "openai:gpt-5.6-sol"


@pytest.mark.parametrize("vendor, key", [("grok", GROK), ("openai", SOL), ("claude", OPUS)])
def test_a_spent_window_reported_on_stderr_alone_is_still_recognised(tmp_path, monkeypatch, vendor, key):
    """A CLI that dies before it prints an envelope has only stderr to say
    why. That was recognised by searching the whole message; with the search
    gone, grok and codex lost it: their extractors report "no envelope" as a
    failure of their own, and the wrapper around it carried no fact."""
    script, runs = _fake_cli(tmp_path, vendor, _STDERR_LIMIT)
    fleet, _ = _fleet(monkeypatch, vendor, script)
    with pytest.raises(WindowExhausted) as caught:
        fleet.invoke(key, "hello")
    assert caught.value.window_exhausted is True
    assert not fleet.available(key), "routed around for the rest of the run"
    assert _runs(runs) == 1, "a spent window is not retried"


_TRACE = "".join(f"    at frame{i} (node:internal/x:{i}:1)\n" for i in range(14)) + "\nNode.js v22.0.0\n"


@pytest.mark.parametrize("vendor, key", [("claude", OPUS), ("grok", GROK), ("openai", SOL)])
def test_a_limit_named_before_a_stack_trace_is_still_a_spent_window(tmp_path, monkeypatch, vendor, key):
    """A runtime names the error first and prints its stack after it. Read
    from the last ten lines alone, the limit was out of sight and a spent
    window was a plain transport error; the search this replaced read the
    start of stderr and saw it."""
    body = ("sys.stderr.write(\"Error: You've hit your usage limit. Upgrade to continue.\\n\" + "
            f"{_TRACE!r})\nsys.exit(1)\n")
    script, runs = _fake_cli(tmp_path, vendor, body)
    fleet, _ = _fleet(monkeypatch, vendor, script)
    with pytest.raises(WindowExhausted):
        fleet.invoke(key, "hello")
    assert not fleet.available(key) and _runs(runs) == 1


def test_the_report_is_read_at_both_ends_and_only_in_whole_lines():
    trace = [f"    at frame{i}" for i in range(30)]
    assert _own_error_facts("\n".join(["TypeError: fetch failed"] + trace)) == {"retryable": True}
    assert _own_error_facts("\n".join(trace + ["API Error: 529 Overloaded"])) == {
        "retryable": True, "transport_status": 529}
    # The middle of a long report is not read: it is bounded at both ends.
    middle = trace[:15] + ["API Error: 529 Overloaded", "usage limit reached"] + trace[:15]
    assert _own_error_facts("\n".join(middle)) == {}
    # Short stderr goes whole; long stderr goes as its two ends.
    assert cli_providers._own_stderr("first\nlast") == "first\nlast"
    assert cli_providers._own_stderr(None) == ""
    long = "Error: usage limit reached\n" + "x" * 5000 + "\nNode.js v22.0.0\n"
    assert _own_error_facts(cli_providers._own_stderr(long)) == {"window_exhausted": True}
    long = "warning: deprecated flag\n" + "x" * 5000 + "\nAPI Error: 529 Overloaded\n"
    assert _own_error_facts(cli_providers._own_stderr(long)) == {"retryable": True, "transport_status": 529}
    assert _own_error_facts(cli_providers._own_stderr("x" * 5000)) == {}, "one endless line is nobody's report"
    # A cut that lands inside a line leaves the end of a sentence, and the
    # end of a sentence that quotes a shape is not that shape.
    quote = "API Error: 529 overloaded\n"
    stderr = ("x" * 3000 + "\nnote: the fixture quotes " + quote
              + "y" * (cli_providers.STDERR_TAIL - len(quote)))
    assert stderr[-cli_providers.STDERR_TAIL:].startswith("API Error: 529"), "the cut lands on the quote"
    assert _own_error_facts(cli_providers._own_stderr(stderr)) == {}
    # The same line, whole, at the same place, is read.
    whole = "x" * 3000 + "\n" + quote + "y" * (cli_providers.STDERR_TAIL - len(quote))
    assert _own_error_facts(cli_providers._own_stderr(whole)) == {"retryable": True, "transport_status": 529}


def test_a_codex_stream_with_nothing_in_it_leaves_the_report_to_stderr(tmp_path, monkeypatch):
    """The turn starts and ends with no message and no error item: stdout
    says nothing about why, so on a failed exit stderr is read. On a clean
    exit nothing is, and the failure keeps its hint."""
    events = ("print(json.dumps({'type': 'thread.started', 'thread_id': 't'}))\n"
              "print(json.dumps({'type': 'turn.started'}))\n")
    script, runs = _fake_cli(tmp_path, "openai", events + _STDERR_LIMIT)
    fleet, _ = _fleet(monkeypatch, "openai", script)
    with pytest.raises(WindowExhausted):
        fleet.invoke(SOL, "hello")
    assert not fleet.available(SOL) and _runs(runs) == 1
    runs.unlink()
    script, runs = _fake_cli(tmp_path, "openai", events + "sys.stderr.write('usage limit reached\\n')\n")
    fleet, _ = _fleet(monkeypatch, "openai", script)
    with pytest.raises(ProviderError, match="no assistant message") as caught:
        fleet.invoke(SOL, "hello")
    assert not isinstance(caught.value, WindowExhausted) and fleet.available(SOL)


@pytest.mark.parametrize("vendor, key, provider_class", [
    ("grok", GROK, GrokCLIProvider), ("openai", SOL, CodexCLIProvider)])
def test_a_transport_line_on_stderr_with_no_envelope_is_typed_for_every_vendor(
        tmp_path, monkeypatch, vendor, key, provider_class):
    script, runs = _fake_cli(tmp_path, vendor, _CONNECTION_ERROR)
    monkeypatch.setenv(f"QUADRATUS_CLI_BINARY_{vendor.upper()}", str(script))
    provider = provider_class(key.partition(":")[2] if vendor == "openai" else "",
                              workdir=str(tmp_path / "w"), max_retries=3, retry_base_delay=0.0)
    with pytest.raises(ProviderError) as caught:
        provider.generate("review this")
    assert caught.value.retryable is True and _runs(runs) == 3
    # The same exit with a sentence that only mentions the words: final.
    runs.unlink()
    script, runs = _fake_cli(tmp_path, vendor, "sys.stderr.write('the connection to the 500 widgets, try again\\n')\n"
                                              "sys.exit(1)\n")
    with pytest.raises(ProviderError) as caught:
        provider.generate("review this")
    assert getattr(caught.value, "retryable", False) is False and _runs(runs) == 1


def test_an_envelope_that_reported_is_not_overruled_from_stderr(tmp_path, monkeypatch):
    """A cancelled turn printed its envelope, so the CLI did report: stderr
    beside it is not read, whatever it says. Only a stdout with no report at
    all hands the classification to stderr."""
    body = ("print(json.dumps({'stopReason': 'cancelled', 'sessionId': 's-1', 'text': 'I was about to edit'}))\n"
            "sys.stderr.write('usage limit reached; upgrade to continue\\n')\nsys.exit(1)\n")
    script, runs = _fake_cli(tmp_path, "grok", body)
    fleet, _ = _fleet(monkeypatch, "grok", script)
    with pytest.raises(ProviderError) as caught:
        fleet.invoke(GROK, "hello")
    assert not isinstance(caught.value, WindowExhausted)
    assert "usage limit reached" in str(caught.value), "quoted for a person, classified by nothing"
    assert fleet.available(GROK) and _runs(runs) == 1


@pytest.mark.parametrize("vendor, key", [("claude", OPUS), ("grok", GROK), ("openai", SOL)])
def test_a_spent_window_met_by_an_editing_call_is_marked(tmp_path, monkeypatch, vendor, key):
    """The provider turns a failed writing call into the partial-work stop.
    That stop dropped the facts its cause carried, so the fleet never marked
    the window: the failure said ``window_exhausted`` and the model stayed on
    the roster."""
    root = _project(tmp_path)
    script, runs = _fake_cli(tmp_path, vendor, _STDERR_LIMIT)
    fleet, _ = _fleet(monkeypatch, vendor, script, root)
    with pytest.raises(WindowExhausted) as caught:
        fleet.invoke(key, "edit", allow_writes=True)
    assert caught.value.window_exhausted is True
    assert not fleet.available(key)
    assert _runs(runs) == 1, "never replayed: the window does not come back by asking again"


def test_the_partial_work_stop_keeps_the_facts_of_its_cause(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    spent = RuntimeError("Claude CLI exited 1: limit")
    spent.window_exhausted, spent.transport_status = True, 429

    def launch(argv, **kwargs):
        raise spent

    monkeypatch.setattr(cli_providers, "_launch", launch)
    writer = ClaudeCLIProvider("opus", workdir=str(tmp_path), allow_writes=True, max_retries=3,
                               retry_base_delay=0.0)
    with pytest.raises(PartialWorkSuspected) as caught:
        writer.generate("edit")
    stop = caught.value
    assert stop.window_exhausted is True and stop.transport_status == 429 and stop.cause is spent
    assert stop.retryable is False and stop.interrupted is False
    # A stopped call is never marked as one that passes, whatever its cause carries.
    cut = cli_providers.CLIOutputTooLarge("stdout", 10, "x" * 50)
    cut.retryable = True
    spent = cut
    with pytest.raises(PartialWorkSuspected) as caught:
        writer.generate("edit")
    assert caught.value.interrupted is True and caught.value.retryable is False


def test_carrying_facts_copies_only_what_is_set():
    source = RuntimeError("x")
    source.retryable, source.window_exhausted, source.transport_status = True, False, 503
    target = carry_transport_facts(ProviderError("wrapped"), source)
    assert target.retryable is True and target.transport_status == 503
    assert not hasattr(target, "window_exhausted") and not hasattr(target, "auth_invalid")
    assert not hasattr(carry_transport_facts(ProviderError("bare"), None), "retryable")


# -- item 6: nothing outlives a call, and a held pipe does not hang one ----------

_RECORD_SELF = (
    "import json, os, pathlib, sys, time\n"
    "stat = pathlib.Path('/proc/self/stat')\n"
    "start = stat.read_text().rpartition(')')[2].split()[19] if sys.platform.startswith('linux') else None\n"
    "pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(), start]))\n"
    "time.sleep(120)\n"
)


def _launcher(marker, popen_extra=""):
    """A launcher that starts a long-lived child, waits until the child has
    recorded itself, prints its whole answer and exits 0."""
    return (
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {_RECORD_SELF!r}, {str(marker)!r}]{popen_extra})\n"
        f"while not os.path.exists({str(marker)!r}) or not open({str(marker)!r}).read().endswith(']'):\n"
        "    time.sleep(0.02)\n"
        "print('the complete answer', flush=True)\n"
    )


def _stopped_within(marker, seconds=5.0):
    pid, start = json.loads(marker.read_text())
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if process_stopped(pid, start):
            return True
        time.sleep(0.05)
    return False


@POSIX
def test_a_child_holding_the_pipe_does_not_turn_a_finished_call_into_a_timeout(tmp_path):
    """The launcher exits at once; its child keeps stdout open. That was a
    TimeoutExpired with the complete answer thrown away."""
    marker = tmp_path / "child.json"
    started = time.monotonic()
    done = _launch([sys.executable, "-c", _launcher(marker)], timeout=60, cwd=str(tmp_path))
    assert time.monotonic() - started < 20
    assert done.returncode == 0 and done.stdout == "the complete answer\n"
    assert _stopped_within(marker), "the child the launcher left behind is stopped"


@POSIX
def test_a_background_child_is_stopped_after_a_normal_exit_even_with_no_pipe(tmp_path):
    """A dev server a model started and forgot: its output goes elsewhere, so
    nothing waited on it, and it kept running against the project."""
    marker = tmp_path / "child.json"
    quiet = ", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
    done = _launch([sys.executable, "-c", _launcher(marker, quiet)], timeout=60, cwd=str(tmp_path))
    assert done.returncode == 0 and done.stdout == "the complete answer\n"
    assert _stopped_within(marker)


@POSIX
def test_a_child_that_left_the_group_is_not_touched_and_is_not_waited_for(tmp_path):
    marker = tmp_path / "child.json"
    started = time.monotonic()
    done = _launch([sys.executable, "-c", _launcher(marker, ", start_new_session=True")],
                   timeout=60, cwd=str(tmp_path))
    pid, start = json.loads(marker.read_text())
    try:
        assert time.monotonic() - started < 20, "a bounded drain, not a wait for end of file"
        assert done.returncode == 0 and done.stdout == "the complete answer\n"
        assert not process_stopped(pid, start), "outside the group is outside what the call owns"
    finally:
        os.kill(pid, signal.SIGKILL)


@POSIX
def test_an_ordinary_call_keeps_its_exit_status_output_and_stdin(tmp_path):
    script = ("import sys\ndata = sys.stdin.read()\nsys.stdout.write(data.upper())\n"
              "sys.stderr.write('warn\\r\\nline\\n')\nsys.exit(3)\n")
    done = _launch([sys.executable, "-c", script], input="héllo\n", timeout=30, cwd=str(tmp_path))
    assert (done.returncode, done.stdout, done.stderr) == (3, "HÉLLO\n", "warn\nline\n")


def _slow_reads(monkeypatch, seconds):
    """Make this module's pipe reads slow, as a starved reader thread's are."""
    real = os.read

    class SlowOS:
        def __getattr__(self, name):
            if name != "read":
                return getattr(os, name)

            def read(descriptor, size):
                block = real(descriptor, size)
                time.sleep(seconds)
                return block
            return read

    monkeypatch.setattr(cli_providers, "os", SlowOS())


@POSIX
def test_a_reader_that_is_behind_is_never_returned_as_a_finished_call(tmp_path, monkeypatch):
    """The wait after a normal exit is bounded so a held pipe cannot hang a
    call. Bounded by time alone, it also returned exit 0 with whatever a slow
    reader had got to: 196,608 of 300,000 bytes here. A cut stream can still
    parse (the last whole line of a codex stream may be a progress note), so
    that was a wrong answer reported as a finished call."""
    monkeypatch.setattr(cli_providers, "_EOF_GRACE", 0.05)
    monkeypatch.setattr(cli_providers, "_DRAIN_GRACE", 0.2)
    _slow_reads(monkeypatch, 0.4)
    size = 300_000
    script = f"import sys; sys.stdout.write('x' * {size}); sys.stdout.flush()"
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        _launch([sys.executable, "-c", script], timeout=60, cwd=str(tmp_path))
    assert "could not be read to the end" in caught.value.reason and "stdout" in caught.value.reason
    assert 0 < len(caught.value.output) < size, "what was read is kept for the caller's accounting"


@POSIX
def test_a_slow_reader_that_does_catch_up_returns_the_whole_answer(tmp_path, monkeypatch):
    """Slow is not behind: within the wait, every byte arrives."""
    _slow_reads(monkeypatch, 0.05)
    size = 300_000
    script = f"import sys; sys.stdout.write('x' * {size}); sys.stdout.flush()"
    done = _launch([sys.executable, "-c", script], timeout=60, cwd=str(tmp_path))
    assert done.returncode == 0 and len(done.stdout) == size


@POSIX
def test_the_last_bytes_before_exit_are_read_even_while_something_holds_the_pipe(tmp_path):
    """The reader stops at an empty pipe once the launcher is gone. Empty has
    to mean empty after the launcher's last write, every time."""
    size, holders = 250_000, []
    try:
        for run in range(12):
            # The holder leaves the group, so nothing stops it and the pipe
            # stays open for the whole of the read.
            marker = tmp_path / f"child-{run}.json"
            script = _launcher(marker, ", start_new_session=True").replace(
                "print('the complete answer', flush=True)\n",
                f"sys.stdout.write('y' * {size} + 'END')\nsys.stdout.flush()\n")
            started = time.monotonic()
            done = _launch([sys.executable, "-c", script], timeout=60, cwd=str(tmp_path))
            holders.append(json.loads(marker.read_text())[0])
            assert done.returncode == 0
            assert len(done.stdout) == size + 3 and done.stdout.endswith("END"), f"run {run}"
            assert time.monotonic() - started < 20
    finally:
        for pid in holders:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def test_an_unfinished_read_reaches_the_caller_as_a_stopped_call(tmp_path, monkeypatch):
    """Named for what it was, retried like a timeout when the call could not
    write, and the partial-work stop when it could."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    calls = []

    def launch(argv, **kwargs):
        calls.append(1)
        cut = subprocess.TimeoutExpired(argv, 2.25, output='{"type": "result", "resu', stderr="")
        cut.reason = "exited 0, but its stdout could not be read to the end"
        raise cut

    monkeypatch.setattr(cli_providers, "_launch", launch)
    reader = ClaudeCLIProvider("opus", workdir=str(tmp_path), max_retries=2, retry_base_delay=0.0)
    with pytest.raises(ProviderError, match="could not be read to the end") as caught:
        reader.generate("review")
    assert len(calls) == 2 and "timed out after" not in str(caught.value)
    writer = ClaudeCLIProvider("opus", workdir=str(tmp_path), allow_writes=True, max_retries=2,
                               retry_base_delay=0.0)
    with pytest.raises(PartialWorkSuspected) as caught:
        writer.generate("edit")
    assert len(calls) == 3 and caught.value.interrupted is True and caught.value.retryable is False


def test_a_failed_read_is_not_a_finished_stream():
    """A reader that stopped on an error has not read to the end, whether or
    not the pipe can be polled."""
    read_end, write_end = os.pipe()
    os.write(write_end, b"partial")
    stream = os.fdopen(read_end, "rb", buffering=0)
    capture = cli_providers._Capture(stream, 1024, threading.Event())
    capture.closing.set()
    capture.thread.join(5)
    assert capture.drained.is_set() and not capture.unfinished() and capture.text() == "partial"
    os.close(write_end)

    class Broken:
        def fileno(self):
            raise ValueError("I/O operation on closed file")

        def close(self):
            pass

    broken = cli_providers._Capture(Broken(), 1024, threading.Event())
    broken.thread.join(5)
    assert not broken.thread.is_alive() and broken.unfinished()


# -- item 7: harness scratch is private and outside the shared temp tree ---------

def test_the_scratch_root_defaults_to_a_private_folder_under_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("QUADRATUS_SCRATCH_DIR", raising=False)
    root = Path(scratch_root())
    assert root == home / ".cache" / "quadratus" / "scratch"
    for folder in (root, root.parent, root.parent.parent):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700, folder
    assert scratch_root() == str(root), "and again, once it exists"


def test_the_scratch_root_is_refused_when_it_is_not_private(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(shared))
    with pytest.raises(ProviderError, match="QUADRATUS_SCRATCH_DIR") as caught:
        scratch_root()
    assert "open to other users" in str(caught.value)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755, "refused, not repaired"
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(link))
    with pytest.raises(ProviderError, match="QUADRATUS_SCRATCH_DIR"):
        scratch_root()
    (tmp_path / "file").write_text("x")
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(tmp_path / "file"))
    with pytest.raises(ProviderError, match="QUADRATUS_SCRATCH_DIR"):
        scratch_root()


@pytest.fixture
def private_scratch(tmp_path, monkeypatch):
    root = tmp_path / "scratch-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(root))
    return root


def test_source_copies_live_under_the_scratch_root_and_are_removed(tmp_path, private_scratch):
    project = Project(_project(tmp_path))
    with project.snapshot() as copy:
        assert copy.parent == private_scratch and copy.name.startswith("quadratus-review-")
        assert stat.S_IMODE(copy.stat().st_mode) == 0o700
        assert (copy / "app.py").read_text() == "print('before')\n"
    assert list(private_scratch.iterdir()) == []
    with pytest.raises(RuntimeError, match="mid-review"), project.snapshot() as copy:
        raise RuntimeError("mid-review")
    assert list(private_scratch.iterdir()) == [], "removed on an error as well"
    with runtime._review_directory(project, {"a/b.py": b"captured\n"}) as copy:
        assert copy.parent == private_scratch
        assert (copy / "a" / "b.py").read_bytes() == b"captured\n"
    assert list(private_scratch.iterdir()) == []


def test_the_provider_scratch_folder_and_prompt_files_are_private(tmp_path, private_scratch, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    provider = GrokCLIProvider(model="")
    assert Path(provider.workdir).parent == private_scratch
    argv = provider._build_argv("the prompt", "role")
    prompt_file = Path(argv[argv.index("--prompt-file") + 1])
    assert prompt_file.parent == private_scratch
    assert stat.S_IMODE(prompt_file.stat().st_mode) == 0o600
    assert prompt_file.read_text() == "the prompt"
    provider.cleanup()
    assert list(private_scratch.iterdir()) == []


def test_a_review_copy_still_gets_its_own_temp_folder_inside_the_copy(tmp_path, private_scratch, monkeypatch):
    """The copy moved; the rule that TMPDIR points inside it did not."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    seen = {}

    def launch(argv, **kwargs):
        seen.update(cwd=kwargs["cwd"], tmpdir=kwargs["env"]["TMPDIR"])
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "NO FINDINGS"}}), "")

    monkeypatch.setattr(cli_providers, "_launch", launch)
    project = Project(_project(tmp_path))
    with project.snapshot() as copy:
        view = CodexCLIProvider("gpt-5.6-sol").in_directory(copy, allow_writes=False, disposable=True)
        assert view.generate("review") == "NO FINDINGS"
        assert seen["cwd"] == str(copy)
        assert Path(seen["tmpdir"]) == copy / ".quadratus-tmp" and Path(seen["tmpdir"]).is_dir()
    assert list(private_scratch.iterdir()) == []


def test_the_bridge_socket_moves_to_the_scratch_root_only_when_its_path_fits(tmp_path, monkeypatch):
    import shutil
    import tempfile
    short = Path(tempfile.mkdtemp(prefix="qs-", dir="/tmp"))    # a short path, whatever pytest's is
    try:
        monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(short))
        with WorkerBridge(lambda arguments: ("ok", False)) as bridge:
            folder = Path(bridge.socket_path).parent
            assert folder.parent == short and stat.S_IMODE(folder.stat().st_mode) == 0o700
            assert len(os.fsencode(bridge.socket_path)) <= 100
            results = []
            _call_bridge(bridge, {}, results)
            assert results == [{"text": "ok", "is_error": False}]
        assert not folder.exists()
    finally:
        shutil.rmtree(short, ignore_errors=True)
    # A root too deep for a socket address keeps the short base it always had.
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True, mode=0o700)
    deep.chmod(0o700)
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(deep))
    with WorkerBridge(lambda arguments: ("ok", False)) as bridge:
        assert Path(bridge.socket_path).parent.parent == Path("/tmp")
    # And so does a scratch root that cannot be used at all.
    deep.chmod(0o755)
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(deep))
    with WorkerBridge(lambda arguments: ("ok", False)) as bridge:
        assert Path(bridge.socket_path).parent.parent == Path("/tmp")


def test_summary_calls_run_in_an_empty_folder_under_the_scratch_root(tmp_path, private_scratch, monkeypatch):
    from quadratus.delegation import invocation
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    seen = []

    def launch(argv, **kwargs):
        seen.append(Path(kwargs["cwd"]))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"type": "result", "result": "SUMMARY: ok"}), "")

    monkeypatch.setattr(cli_providers, "_launch", launch)
    fleet = Fleet(Settings(backend="cli"))
    with invocation("t1", "closeout"):
        assert fleet.invoke(OPUS, "evidence") == "SUMMARY: ok"
    assert seen[0].parent == private_scratch and seen[0].name.startswith("quadratus-closeout-")
    assert not seen[0].exists()


# -- item 8: the argv limit is counted in bytes, under Linux's cap -----------------

def _restricted_grok(monkeypatch):
    from quadratus.registry import resolve
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    spec = resolve("grok:worker")
    return GrokCLIProvider(model="").for_seat("", effort=spec.effort, restricted=spec.restricted)


def test_the_restricted_prompt_limit_counts_bytes_not_characters(monkeypatch):
    assert MAX_ARGV_PROMPT < 131_072, "one argument on Linux"
    provider = _restricted_grok(monkeypatch)
    # 50,000 characters, 150,000 bytes: under the old character count, over the cap.
    with pytest.raises(ProviderError) as caught:
        provider._build_argv("€" * 50_000, "role")
    assert "150,000 bytes" in str(caught.value) and "unrestricted seat" in str(caught.value)
    with pytest.raises(ProviderError):
        provider._build_argv("x" * (MAX_ARGV_PROMPT + 1), "role")
    fits = "x" * MAX_ARGV_PROMPT
    argv = provider._build_argv(fits, "role")
    assert argv[argv.index("-p") + 1] == fits


@POSIX
def test_a_prompt_at_the_limit_really_is_one_argument_this_system_accepts(tmp_path):
    done = _launch([sys.executable, "-c", "import sys; print(len(sys.argv[1]))", "x" * MAX_ARGV_PROMPT],
                   timeout=30, cwd=str(tmp_path))
    assert done.returncode == 0 and done.stdout.strip() == str(MAX_ARGV_PROMPT)


def test_an_argument_list_exec_refuses_is_named_and_final(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    calls = []

    def launch(argv, **kwargs):
        calls.append(1)
        raise OSError(errno.E2BIG, "Argument list too long")

    monkeypatch.setattr(cli_providers, "_launch", launch)
    for writes in (False, True):
        provider = ClaudeCLIProvider("opus", workdir=str(tmp_path), allow_writes=writes, max_retries=3,
                                     retry_base_delay=0.0)
        with pytest.raises(ProviderError, match="argument list is too long") as caught:
            provider.generate("p", system="s" * 10)
        assert not isinstance(caught.value, PartialWorkSuspected), "nothing ran, so nothing partial"
    assert len(calls) == 2


# -- item 9: bounded reads -----------------------------------------------------

@POSIX
def test_output_past_the_cap_stops_the_call_and_keeps_both_ends(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_CLI_OUTPUT_LIMIT_MB", "0.05")          # about 52 KiB
    script = ("import sys, time\n"
              "sys.stdout.write('HEAD' + 'x' * 400_000 + '\\n')\nsys.stdout.flush()\ntime.sleep(120)\n")
    started = time.monotonic()
    with pytest.raises(CLIOutputTooLarge) as caught:
        _launch([sys.executable, "-c", script], timeout=100, cwd=str(tmp_path))
    assert time.monotonic() - started < 30, "stopped at the cap, not at the timeout"
    error = caught.value
    assert error.stream == "stdout" and error.limit == int(0.05 * 1024 * 1024)
    assert error.head.startswith("HEADxxx") and error.tail.endswith("xxx")
    assert len(error.output.encode()) <= error.limit, "what was kept is bounded by the cap"
    assert "QUADRATUS_CLI_OUTPUT_LIMIT_MB" in str(error), "the way out is named"
    assert error.interrupted is True and not isinstance(error, ProviderError)


@POSIX
def test_a_flood_on_stderr_is_capped_too_and_a_bad_override_keeps_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_CLI_OUTPUT_LIMIT_MB", "0.05")
    script = "import sys, time\nsys.stderr.write('e' * 400_000)\nsys.stderr.flush()\ntime.sleep(120)\n"
    with pytest.raises(CLIOutputTooLarge) as caught:
        _launch([sys.executable, "-c", script], timeout=100, cwd=str(tmp_path))
    assert caught.value.stream == "stderr"
    for bad in ("zero", "-1", "nan", "0"):
        monkeypatch.setenv("QUADRATUS_CLI_OUTPUT_LIMIT_MB", bad)
        assert cli_providers._output_limit() == cli_providers.MAX_CLI_OUTPUT_BYTES


def test_an_over_long_output_is_a_stop_not_a_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    calls = []

    def launch(argv, **kwargs):
        calls.append(1)
        raise CLIOutputTooLarge("stdout", 10, "x" * 50, output="x" * 50, stderr="tail of stderr")

    monkeypatch.setattr(cli_providers, "_launch", launch)
    reader = ClaudeCLIProvider("opus", workdir=str(tmp_path), max_retries=3, retry_base_delay=0.0)
    with pytest.raises(ProviderError, match="wrote more than 10 bytes"):
        reader.generate("review")
    assert len(calls) == 1 and reader.last_stderr == "tail of stderr"
    writer = ClaudeCLIProvider("opus", workdir=str(tmp_path), allow_writes=True, max_retries=3,
                               retry_base_delay=0.0)
    with pytest.raises(PartialWorkSuspected) as caught:
        writer.generate("edit")
    assert len(calls) == 2 and caught.value.interrupted is True and caught.value.retryable is False
    assert caught.value.cause.output == "", "the bulk is dropped once it has been observed"


_PARENT = "01a0dab8-8947-72f1-9283-8b4b15408d54"


def _journal(tmp_path, first_line):
    folder = tmp_path / "sessions" / "2026" / "11" / "10"
    folder.mkdir(parents=True)
    path = folder / f"rollout-2026-11-10T09-00-00-{_PARENT}.jsonl"
    path.write_text(first_line + "\n")
    return path


def test_a_journal_first_line_is_read_with_a_byte_bound(tmp_path):
    record = {"type": "session_meta", "payload": {"id": _PARENT, "cwd": str(tmp_path),
                                                  "timestamp": datetime.now(timezone.utc).isoformat()}}
    path = _journal(tmp_path, json.dumps(record))
    assert native_sessions.first_record(path)["payload"]["id"] == _PARENT
    with pytest.raises(ValueError, match="longer than the read bound"):
        native_sessions.first_record(path, limit=20)
    # A first line that never ends is not read whole, and is nobody's journal.
    endless = dict(record, padding="p" * (native_sessions.FIRST_RECORD_BYTES + 10))
    path.write_text(json.dumps(endless) + "\n")
    root = tmp_path / "sessions"
    assert native_sessions.codex_children(root, _PARENT, str(tmp_path)) == []
    assert cli_providers._codex_journal(root, _PARENT, str(tmp_path), datetime.now(timezone.utc)) is None


def test_later_journal_lines_are_bounded_too(tmp_path):
    """A journal also records tool output, as long as a model makes it. A
    line past the bound is passed over, never held, and the short records
    on either side of it are still read."""
    edge = tmp_path / "edge.jsonl"
    edge.write_bytes(b"a" * 9 + b"\n" + b"b" * 10 + b"\n" + b"c" * 25 + b"\ntail")
    assert list(native_sessions.bounded_lines(edge, limit=10)) == ["a" * 9 + "\n", "tail"]

    def count(total):
        return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total, "output_tokens": 1}}}})

    flood = json.dumps({"type": "response_item", "payload": {
        "type": "function_call_output", "output": "z" * (native_sessions.FIRST_RECORD_BYTES + 10)}})
    path = _journal(tmp_path, "\n".join([count(5), flood, count(9)]))
    assert [len(line) < 200 for line in native_sessions.bounded_lines(path)] == [True, True]
    assert [event["payload"]["info"]["total_token_usage"]["input_tokens"]
            for event in native_sessions._events(path)] == [5, 9]
    usage = cli_providers._codex_rollout_usage(tmp_path / "sessions", _PARENT)
    assert usage["input_tokens"] == 9 and usage["token_count_events"] == 2


# -- item 10: the debug log names the binary, not the prompt ----------------------

def test_the_debug_log_does_not_copy_the_system_prompt(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("shutil.which", lambda _: "/opt/vendor/bin/claude")
    monkeypatch.setattr(cli_providers, "_launch", lambda argv, **k: subprocess.CompletedProcess(
        argv, 0, json.dumps({"type": "result", "result": "ok"}), ""))
    provider = ClaudeCLIProvider("opus", workdir=str(tmp_path))
    with caplog.at_level(logging.DEBUG, logger="quadratus.cli_providers"):
        provider.generate("TASK-TEXT-7", system="ROLE-SECRET-42")
    line = next(r.getMessage() for r in caplog.records if "invoking" in r.getMessage())
    assert "ROLE-SECRET-42" not in caplog.text and "TASK-TEXT-7" not in caplog.text
    assert "claude with" in line and "arguments" in line and "/opt/vendor" not in line


# -- item 14: an envelope that is not an object is a named failure -------------------

@pytest.mark.parametrize("stdout", ["[]", "null", "", "   ", "42", '"text"', "not json at all"])
def test_a_claude_reply_that_is_not_an_envelope_is_a_provider_error(stdout):
    with pytest.raises(ProviderError) as caught:
        _extract_claude_result(stdout)
    assert caught.value.no_envelope is True
    assert "JSON" in str(caught.value)


def test_a_non_object_envelope_on_exit_zero_is_final_and_carries_diagnostics(tmp_path, monkeypatch):
    script, runs = _fake_cli(tmp_path, "claude", "print('[]')\n")
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    provider = ClaudeCLIProvider("opus", workdir=str(tmp_path / "w"), max_retries=3, retry_base_delay=0.0)
    with pytest.raises(ProviderError, match=r"unexpected JSON \(list\)") as caught:
        provider.generate("q")
    assert "object has no attribute" not in str(caught.value)
    assert hasattr(caught.value, "diagnostics")
    assert _runs(runs) == 1


def test_a_failed_exit_with_nothing_parseable_still_reports_the_exit_and_stderr(tmp_path, monkeypatch):
    script, _ = _fake_cli(tmp_path, "claude", "sys.stderr.write('fatal: bad flag\\n')\nsys.exit(3)\n")
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    provider = ClaudeCLIProvider("opus", workdir=str(tmp_path / "w"), max_retries=1)
    with pytest.raises(ProviderError, match="CLI exited 3: fatal: bad flag"):
        provider.generate("q")


def test_a_result_that_is_not_text_is_an_empty_answer_not_a_wrong_type():
    assert _extract_claude_result(json.dumps({"type": "result", "result": ["a", "b"]})) == ""
    assert _extract_claude_result(json.dumps({"type": "result", "result": "text"})) == "text"


# -- item 15: the worker bridge does not outlive its lead ------------------------------

def _call_bridge(bridge, arguments, results):
    request = json.dumps({"token": bridge._token, "arguments": arguments}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(bridge.socket_path)
        conn.sendall(request)
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    results.append(json.loads(b"".join(chunks).decode()))


def test_the_bridge_waits_for_a_call_in_flight_and_refuses_one_that_was_queued():
    release, entered, order, results = threading.Event(), threading.Event(), [], []

    def handler(arguments):
        order.append(("start", arguments["n"]))
        entered.set()
        assert release.wait(20)
        order.append(("end", arguments["n"]))
        return f"answer {arguments['n']}", False

    bridge = WorkerBridge(handler).__enter__()
    clients = [threading.Thread(target=_call_bridge, args=(bridge, {"n": 1}, results))]
    clients[0].start()
    assert entered.wait(10)
    clients.append(threading.Thread(target=_call_bridge, args=(bridge, {"n": 2}, results)))
    clients[1].start()
    time.sleep(0.3)                       # the second call is accepted and waits its turn
    closing = threading.Thread(target=bridge.__exit__, args=(None, None, None))
    closing.start()
    closing.join(0.5)
    assert closing.is_alive(), "exit waits for the worker call already running"
    order.append("session would move on here")
    release.set()
    closing.join(20)
    for client in clients:
        client.join(20)
    assert not closing.is_alive() and bridge.abandoned == 0
    assert order == [("start", 1), "session would move on here", ("end", 1)], \
        "the queued call never reached the handler"
    assert bridge.calls == 1
    by_text = sorted(result["text"] for result in results)
    assert by_text[0] == "The worker channel is closed: the call that opened it has ended."
    assert by_text[1] == "answer 1"
    assert not os.path.exists(bridge.socket_path)


def test_the_bridge_says_so_when_a_call_outlasts_its_allowance(caplog):
    release, entered = threading.Event(), threading.Event()

    def handler(arguments):
        entered.set()
        release.wait(20)
        return "late", False

    results = []
    bridge = WorkerBridge(handler, drain_seconds=0.2).__enter__()
    client = threading.Thread(target=_call_bridge, args=(bridge, {}, results))
    client.start()
    assert entered.wait(10)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="quadratus.worker_bridge"):
        bridge.__exit__(None, None, None)
    assert time.monotonic() - started < 10
    assert bridge.abandoned == 1 and "still running" in caplog.text
    release.set()
    client.join(20)


def test_an_operator_stopping_the_run_is_not_made_to_wait_out_a_worker():
    release, entered = threading.Event(), threading.Event()

    def handler(arguments):
        entered.set()
        release.wait(30)
        return "late", False

    results = []
    bridge = WorkerBridge(handler).__enter__()          # the default, long allowance
    client = threading.Thread(target=_call_bridge, args=(bridge, {}, results))
    client.start()
    assert entered.wait(10)
    started = time.monotonic()
    bridge.__exit__(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert time.monotonic() - started < 10 and bridge.abandoned == 1
    release.set()
    client.join(20)


def test_a_handler_is_never_listed_before_it_has_started(monkeypatch):
    """``drain`` joins every listed handler, and joining a thread that has
    not started raises. A handler is therefore started while the list's lock
    is held, so no reader of the list can see one that is not running yet."""
    from quadratus import worker_bridge
    started_under_lock = []

    class Watched(threading.Thread):
        def start(self):
            if self._target is not None and getattr(self._target, "__name__", "") == "_answer":
                started_under_lock.append(self._target.__self__._handlers_lock.locked())
            super().start()

    class Shim:                      # only this module's view of threading changes
        Thread = Watched

        def __getattr__(self, name):
            return getattr(threading, name)

    monkeypatch.setattr(worker_bridge, "threading", Shim())
    results = []
    with WorkerBridge(lambda arguments: ("ok", False)) as bridge:
        _call_bridge(bridge, {}, results)
        _call_bridge(bridge, {}, results)
        assert bridge.drain(5) == 0
    assert started_under_lock == [True, True]
    assert [result["text"] for result in results] == ["ok", "ok"]


def test_a_call_accepted_during_a_wait_between_lead_calls_is_still_waited_for_on_exit():
    """``drain`` may be called while the channel is open. It took a copy of
    the handler list, waited, and then replaced the list with that copy's
    survivors, so a call accepted during the wait was no longer listed and
    exit returned while its worker ran."""
    entered = {1: threading.Event(), 2: threading.Event()}
    release = {1: threading.Event(), 2: threading.Event()}
    results = []

    def handler(arguments):
        entered[arguments["n"]].set()
        assert release[arguments["n"]].wait(20)
        return f"answer {arguments['n']}", False

    bridge = WorkerBridge(handler).__enter__()
    clients = [threading.Thread(target=_call_bridge, args=(bridge, {"n": 1}, results))]
    clients[0].start()
    assert entered[1].wait(10)
    still_running = []
    waiting = threading.Thread(target=lambda: still_running.append(bridge.drain(1.0)))
    waiting.start()
    time.sleep(0.2)                       # the wait has taken its copy of the list
    clients.append(threading.Thread(target=_call_bridge, args=(bridge, {"n": 2}, results)))
    clients[1].start()
    waiting.join(20)
    assert still_running == [2], "the call accepted during the wait is counted"
    release[1].set()
    assert entered[2].wait(10)            # the second call is now inside the handler
    closing = threading.Thread(target=bridge.__exit__, args=(None, None, None))
    closing.start()
    closing.join(0.5)
    assert closing.is_alive(), "exit waits for the call it was never shown"
    release[2].set()
    closing.join(20)
    for client in clients:
        client.join(20)
    assert not closing.is_alive() and bridge.abandoned == 0 and bridge.calls == 2
    assert sorted(result["text"] for result in results) == ["answer 1", "answer 2"]


def test_the_socket_folder_is_removed_when_the_wait_on_exit_is_interrupted(monkeypatch):
    bridge = WorkerBridge(lambda arguments: ("ok", False)).__enter__()
    folder = os.path.dirname(bridge.socket_path)

    def interrupted(seconds=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge, "drain", interrupted)
    with pytest.raises(KeyboardInterrupt):
        bridge.__exit__(None, None, None)
    assert not os.path.exists(folder)


# -- item 16: one allow flag, not two ---------------------------------------------------

def test_granted_checks_and_the_worker_tool_share_one_allow_flag(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    tool = dict(name="quadratus", command="/py", args=["/srv.py"], env={"K": "v"}, timeout=900)
    view = ClaudeCLIProvider(model="opus").in_directory("/tmp/p", allow_writes=True)
    view.granted_commands = ("python -m pytest -q",)
    view.worker_tool = tool
    argv = view._build_argv("p", "s")
    assert argv.count("--allowedTools") == 1
    at = argv.index("--allowedTools")
    assert argv[at + 1:at + 4] == ["Bash(python -m pytest -q)", "Bash(python -m pytest -q:*)",
                                   "mcp__quadratus__commission_worker"]
    assert argv[at + 4].startswith("-"), "the rules end where the next option begins"
    assert "--strict-mcp-config" in argv and json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
    # Each alone is unchanged.
    only_tool = ClaudeCLIProvider(model="opus")
    only_tool.worker_tool = tool
    alone = only_tool._build_argv("p", "s")
    assert alone.count("--allowedTools") == 1
    assert alone[alone.index("--allowedTools") + 1] == "mcp__quadratus__commission_worker"
    assert cli_providers._fold_allowed(["x"], "--allowedTools", ["A", "B"]) == ["x", "--allowedTools", "A", "B"]
    assert cli_providers._fold_allowed(["--allowedTools", "A", "--next"], "--allowedTools", ["A", "B"]) == [
        "--allowedTools", "A", "B", "--next"]


# -- item 17: one bad record does not cost the rest of the accounting ----------------------

def test_a_parent_journal_with_no_usable_clock_links_no_children_and_does_not_raise(tmp_path):
    root = tmp_path / "sessions"
    for meta in ({"id": _PARENT, "cwd": str(tmp_path)},                                   # no timestamp
                 {"id": _PARENT, "cwd": str(tmp_path), "timestamp": "not a time"},
                 {"id": _PARENT, "cwd": str(tmp_path), "timestamp": "2026-11-10T09:00:00"},  # no zone
                 {"id": _PARENT, "timestamp": "2026-11-10T09:00:00Z"}):                    # no folder
        path = _journal(tmp_path, json.dumps({"type": "session_meta", "payload": meta}))
        assert native_sessions.codex_children(root, _PARENT, str(tmp_path),
                                              ended=datetime.now(timezone.utc)) == []
        path.unlink()
        path.parent.rmdir()
        path.parent.parent.rmdir()
        path.parent.parent.parent.rmdir()
        root.rmdir()
    _journal(tmp_path, "[1, 2, 3]")                                                       # not an object
    assert native_sessions.codex_children(root, _PARENT, str(tmp_path)) == []


def test_a_failed_journal_read_does_not_skip_the_control_failure_note(tmp_path, monkeypatch):
    """A child ran although codex was told not to spawn one. The journal step
    then fails; the note that says CONTROL FAILURE used to be skipped with it."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")

    def broken(*args, **kwargs):
        raise KeyError("timestamp")

    monkeypatch.setattr(cli_providers, "_codex_journal_tool_failures", broken)
    stream = "\n".join(json.dumps(event) for event in [
        {"type": "thread.started", "thread_id": _PARENT},
        {"type": "item.completed", "item": {"type": "collab_tool_call", "id": "c1", "tool": "spawn_agent",
                                            "receiver_thread_ids": ["child-1"]}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}])
    provider = CodexCLIProvider("gpt-5.6-sol", workdir=str(tmp_path))
    provider._call_started = datetime.now(timezone.utc)
    provider._observe_output(stream)
    assert provider.last_usage == {"input_tokens": 10, "output_tokens": 2}
    assert provider.last_session_id == _PARENT
    [child] = provider.native_children
    assert child.detail.startswith("CONTROL FAILURE: native child ran")


def test_a_model_usage_that_is_not_a_mapping_does_not_end_the_accounting(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    provider = ClaudeCLIProvider("opus", workdir=str(tmp_path))
    provider.last_session_id = None
    # The bad record comes first: iterating a number raised, and the one
    # ``except`` around the whole function then dropped everything after
    # it, here the session id on the next line.
    provider._observe_output("\n".join([
        json.dumps({"type": "system", "modelUsage": 7}),
        json.dumps({"type": "result", "result": "x", "session_id": "s-9"})]))
    assert provider.last_session_id == "s-9"
    provider._observe_output(json.dumps({"type": "result", "result": "x", "session_id": "s-10",
                                         "modelUsage": {"claude-opus-5-20260101": {}, "other": {}}}))
    assert provider.last_session_id == "s-10" and provider.resolved_model == "claude-opus-5-20260101"


# -- item 18: the isolated runner's cleanup cannot replace the outcome ---------------------

def _docker(monkeypatch, tmp_path, *, wait_stdout="0\n", rm=None):
    work, runtime_tree = tmp_path / "work", tmp_path / "runtime"
    work.mkdir()
    runtime_tree.mkdir()
    seen = []

    def run(argv, **kwargs):
        seen.append(argv[1])
        if argv[1] == "rm":
            if isinstance(rm, BaseException):
                raise rm
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, wait_stdout if argv[1] == "wait" else "", "")

    monkeypatch.setattr(isolated_run.subprocess, "run", run)
    return dict(image="sha256:" + "0" * 64, work=work, runtime=runtime_tree, command=["true"]), seen


def test_unreadable_wait_output_is_a_failed_run_not_a_crash(tmp_path, monkeypatch):
    arguments, seen = _docker(monkeypatch, tmp_path, wait_stdout="")
    result = isolated_run.run_isolated(**arguments)
    assert (result.outcome, result.exit_code) == ("failed", None)
    assert seen[-1] == "rm", "and the container was still removed"
    again = tmp_path / "again"
    again.mkdir()
    arguments, _ = _docker(monkeypatch, again, wait_stdout="0\n")
    assert isolated_run.run_isolated(**arguments).outcome == "success"
    assert isolated_run._exit_code("17\n") == 17 and isolated_run._exit_code("Error: no such") is None


def test_a_cleanup_that_times_out_says_the_stop_is_unverified(tmp_path, monkeypatch):
    stuck = subprocess.TimeoutExpired(["docker", "rm"], 15)
    arguments, _ = _docker(monkeypatch, tmp_path, rm=stuck)
    with pytest.raises(RuntimeError, match="Container cleanup failed; workload stop is unverified") as caught:
        isolated_run.run_isolated(**arguments)
    assert caught.value.__cause__ is stuck
