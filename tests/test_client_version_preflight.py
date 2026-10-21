"""The installed vendor client is a recorded fact and a run precondition.

Codex 0.154 discarded a capacity 429's body and Retry-After inside its API
bridge and ended two runs on a bare status after its own retries; 0.163 is
the first stable client that waits inside the same sampling session. The
engine adds no retry of its own: it records which client answered, how a
failed turn ended, and refuses a run before any call when the installed
client parses below the qualified minimum (2026-10-21).
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
    assert parse_client_version("codex-cli 0.154.0")[:3] == (0, 154, 0)
    assert parse_client_version("0.162.0-alpha.17.2") < parse_client_version("0.162.0")
    assert parse_client_version("0.162.0") < parse_client_version("0.163.0")
    assert parse_client_version("2.1.47 (Claude Code)")[:3] == (2, 1, 47)
    assert parse_client_version("") is None and parse_client_version("nightly") is None


@pytest.mark.parametrize("found, verdict", [
    ("codex-cli 0.163.0", True),
    ("codex-cli 0.164.0-alpha.1", True),
    ("codex-cli 0.162.0", False),
    ("codex-cli 0.162.0-alpha.17.2", False),
    ("codex-cli 0.163.0-alpha.1", False),
    ("codex-cli 0.154.0", False),
    (None, None),
    ("codex-cli nightly", None),
])
def test_at_least_is_true_false_or_unknown(found, verdict):
    assert client_version_at_least(found, "0.163.0") is verdict


def test_an_unparseable_minimum_is_unknown_never_a_pass():
    assert client_version_at_least("codex-cli 0.163.0", "latest") is None


# -- reading the binary ---------------------------------------------------


def test_the_version_line_is_read_once_per_binary_and_bounded(tmp_path):
    script = tmp_path / "fake-codex"
    script.write_text("#!/bin/sh\necho 'codex-cli 0.154.0'\necho 'second line is not the version'\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    assert client_version(str(script), ["--version"]) == "codex-cli 0.154.0"
    script.write_text("#!/bin/sh\necho 'codex-cli 9.9.9'\n")
    assert client_version(str(script), ["--version"]) == "codex-cli 0.154.0", "cached per path"


def test_a_binary_that_cannot_run_reads_as_no_version(tmp_path):
    assert client_version(str(tmp_path / "absent"), ["--version"]) is None


def test_the_provider_records_the_client_and_the_qualified_minimum(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("quadratus.cli_providers.client_version", lambda path, args: "codex-cli 0.154.0")
    provider = CodexCLIProvider(model="gpt-6-astra")
    assert provider.client_version == "codex-cli 0.154.0"
    assert provider.minimum_client == "0.163.0" == CLI_SPECS["openai"].minimum_client
    assert provider.available(), "recorded, never refused by the provider"


def test_vendors_without_a_qualified_minimum_record_none(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("quadratus.cli_providers.client_version", lambda path, args: "2.1.47 (Claude Code)")
    provider = ClaudeCLIProvider(model="opus")
    assert provider.client_version == "2.1.47 (Claude Code)" and provider.minimum_client is None


# -- diagnostics ----------------------------------------------------------


def test_a_failed_turn_records_its_terminal_reason_and_status_only():
    assert _extract_codex_diagnostics(FAILED_STREAM) == {"stop_reason": "turn.failed", "status": 429}


def test_a_failed_turn_without_a_status_in_its_message_records_the_reason_alone():
    stream = '{"type":"turn.failed","error":{"message":"stream disconnected before completion"}}'
    assert _extract_codex_diagnostics(stream) == {"stop_reason": "turn.failed"}
    stream = '{"type":"turn.failed","error":{"message":"rejected","status":503}}'
    assert _extract_codex_diagnostics(stream) == {"stop_reason": "turn.failed", "status": 503}


def test_a_completed_turn_reads_as_before():
    assert _extract_codex_diagnostics(CODEX_STREAM)["stop_reason"] == "turn.completed"


def test_the_answering_client_is_on_every_observed_call(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("quadratus.cli_providers.client_version", lambda path, args: "codex-cli 0.163.0")
    provider = CodexCLIProvider(model="gpt-6-astra")
    provider._observe_output(FAILED_STREAM)
    assert provider.last_diagnostics == {"stop_reason": "turn.failed", "status": 429,
                                         "client_version": "codex-cli 0.163.0"}
    provider._observe_output(CODEX_STREAM)
    assert provider.last_diagnostics["client_version"] == "codex-cli 0.163.0"
    assert provider.last_diagnostics["stop_reason"] == "turn.completed"


def test_the_ledger_keeps_the_client_and_status_and_drops_free_text():
    got = safe_diagnostics({"stop_reason": "turn.failed", "status": 429, "client_version": "codex-cli 0.163.0",
                            "failure": "exceeded retry limit, last status: 429 Too Many Requests",
                            "retry_after": 30})
    assert got == {"stop_reason": "turn.failed", "status": 429, "client_version": "codex-cli 0.163.0"}
    bad = safe_diagnostics({"status": 99, "client_version": "x" * 121})
    assert "status" not in bad and "client_version" not in bad
    assert "client_version" not in safe_diagnostics({"client_version": "0.1.0\n--prompt"})


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
    fleet, _ = _fleet(monkeypatch, "codex-cli 0.154.0")
    line = next(line for line in fleet.status_lines() if line.startswith("openai"))
    assert "client codex-cli 0.154.0, qualified minimum 0.163.0 (STALE: refused before any call)" in line
    assert fleet.stale_clients() == ["openai: installed client codex-cli 0.154.0 is below the "
                                     "qualified minimum 0.163.0; update the client before running"]


def test_a_qualified_client_passes_and_is_still_shown(monkeypatch):
    fleet, _ = _fleet(monkeypatch, "codex-cli 0.163.0")
    line = next(line for line in fleet.status_lines() if line.startswith("openai"))
    assert line.endswith("client codex-cli 0.163.0, qualified minimum 0.163.0")
    assert fleet.stale_clients() == []


def test_an_unreadable_version_is_reported_as_unknown_and_not_refused(monkeypatch):
    fleet, _ = _fleet(monkeypatch, None)
    line = next(line for line in fleet.status_lines() if line.startswith("openai"))
    assert "client unknown, qualified minimum 0.163.0 (version unreadable; not refused)" in line
    assert fleet.stale_clients() == []


def test_vendors_without_a_minimum_and_unavailable_seats_are_not_listed(monkeypatch):
    fleet, made = _fleet(monkeypatch, "codex-cli 0.154.0", minimum=None)
    assert fleet.stale_clients() == []
    fleet, made = _fleet(monkeypatch, "codex-cli 0.154.0")
    assert fleet.stale_clients() and "openai" in made
    made["openai"]._client = None
    assert fleet.stale_clients() == [], "a seat nothing can route to is not the preflight's to refuse"


def test_the_api_backend_has_no_client_to_check(monkeypatch):
    fleet, _ = _fleet(monkeypatch, "codex-cli 0.154.0", backend="api")
    assert fleet.stale_clients() == []


def test_the_refusal_is_not_read_as_an_exhausted_window():
    exc = StaleClient("openai: installed client codex-cli 0.154.0 is below the qualified minimum 0.163.0; "
                      "update the client before running")
    assert not _looks_exhausted(exc)


def _run_with_client(tmp_path, monkeypatch, version):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("quadratus.cli_providers.client_version", lambda path, args: version)
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
    result, calls, data = _run_with_client(tmp_path, monkeypatch, "codex-cli 0.154.0")
    assert calls == []
    assert data["completed"] is False
    assert data["error"].startswith("StaleClient: openai: installed client codex-cli 0.154.0 is below "
                                    "the qualified minimum 0.163.0")
    assert (result.run_dir / "report.md").exists()


def test_a_qualified_client_runs(tmp_path, monkeypatch):
    _, calls, data = _run_with_client(tmp_path, monkeypatch, "codex-cli 0.163.0")
    assert calls, "the run reached the orchestrator"
    assert "StaleClient" not in (data["error"] or "")


def test_no_environment_override_lifts_the_minimum(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_CLI_MIN_OPENAI", "0")
    _, calls, data = _run_with_client(tmp_path, monkeypatch, "codex-cli 0.154.0")
    assert calls == [] and data["error"].startswith("StaleClient:")
    assert "QUADRATUS_CLI_MIN" not in data["error"]
    assert os.environ.get("QUADRATUS_CLI_MIN_OPENAI") == "0"
