"""No child process inherits credentials it does not need (review 2026-11-10:
transport 2, checks_fs 4, gui_records 6).

The vendor CLI child used to get the harness's whole environment minus six
key names, and every check, preview server, capture and readiness probe got
all of it. A model with a shell, or project code in a check, could read
GITHUB_TOKEN, cloud keys and the decider's key; an inherited auth token, base
URL or provider switch could still move a subscription call onto billed or
redirected transport.

The call-site tests start real child processes: a fake vendor CLI, a check
command, a preview server, a capture command and a probe that each report
which of the watched names they can see.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import socket
import sys
import time
from pathlib import Path

import pytest

from quadratus import child_env, cli_providers, preview
from quadratus.cli_providers import ClaudeCLIProvider, CodexCLIProvider, GrokCLIProvider
from quadratus.integration import GateCommand, GateSuite, IntegrationGate
from quadratus.readiness import Probe, run_probe

#: The four the brief plants at every call site.
PLANTED = ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY")
#: Planted beside them: what can move a subscription call, and the sign-in.
TRANSPORT = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "CODEX_API_KEY",
             "CLAUDE_CODE_USE_BEDROCK")
LOGIN = "CLAUDE_CODE_OAUTH_TOKEN"
#: Lookalikes planted at every call site: they must still arrive.
HARMLESS = ("QUADRATUS_KEYBOARD_LAYOUT", "QUADRATUS_MONKEY")
WATCHED = (*PLANTED, *TRANSPORT, LOGIN, *HARMLESS, "PATH", "HOME")

#: Prints the watched names this process can see, as a JSON list.
REPORT = ("import json, os; print(json.dumps(sorted(n for n in os.environ if n in "
          + repr(set(WATCHED) | {"GROK_WORKFLOWS", "QUADRATUS_PROBE_DIR", "APP_PORT"}) + ")))")


@pytest.fixture(autouse=True)
def planted(monkeypatch):
    for name in (*PLANTED, *TRANSPORT, LOGIN):
        monkeypatch.setenv(name, f"value-of-{name.lower()}")
    for name in HARMLESS:
        monkeypatch.setenv(name, "kept")
    monkeypatch.delenv(child_env.PASS_VARIABLE, raising=False)
    for vendor in ("OPENAI", "CLAUDE", "GROK"):
        monkeypatch.delenv(f"QUADRATUS_CLI_ARGS_{vendor}", raising=False)
    for name in ("QUADRATUS_NATIVE_DELEGATION", "QUADRATUS_NEUTRAL_PREFERENCES", "QUADRATUS_CONTAINED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(child_env, "_announced", set())  # each test logs afresh


def _seen(text):
    """The JSON list a reporting child printed, from wherever it landed."""
    start = text.index("[")
    return set(json.loads(text[start:text.index("]", start) + 1]))


def _clean(seen, *, login=False):
    for name in (*PLANTED, *TRANSPORT):
        assert name not in seen, f"{name} reached the child"
    assert (LOGIN in seen) is login
    assert {"PATH", "HOME", *HARMLESS} <= seen, "the ordinary environment still arrives"


# -- the rule, name by name ------------------------------------------------------------

REMOVED = [
    # credential words as whole parts, in any case, with or without a number
    "GITHUB_TOKEN", "GH_TOKEN", "TYPESAFE_API_KEY", "ACME_APIKEY", "ACME_KEY", "ACME_TOKEN",
    "ACME_SECRET", "SECRET_KEY_BASE", "DB_PASSWORD", "PGPASSWORD", "MYSQL_PASSWD", "ACME_CREDENTIAL",
    "GOOGLE_APPLICATION_CREDENTIALS", "NODE_AUTH_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "acme_api_key", "Acme_Token", "OPENAI_API_KEY2", "API_KEY_2", "AUTH0_CLIENT_SECRET",
    "NGROK_AUTHTOKEN", "GPG_PASSPHRASE", "ACME_CLIENTSECRET", "S3_ACCESSKEY", "TS_AUTHKEY",
    "npm_config_//registry.npmjs.org/:_authToken", "CLAUDE_CODE_MESSAGING_TOKEN",
    "HOMEBREW_GITHUB_API_TOKEN", "HTTP_AUTHORIZATION", "AUTHORIZATION",
    # the short spellings of a password, a cookie, and the plurals that are always secrets
    "DB_PASS", "SMTP_PASS", "MYSQL_PWD", "SSHPASS", "ERLANG_COOKIE", "APP_SECRETS", "ADMIN_PASSWORDS",
    "VALID_API_KEYS", "ACME_ACCESS_TOKENS",
    # tool families
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_REGION",
    "AZURE_TENANT_ID", "GCP_PROJECT", "GOOGLE_CLOUD_PROJECT", "GH_HOST", "GITHUB_REPOSITORY",
    "GITLAB_HOST", "NPM_CONFIG_USERCONFIG", "npm_config_registry", "PYPI_USERNAME",
    "TWINE_USERNAME", "DOCKER_HOST", "DOCKER_CONFIG", "OP_SERVICE_ACCOUNT_TOKEN", "VAULT_ADDR",
    # named whole
    "SSH_AUTH_SOCK", "GPG_AGENT_INFO", "KUBECONFIG",
    # what moves a subscription call onto billed or redirected transport
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_FOUNDRY_RESOURCE",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_HOST",
    "XAI_API_KEY", "XAI_BASE_URL", "GROK_API_KEY", "GROK_BASE_URL", "GROK_DEPLOYMENT_KEY",
    "CODEX_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE", "AWS_BEARER_TOKEN_BEDROCK", "GEMINI_API_KEY", "GEMINI_MODEL", "GOOGLE_API_KEY",
    # every vendor's sign-in variable, in the check profile
    "CLAUDE_CODE_OAUTH_TOKEN",
]

SURVIVES = [
    # lookalikes: the word is inside a part, or is a plural that is no secret
    "KEYBOARD_LAYOUT", "MONKEY", "HARMLESS_KEYBOARD", "MAX_THINKING_TOKENS",
    "BUN_TLS_EXTRA_DISTRUSTED_KEYS", "XAUTHORITY", "GIT_AUTHOR_NAME", "GIT_ASKPASS",
    "GNOME_KEYRING_CONTROL", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_ID", "TOKENIZERS_PARALLELISM",
    "PWD", "OLDPWD", "SSH_ASKPASS", "BYPASS_CACHE", "COMPASS_MODE", "MAX_OUTPUT_TOKENS",
    "CCR_AUTO_MODE_USER_ENV_KEYS_FACT", "SECURITYSESSIONID", "DESKTOP_SESSION",
    # git's numbered config entries: without the key git itself stops working
    "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_KEY_12", "GIT_CONFIG_VALUE_0",
    # the basics, proxies in any case and under any prefix, CA bundles
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "PYTHONPATH", "NODE_OPTIONS",
    "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "DOCKER_HTTPS_PROXY", "YARN_HTTPS_PROXY",
    "npm_config_https_proxy", "npm_config_noproxy", "NPM_CONFIG_CAFILE", "NPM_CONFIG_CA",
    "NPM_CONFIG_STRICT_SSL", "AWS_CA_BUNDLE",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "GIT_SSL_CAINFO", "PIP_CERT", "XDG_CONFIG_HOME", "XDG_DATA_DIRS", "XDG_RUNTIME_DIR",
    # vendor home folders and settings that pick a model, not a transport
    "CODEX_HOME", "CLAUDE_CONFIG_DIR", "GROK_HOME", "ANTHROPIC_MODEL", "OPENAI_MODEL", "GROK_MODEL",
    "GROK_WORKFLOWS", "CLAUDE_CODE_DISABLE_WORKFLOWS", "CLAUDE_CODE_PROXY_RESOLVES_HOSTS",
    "PLAYWRIGHT_BROWSERS_PATH", "QUADRATUS_CHROMIUM", "QUADRATUS_ENV_PASS",
]


@pytest.mark.parametrize("name", REMOVED)
def test_a_credential_shaped_or_transport_moving_name_is_left_out_of_both_profiles(name):
    base = {"PATH": "/bin", name: "secret-value"}
    env, removed = child_env.check_env(base)
    assert name not in env and removed == [name]
    for vendor in ("openai", "grok", ""):
        env, removed = child_env.vendor_env(vendor, base)
        assert name not in env and removed == [name], vendor
    env, removed = child_env.vendor_env("claude", base)
    assert (name in env) is (name == LOGIN), "claude keeps its own sign-in variable and nothing else"
    assert base == {"PATH": "/bin", name: "secret-value"}, "the source mapping is never changed"


@pytest.mark.parametrize("name", SURVIVES)
def test_a_lookalike_or_plumbing_name_survives_both_profiles(name):
    base = {name: "kept"}
    assert child_env.check_env(base) == ({name: "kept"}, [])
    for vendor in ("claude", "openai", "grok"):
        assert child_env.vendor_env(vendor, base) == ({name: "kept"}, [])


def test_only_the_calling_vendor_keeps_a_sign_in_variable():
    base = {LOGIN: "sk-ant-oat01-subscription", "PATH": "/bin"}
    assert child_env.vendor_env("claude", base) == (base, [])
    for vendor in ("openai", "grok"):
        env, removed = child_env.vendor_env(vendor, base)
        assert LOGIN not in env and removed == [LOGIN], f"a {vendor} child never gets the claude token"
    env, removed = child_env.check_env(base)
    assert LOGIN not in env and removed == [LOGIN], "project code has no use for a vendor login"
    assert set(child_env.VENDOR_LOGIN) == {"claude"}, "codex and grok sign in through files and keep none"


def test_the_names_removed_come_back_sorted_and_the_parent_environment_is_untouched(monkeypatch):
    before = dict(os.environ)
    env, removed = child_env.check_env()
    assert removed == sorted(removed) and set(PLANTED) <= set(removed)
    assert dict(os.environ) == before, "the harness's own process keeps every variable"
    assert set(env) == set(before) - set(removed)
    assert child_env.withheld_names() == removed


def test_the_harness_still_reads_its_own_keys_in_process(monkeypatch):
    """Ruling 4: this is about children. The billed API backend and the Jev
    decider read their keys from the harness's own environment."""
    from quadratus.config import Settings
    from quadratus.decisions import JevDecider
    monkeypatch.setenv("OPENAI_API_KEY", "sk-operator-opted-in")
    child_env.check_env()
    child_env.vendor_env("claude")
    settings = Settings()
    assert settings.anthropic_api_key == "value-of-anthropic_api_key"
    assert settings.openai_api_key == "sk-operator-opted-in"
    assert JevDecider().api_key == "value-of-typesafe_api_key"


# -- the operator's escape -------------------------------------------------------------

def test_env_pass_keeps_exact_names_for_both_profiles():
    base = {"GITHUB_TOKEN": "a", "DOCKER_HOST": "b", "ANTHROPIC_BASE_URL": "c", "AWS_PROFILE": "d",
            child_env.PASS_VARIABLE: " DOCKER_HOST , ANTHROPIC_BASE_URL,,github_token"}
    for build in (child_env.check_env, lambda b: child_env.vendor_env("openai", b)):
        env, removed = build(base)
        assert env["DOCKER_HOST"] == "b" and env["ANTHROPIC_BASE_URL"] == "c"
        assert removed == ["AWS_PROFILE", "GITHUB_TOKEN"], "exact names only: another case is another name"
    assert child_env.withheld_names(base) == ["AWS_PROFILE", "GITHUB_TOKEN"]


#: The six names the old loop removed, and the one beside them the review found.
BILLED = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY",
          "GROK_API_KEY", "CODEX_API_KEY")


@pytest.mark.parametrize("name", BILLED)
def test_env_pass_never_hands_a_vendor_cli_a_billed_api_key(name, caplog):
    """A key the project's tests need must not move the model calls onto
    billed transport as a side effect. The old loop removed these with no way
    back, and the escape does not weaken that."""
    caplog.set_level(logging.INFO, logger="quadratus.child_env")
    base = {name: "sk-billed", "GITHUB_TOKEN": "ghp-secret", "PATH": "/bin",
            child_env.PASS_VARIABLE: f"{name},GITHUB_TOKEN"}
    env, removed = child_env.check_env(base)
    assert env[name] == "sk-billed" and removed == [], "a check keeps what the operator names"
    for vendor in ("claude", "openai", "grok", ""):
        caplog.clear()
        env, removed = child_env.vendor_env(vendor, base)
        assert name not in env and removed == [name], vendor
        assert env["GITHUB_TOKEN"] == "ghp-secret", "every other name the operator passed still arrives"
        said = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(said) == 1, "part of what the operator asked for is not done: never left at info"
        assert name in said[0].getMessage() and "billed API key" in said[0].getMessage()
        assert child_env.PASS_VARIABLE in said[0].getMessage()
        child_env.vendor_env(vendor, base)
        child_env.vendor_env(vendor, base)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1, "said once"
        hinted = [r.getMessage() for r in caplog.records if "to keep one" in r.getMessage()]
        assert not hinted, "the way out is never offered for a name it cannot keep, on any call"
    assert "sk-billed" not in caplog.text and "ghp-secret" not in caplog.text
    assert set(BILLED) == set(child_env.NEVER_FOR_A_VENDOR_CLI)


def test_env_pass_never_hands_one_vendors_sign_in_to_another_vendors_cli(caplog):
    caplog.set_level(logging.INFO, logger="quadratus.child_env")
    base = {LOGIN: "sk-ant-oat01-subscription", "PATH": "/bin", child_env.PASS_VARIABLE: LOGIN}
    assert child_env.vendor_env("claude", base) == (base, []), "its own sign-in needs no escape"
    assert caplog.records == []
    for vendor in ("openai", "grok", ""):
        env, removed = child_env.vendor_env(vendor, base)
        assert LOGIN not in env and removed == [LOGIN], vendor
    assert all(r.levelno == logging.WARNING and "another vendor's sign-in" in r.getMessage()
               for r in caplog.records) and len(caplog.records) == 3
    env, removed = child_env.check_env(base)
    assert env[LOGIN] == base[LOGIN] and removed == [], "a check that runs the CLI itself may be given it"
    assert "sk-ant-oat01" not in caplog.text


def test_env_pass_reaches_a_real_check_command(monkeypatch, tmp_path):
    monkeypatch.setenv(child_env.PASS_VARIABLE, "GITHUB_TOKEN")
    result = IntegrationGate([sys.executable, "-c", REPORT], cwd=tmp_path).run()
    seen = _seen(result.output)
    assert "GITHUB_TOKEN" in seen, "the operator named it"
    assert not {"AWS_SECRET_ACCESS_KEY", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY"} & seen


# -- said once, names only -------------------------------------------------------------

def test_the_removed_names_are_logged_once_per_process_and_never_a_value(caplog):
    caplog.set_level(logging.INFO, logger="quadratus.child_env")
    child_env.check_env()
    first = [r.getMessage() for r in caplog.records]
    assert len(first) == 1
    for name in PLANTED:
        assert name in first[0]
    assert child_env.PASS_VARIABLE in first[0], "the message names the way to keep one"
    assert "value-of-" not in caplog.text, "names, never values"
    for _ in range(3):
        child_env.check_env()
    assert len(caplog.records) == 1, "a run of several hundred checks says it once"
    child_env.vendor_env("claude")
    child_env.vendor_env("claude")
    assert len(caplog.records) == 2, "each kind of child is announced once"
    assert LOGIN not in caplog.records[1].getMessage(), "claude keeps its sign-in, so it is not listed"
    assert all(r.levelno == logging.INFO for r in caplog.records)


def test_a_name_kept_by_the_operator_is_logged_as_kept(caplog):
    caplog.set_level(logging.INFO, logger="quadratus.child_env")
    child_env.check_env({"GITHUB_TOKEN": "ghp-secret", "PATH": "/bin", child_env.PASS_VARIABLE: "GITHUB_TOKEN"})
    assert [r.getMessage() for r in caplog.records] == [
        f"checks, previews and probes keep GITHUB_TOKEN because {child_env.PASS_VARIABLE} names it."]
    assert "ghp-secret" not in caplog.text
    assert caplog.records[0].levelno == logging.INFO


def test_a_vendor_token_or_base_url_kept_for_a_vendor_cli_is_a_warning_not_a_whisper(caplog):
    """An operator behind a gateway may hand a vendor CLI its base URL and
    token back, but a subscription call that may now be billed or redirected
    is never left silent."""
    caplog.set_level(logging.INFO, logger="quadratus.child_env")
    base = {"ANTHROPIC_BASE_URL": "https://gateway.example", "ANTHROPIC_AUTH_TOKEN": "gw-secret",
            "GITHUB_TOKEN": "ghp-secret", "PATH": "/bin",
            child_env.PASS_VARIABLE: "ANTHROPIC_BASE_URL,ANTHROPIC_AUTH_TOKEN"}
    env, removed = child_env.vendor_env("claude", base)
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example" and env["ANTHROPIC_AUTH_TOKEN"] == "gw-secret"
    assert removed == ["GITHUB_TOKEN"]
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and len(caplog.records) == 2
    assert "keep ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL" in warned[0].getMessage()
    assert "off the subscription" in warned[0].getMessage()
    child_env.vendor_env("claude", base)
    assert len(caplog.records) == 2, "said once"
    caplog.clear()
    child_env.check_env(base)
    assert all(r.levelno == logging.INFO for r in caplog.records), "a check cannot bill a vendor call"
    assert "gw-secret" not in caplog.text and "ghp-secret" not in caplog.text and "gateway.example" not in caplog.text


# -- call site: the vendor CLI child ----------------------------------------------------

FAKE_CLI = '''
import json, os, sys
reply = json.dumps(sorted(n for n in os.environ if n in WATCHED))
kind = os.path.basename(sys.argv[0])
if kind == "fake-codex":
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": reply}}))
elif kind == "fake-grok":
    print(json.dumps({"text": reply, "stopReason": "end_turn"}))
else:
    print(json.dumps({"type": "result", "result": reply, "is_error": False}))
'''


def _fake_cli(tmp_path, monkeypatch, kind, vendor):
    binary = tmp_path / f"fake-{kind}"
    binary.write_text(f"#!{sys.executable}\nWATCHED = {set(WATCHED) | {'GROK_WORKFLOWS'}!r}\n" + FAKE_CLI)
    binary.chmod(0o755)
    monkeypatch.setenv(f"QUADRATUS_CLI_BINARY_{vendor}", str(binary))


@pytest.mark.parametrize("kind, vendor, provider, login", [
    ("claude", "CLAUDE", lambda: ClaudeCLIProvider(model="opus"), True),
    ("codex", "OPENAI", lambda: CodexCLIProvider("gpt-5.6-sol"), False),
    ("grok", "GROK", lambda: GrokCLIProvider(model=""), False),
])
def test_a_real_vendor_cli_child_sees_no_planted_credential(tmp_path, monkeypatch, kind, vendor, provider, login):
    _fake_cli(tmp_path, monkeypatch, kind, vendor)
    cli = provider()
    try:
        seen = set(json.loads(cli.generate("which names can you see?")))
    finally:
        cli.cleanup()
    _clean(seen, login=login)


@pytest.mark.parametrize("kind, vendor, provider, login", [
    ("claude", "CLAUDE", lambda: ClaudeCLIProvider(model="opus"), True),
    ("codex", "OPENAI", lambda: CodexCLIProvider("gpt-5.6-sol"), False),
    ("grok", "GROK", lambda: GrokCLIProvider(model=""), False),
])
def test_env_pass_reaches_a_real_vendor_cli_child_but_never_with_a_billed_key(tmp_path, monkeypatch, kind,
                                                                             vendor, provider, login):
    _fake_cli(tmp_path, monkeypatch, kind, vendor)
    monkeypatch.setenv(child_env.PASS_VARIABLE, f"GITHUB_TOKEN,ANTHROPIC_API_KEY,CODEX_API_KEY,{LOGIN}")
    cli = provider()
    try:
        seen = set(json.loads(cli.generate("which names can you see?")))
    finally:
        cli.cleanup()
    assert "GITHUB_TOKEN" in seen, "the operator named it"
    assert not {"ANTHROPIC_API_KEY", "CODEX_API_KEY", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_AUTH_TOKEN"} & seen
    assert (LOGIN in seen) is login, "naming the claude sign-in gives it to no other vendor's CLI"


def test_what_the_harness_sets_after_building_the_environment_still_arrives(tmp_path, monkeypatch):
    """The fan-out switch is a GROK_ name the harness sets itself; the
    profile is built first, so nothing the call adds is ever removed."""
    _fake_cli(tmp_path, monkeypatch, "grok", "GROK")
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    cli = GrokCLIProvider(model="", allow_writes=True, workdir=tmp_path)
    seen = set(json.loads(cli.generate("p")))
    assert "GROK_WORKFLOWS" in seen
    _clean(seen)


def test_everything_the_spec_sets_itself_is_kept(monkeypatch):
    class Provider(ClaudeCLIProvider):
        spec = dataclasses.replace(cli_providers.CLAUDE_SPEC,
                                   env={"SPEC_SETTING": "1", "SPEC_SERVICE_TOKEN": "set-by-the-spec"})

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    seen = {}

    def launch(argv, **kwargs):
        seen.update(kwargs["env"])
        return type("Done", (), dict(stdout=json.dumps({"type": "result", "result": "ok"}),
                                     stderr="", returncode=0))()
    monkeypatch.setattr(cli_providers, "_launch", launch)
    cli = Provider(model="opus")
    try:
        cli.generate("q")
    finally:
        cli.cleanup()
    assert seen["SPEC_SETTING"] == "1" and seen["SPEC_SERVICE_TOKEN"] == "set-by-the-spec"
    _clean(set(seen), login=True)


# -- call site: gate commands -----------------------------------------------------------

def test_a_single_check_command_sees_no_planted_credential(tmp_path):
    result = IntegrationGate([sys.executable, "-c", REPORT], cwd=tmp_path).run()
    assert result.passed, result.output
    _clean(_seen(result.output))


def test_a_suite_check_command_sees_no_planted_credential(tmp_path):
    # A file, not -c: the suite looks each argument up as a path.
    (tmp_path / "report_env.py").write_text(REPORT + "\n")
    suite = GateSuite([GateCommand("check", (sys.executable, "report_env.py"))], cwd=tmp_path)
    result = suite.run()
    receipt = result.receipts[0]
    assert receipt.status == "passed", receipt
    _clean(_seen(receipt.output))


# -- call site: the preview server and the capture ---------------------------------------

def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _preview_app(tmp_path, port):
    """A server that writes the watched names it can see, then serves."""
    (tmp_path / "app.py").write_text(
        "import http.server, json, os, socketserver\n"
        f"WATCHED = {set(WATCHED) | {'APP_PORT'}!r}\n"
        "open('preview-seen.json', 'w').write(json.dumps(sorted(n for n in os.environ if n in WATCHED)))\n"
        "socketserver.TCPServer.allow_reuse_address = True\n"
        "socketserver.TCPServer(('127.0.0.1', int(os.environ['APP_PORT'])),"
        " http.server.SimpleHTTPRequestHandler).serve_forever()\n")
    return preview.profile_from_dict(dict(preview=[sys.executable, "app.py"],
                                          origin=f"http://127.0.0.1:{port}",
                                          env={"APP_PORT": str(port)}), tmp_path)


def test_the_preview_server_sees_no_planted_credential_and_keeps_the_profile_env(tmp_path):
    profile = _preview_app(tmp_path, _free_port())
    with preview.running(profile, tmp_path):
        pass
    seen = set(json.loads((tmp_path / "preview-seen.json").read_text()))
    assert "APP_PORT" in seen, "what the operator's profile sets is added after the profile is built"
    _clean(seen)


def test_the_capture_command_sees_no_planted_credential(tmp_path):
    profile = _preview_app(tmp_path, _free_port())
    out = tmp_path / "capture-seen.json"
    code = REPORT.replace("print(", f"open({str(out)!r}, 'w').write(")
    failure, _ = preview._capture_once(profile, tmp_path, [sys.executable, "-c", code],
                                       time.monotonic() + 60, 30.0)
    assert failure == "", failure
    _clean(set(json.loads(out.read_text())))


def test_env_pass_gives_the_preview_an_inherited_value_as_the_refusal_says(tmp_path, monkeypatch):
    monkeypatch.setenv(child_env.PASS_VARIABLE, "GITHUB_TOKEN")
    profile = _preview_app(tmp_path, _free_port())
    with preview.running(profile, tmp_path):
        pass
    seen = set(json.loads((tmp_path / "preview-seen.json").read_text()))
    assert "GITHUB_TOKEN" in seen and "AWS_SECRET_ACCESS_KEY" not in seen


# -- call site: readiness probes --------------------------------------------------------

def test_a_readiness_probe_sees_no_planted_credential(tmp_path):
    receipt = run_probe(Probe("env", (sys.executable, "-c", REPORT)), tmp_path)
    assert receipt.passed, receipt
    seen = _seen(receipt.output)
    assert "QUADRATUS_PROBE_DIR" in seen, "what the probe runner sets itself still arrives"
    _clean(seen)


# -- one pattern: what a child does not inherit, a capture profile may not set -----------

def _profile_env(tmp_path, name):
    (tmp_path / "app.py").write_text("")
    return preview.profile_from_dict(dict(preview=["python", "app.py"], origin="http://127.0.0.1:5000",
                                          env={name: "x"}), tmp_path)


@pytest.mark.parametrize("name", sorted(n for n in REMOVED if preview._ENV_NAME.fullmatch(n)))
def test_a_capture_profile_may_not_set_a_name_a_child_would_not_inherit(tmp_path, name):
    with pytest.raises(ValueError, match="may not be set") as refused:
        _profile_env(tmp_path, name)
    assert child_env.PASS_VARIABLE in str(refused.value), "the refusal names the operator's way out"


@pytest.mark.parametrize("name", ["MONKEY", "KEYBOARD_LAYOUT", "APP_SESSION", "APP_COOKIE", "PATH",
                                  "LD_PRELOAD", "PYTHONPATH", "NODE_OPTIONS", "GIT_CONFIG_KEY_0"])
def test_the_profile_refusal_stays_as_strict_as_it_was(tmp_path, name):
    """The capture profile matches the credential words anywhere in a name,
    which is stricter than what a child inherits; none of that is loosened."""
    with pytest.raises(ValueError, match="may not be set"):
        _profile_env(tmp_path, name)


def test_the_profile_refusal_pattern_is_the_one_it_was():
    """Built from the shared word list now, and still the same expression."""
    assert preview._ENV_RESERVED.pattern == (
        "PATH|HOME|SHELL|USER|TMPDIR|.*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH"
        "|COOKIE|SESSION).*|(?:LD|DYLD|PYTHON|NODE|NPM|UV|PIP|GIT|SSH|AWS|GCP|AZURE)_.*"
        "|PYTHON.*|NODE.*")


def test_the_profile_refusal_is_built_from_the_one_word_list(tmp_path):
    for word in child_env.CREDENTIAL_WORDS:
        assert preview._ENV_RESERVED.fullmatch(f"APP{word}X"), word
        assert child_env.credential_shaped(f"APP_{word}_X") and not child_env.credential_shaped(f"APP{word}X")
    for allowed in ("APP_PORT", "DOCKER_HTTPS_PROXY"):
        assert dict(_profile_env(tmp_path, allowed).env) == {allowed: "x"}


# -- the old idiom stays out ------------------------------------------------------------

def test_the_modules_that_start_a_model_or_project_code_never_copy_the_whole_environment():
    """Every old site copied os.environ and added or subtracted. These four
    modules start vendor CLIs, checks, previews, captures and probes; a new
    child there starts from a child_env profile, so the copy idiom stays out."""
    copies = re.compile(r"dict\(\s*os\.environ|\*\*\s*os\.environ|os\.environ\.copy\(\)|env\s*=\s*os\.environ\s*[,)]")
    package = Path(child_env.__file__).parent
    found = [f"{name}:{number}"
             for name in ("cli_providers.py", "integration.py", "preview.py", "readiness.py")
             for number, line in enumerate((package / name).read_text().splitlines(), 1) if copies.search(line)]
    assert found == [], "build the child's environment with child_env.check_env or vendor_env"
    for old in ("env = {**os.environ, **spec.env}", "env = dict(os.environ, X=1)", "run(argv, env=os.environ)",
                "env = os.environ.copy()"):
        assert copies.search(old), old
