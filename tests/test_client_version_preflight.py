"""The installed vendor client is a recorded fact and a run precondition.

Codex 0.154 discarded a capacity 429's body and Retry-After inside its API
bridge and ended two runs on a bare status after its own retries; 0.163.0,
which waits inside the same sampling session, is the first version this
engine qualified (live, 2026-10-21). The engine adds no retry of its own:
it records which client answered, how a failed turn ended, and refuses a
run before any call when the installed client parses below that minimum.
"""
import json
import os
import stat

import pytest

from quadratus.cli_providers import (
    CLI_SPECS,
    ClaudeCLIProvider,
    CLIProvider,
    CodexCLIProvider,
    StaleClient,
    _extract_codex_diagnostics,
    client_version,
    client_version_at_least,
    parse_client_version,
)
from quadratus.config import Settings
from quadratus.delegation import safe_diagnostics
from quadratus.project_run import run_project
from quadratus.runtime import Fleet, _looks_exhausted
from tests.test_cli_providers import CODEX_STREAM
from tests.test_runtime import FakeCLIProvider

FAILED_STREAM = "\n".join([
    '{"type":"thread.started","thread_id":"01a097a6-82bf-7ca2-ab48-a6afd5800585"}',
    '{"type":"turn.started"}',
    '{"type":"turn.failed","error":{"message":"exceeded retry limit, last status: 429 Too Many Requests"}}',
])


# -- version text ---------------------------------------------------------


def test_versions_parse_and_prereleases_order_below_their_release():
    assert parse_client_version("0.154.0")[:3] == (0, 154, 0)
    assert parse_client_version("0.162.0-alpha.17.2") < parse_client_version("0.162.0")
    assert parse_client_version("0.162.0") < parse_client_version("0.163.0")
    assert parse_client_version("2.1.47 (Claude Code)")[:3] == (2, 1, 47)
    assert parse_client_version("") is None and parse_client_version("nightly") is None


@pytest.mark.parametrize("found, verdict", [
    ("0.163.0", True),
    ("0.164.0-alpha.1", True),
    ("0.162.0", False),
    ("0.162.0-alpha.17.2", False),
    ("0.163.0-alpha.1", False),
    ("0.154.0", False),
    (None, None),
    ("nightly", None),
])
def test_at_least_is_true_false_or_unknown(found, verdict):
    assert client_version_at_least(found, "0.163.0") is verdict


def test_an_unparseable_minimum_is_unknown_never_a_pass():
    assert client_version_at_least("0.163.0", "latest") is None


# -- reading the binary ---------------------------------------------------


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_only_the_version_form_is_kept_from_the_first_stdout_line(tmp_path):
    script = _script(tmp_path / "fake-codex", "echo 'codex-cli 0.154.0'\necho 'second line is not the version'\n")
    assert client_version(script, ["--version"]) == "0.154.0"
    script = _script(tmp_path / "fake-claude", "echo '2.1.47 (Claude Code)'\n")
    assert client_version(script, ["--version"]) == "2.1.47"
    script = _script(tmp_path / "fake-alpha", "echo 'codex-cli 0.162.0-alpha.17.2'\n")
    assert client_version(script, ["--version"]) == "0.162.0-alpha.17.2"


def test_a_replaced_executable_at_the_same_path_is_read_again(tmp_path):
    """Root's review of 955d3ad: a cache keyed on the path alone kept
    0.154.0 after the operator installed 0.163.0 at the same path, so a
    long-lived process went on refusing after its own prescribed remedy."""
    script = _script(tmp_path / "fake-codex", "echo 'codex-cli 0.154.0'\n")
    assert client_version(script, ["--version"]) == "0.154.0"
    assert client_version(script, ["--version"]) == "0.154.0", "read once per identity"
    _script(tmp_path / "fake-codex", "# the operator's upgrade, installed at the same path\necho 'codex-cli 0.163.0'\n")
    assert client_version(script, ["--version"]) == "0.163.0"


@pytest.mark.parametrize("body", [
    "echo 'codex-cli 0.154.0'\nexit 1\n",                  # a non-zero exit is not a version
    "echo 'codex-cli 0.154.0' 1>&2\n",                      # stderr is never read
    "echo 'error: /usr/lib/node_modules/codex token abc'\n",  # no version form on the line
    "echo ''\n",
    "exit 0\n",
])
def test_anything_but_a_recognised_version_is_unknown(tmp_path, body):
    assert client_version(_script(tmp_path / "fake", body), ["--version"]) is None


def test_a_binary_that_cannot_run_or_be_found_reads_as_no_version(tmp_path):
    assert client_version(str(tmp_path / "absent"), ["--version"]) is None
    plain = tmp_path / "not-executable"
    plain.write_text("echo 'codex-cli 0.154.0'\n")
    assert client_version(str(plain), ["--version"]) is None


def test_the_provider_records_the_client_and_the_qualified_minimum(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("quadratus.cli_providers.client_version", lambda path, args: "0.154.0")
    provider = CodexCLIProvider(model="gpt-6-astra")
    assert provider.client_version == "0.154.0"
    assert provider.minimum_client == "0.163.0" == CLI_SPECS["openai"].minimum_client
    assert provider.available(), "recorded, never refused by the provider"


def test_every_vendor_carries_its_qualified_minimum(monkeypatch):
    """Root's qualification targets of 2026-10-21: codex 0.163.0, claude
    2.1.304 (npm stable, from 2.1.296) and grok 1.0.62 (Homebrew/x.ai
    stable, from 1.0.30)."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("quadratus.cli_providers.client_version", lambda path, args: "2.1.296")
    provider = ClaudeCLIProvider(model="opus")
    assert provider.client_version == "2.1.296" and provider.minimum_client == "2.1.304"
    assert {v: CLI_SPECS[v].minimum_client for v in ("claude", "openai", "grok")} == {
        "claude": "2.1.304", "openai": "0.163.0", "grok": "1.0.62"}


# -- diagnostics ----------------------------------------------------------


def test_a_failed_turn_records_its_terminal_reason_status_and_class_only():
    assert _extract_codex_diagnostics(FAILED_STREAM) == {"stop_reason": "turn.failed", "status": 429,
                                                         "failure_class": "rate_limited"}


def test_a_failed_turn_without_a_status_in_its_message_records_the_reason_alone():
    stream = '{"type":"turn.failed","error":{"message":"stream disconnected before completion"}}'
    assert _extract_codex_diagnostics(stream) == {"stop_reason": "turn.failed"}
    stream = '{"type":"turn.failed","error":{"message":"rejected","status":503}}'
    assert _extract_codex_diagnostics(stream) == {"stop_reason": "turn.failed", "status": 503,
                                                  "failure_class": "unavailable"}


def test_a_completed_turn_reads_as_before():
    assert _extract_codex_diagnostics(CODEX_STREAM)["stop_reason"] == "turn.completed"


def test_the_answering_client_is_on_every_observed_call(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("quadratus.cli_providers.client_version", lambda path, args: "0.163.0")
    provider = CodexCLIProvider(model="gpt-6-astra")
    provider._observe_output(FAILED_STREAM)
    assert provider.last_diagnostics == {"stop_reason": "turn.failed", "status": 429,
                                         "failure_class": "rate_limited", "client_version": "0.163.0"}
    provider._observe_output(CODEX_STREAM)
    assert provider.last_diagnostics["client_version"] == "0.163.0"
    assert provider.last_diagnostics["stop_reason"] == "turn.completed"


def test_the_ledger_keeps_the_client_and_status_and_drops_free_text():
    got = safe_diagnostics({"stop_reason": "turn.failed", "status": 429, "client_version": "0.163.0",
                            "failure": "exceeded retry limit, last status: 429 Too Many Requests",
                            "retry_after": 30})
    assert got == {"stop_reason": "turn.failed", "status": 429, "client_version": "0.163.0"}
    assert safe_diagnostics({"client_version": "0.162.0-alpha.17.2"})["client_version"] == "0.162.0-alpha.17.2"


@pytest.mark.parametrize("value", [
    "codex-cli 0.163.0", "0.1.0\n--prompt", "/usr/bin/codex 0.163.0", "0.163.0 token=abc",
    "0.163.0-" + "a" * 33, "x" * 121, 99, "", "nightly",
])
def test_the_ledger_takes_only_the_version_form(value):
    assert "client_version" not in safe_diagnostics({"client_version": value})
    assert "status" not in safe_diagnostics({"status": 99})


# -- the fleet and the preflight ------------------------------------------


def _fleet(monkeypatch, version, minimum="0.163.0", backend="cli"):
    made = {}

    def fake_build(name, settings, **kw):
        made[name] = made.get(name) or FakeCLIProvider(name)
        if name == "openai":
            made[name].client_version, made[name].minimum_client = version, minimum
        return made[name]

    monkeypatch.setattr("quadratus.runtime.build_provider", fake_build)
    return Fleet(Settings(backend=backend)), made


def test_a_stale_client_is_named_on_the_status_line_and_listed_for_the_preflight(monkeypatch):
    fleet, _ = _fleet(monkeypatch, "0.154.0")
    line = next(line for line in fleet.status_lines() if line.startswith("openai"))
    assert "client 0.154.0, qualified minimum 0.163.0 (STALE: refused before any call)" in line
    assert fleet.stale_clients() == ["openai: installed client 0.154.0 is below the "
                                     "qualified minimum 0.163.0; update the client before running"]


def test_a_qualified_client_passes_and_is_still_shown(monkeypatch):
    fleet, _ = _fleet(monkeypatch, "0.163.0")
    line = next(line for line in fleet.status_lines() if line.startswith("openai"))
    assert line.endswith("client 0.163.0, qualified minimum 0.163.0")
    assert fleet.stale_clients() == []


def test_an_unreadable_version_is_reported_as_unknown_and_not_refused(monkeypatch):
    fleet, _ = _fleet(monkeypatch, None)
    line = next(line for line in fleet.status_lines() if line.startswith("openai"))
    assert "client unknown, qualified minimum 0.163.0 (version unreadable; not refused)" in line
    assert fleet.stale_clients() == []


def test_vendors_without_a_minimum_and_unavailable_seats_are_not_listed(monkeypatch):
    fleet, made = _fleet(monkeypatch, "0.154.0", minimum=None)
    assert fleet.stale_clients() == []
    fleet, made = _fleet(monkeypatch, "0.154.0")
    assert fleet.stale_clients() and "openai" in made
    made["openai"]._client = None
    assert fleet.stale_clients() == [], "a seat nothing can route to is not the preflight's to refuse"


def test_the_api_backend_has_no_client_to_check(monkeypatch):
    fleet, _ = _fleet(monkeypatch, "0.154.0", backend="api")
    assert fleet.stale_clients() == []


def test_the_refusal_is_not_read_as_an_exhausted_window():
    exc = StaleClient("openai: installed client 0.154.0 is below the qualified minimum 0.163.0; "
                      "update the client before running")
    assert not _looks_exhausted(exc)


QUALIFIED = {"claude": "2.1.304", "codex": "0.163.0", "grok": "1.0.62"}


def _run_with_client(tmp_path, monkeypatch, version, vendor_binary="codex"):
    """Every vendor's fake binary reports its qualified version except
    ``vendor_binary``, which reports ``version``."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("quadratus.cli_providers.client_version",
                        lambda path, args: version if path.endswith("/" + vendor_binary) else QUALIFIED[path.rsplit("/", 1)[-1]])
    monkeypatch.setattr(CLIProvider, "available", lambda _: True)
    calls = []

    def call(self, prompt, system, history):
        calls.append(prompt)
        self.last_usage = {"input_tokens": 11, "output_tokens": 3}
        return "DONE"

    monkeypatch.setattr(CLIProvider, "_call", call)
    result = run_project("Nothing", tmp_path, Settings(backend="cli"), max_tasks=1)
    return result, calls, json.loads((result.run_dir / "result.json").read_text())


def test_a_stale_client_refuses_the_run_before_any_call(tmp_path, monkeypatch):
    result, calls, data = _run_with_client(tmp_path, monkeypatch, "0.154.0")
    assert calls == []
    assert data["completed"] is False
    assert data["error"].startswith("StaleClient: openai: installed client 0.154.0 is below "
                                    "the qualified minimum 0.163.0")
    assert (result.run_dir / "report.md").exists()


def test_a_qualified_client_runs(tmp_path, monkeypatch):
    _, calls, data = _run_with_client(tmp_path, monkeypatch, "0.163.0")
    assert calls, "the run reached the orchestrator"
    assert "StaleClient" not in (data["error"] or "")


@pytest.mark.parametrize("binary, installed, minimum", [("claude", "2.1.296", "2.1.304"), ("grok", "1.0.30", "1.0.62")])
def test_the_other_vendors_previous_clients_are_refused_the_same_way(tmp_path, monkeypatch, binary, installed, minimum):
    _, calls, data = _run_with_client(tmp_path, monkeypatch, installed, vendor_binary=binary)
    assert calls == []
    assert data["error"].startswith(f"StaleClient: {'openai' if binary == 'codex' else binary}: installed client "
                                    f"{installed} is below the qualified minimum {minimum}")


def test_no_environment_override_lifts_the_minimum(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_CLI_MIN_OPENAI", "0")
    _, calls, data = _run_with_client(tmp_path, monkeypatch, "0.154.0")
    assert calls == [] and data["error"].startswith("StaleClient:")
    assert "QUADRATUS_CLI_MIN" not in data["error"]
    assert os.environ.get("QUADRATUS_CLI_MIN_OPENAI") == "0"
