"""Offline permission checks: no vendor processes or network calls."""

import shlex

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import ClaudeCLIProvider, claude_check_rule
from quadratus.config import Settings
from quadratus.delegation import invocation
from quadratus.integration import GateCommand
from quadratus.project_run import _project_check_command, run_project
from quadratus.providers import ProviderError
from quadratus.runtime import Fleet, new_session

CHECK = "python -m pytest -q"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/unused/claude")
    monkeypatch.delenv("QUADRATUS_CLI_ARGS_CLAUDE", raising=False)
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")


@pytest.mark.parametrize("contained", [False, True])
def test_project_write_has_scoped_edit_and_exact_check(tmp_path, monkeypatch, contained):
    if contained:
        monkeypatch.setenv("QUADRATUS_CONTAINED", "1")
    else:
        monkeypatch.delenv("QUADRATUS_CONTAINED", raising=False)
    provider = ClaudeCLIProvider("opus").in_directory(tmp_path, allow_writes=True)
    provider.check_commands = (CHECK,)
    argv = provider._build_argv("p", "")
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--allowedTools") + 1] == f"Bash({CHECK})"
    assert argv.count("--allowedTools") == 1
    denied = argv[argv.index("--disallowed-tools") + 1].split()
    assert {"Task", "Agent", "mcp__*"} <= set(denied)
    assert "--dangerously-skip-permissions" not in argv
    assert "--add-dir" not in argv


@pytest.mark.parametrize("restricted,write,summary", [
    (False, False, False), (True, False, False), (True, True, False),
    (True, True, True),
])
def test_readonly_and_bounded_views_never_receive_permissions(tmp_path, restricted, write, summary):
    provider = ClaudeCLIProvider("opus", max_retries=1, timeout=60)
    provider = provider.for_seat("opus", restricted=restricted).in_directory(tmp_path, allow_writes=write)
    provider.summary_only = summary
    provider.check_commands = (CHECK,)
    argv = provider._build_argv("p", "")
    assert "--allowedTools" not in argv and "--permission-mode" not in argv
    assert {"Bash", "Edit", "Write"} <= set(argv[argv.index("--disallowed-tools") + 1].split())


def test_check_and_worker_tool_share_one_allow_option(tmp_path):
    provider = ClaudeCLIProvider("opus").in_directory(tmp_path, allow_writes=True)
    provider.check_commands = (CHECK,)
    provider.worker_tool = dict(name="quadratus", command="/py", args=["/srv.py"], env={}, timeout=900)
    argv = provider._build_argv("p", "")
    offset = argv.index("--allowedTools")
    assert argv.count("--allowedTools") == 1
    assert argv[offset + 1:offset + 3] == ["mcp__quadratus__commission_worker", f"Bash({CHECK})"]
    assert "--strict-mcp-config" in argv
    denied = argv[argv.index("--disallowed-tools") + 1].split()
    assert "Agent" in denied and "Task" in denied and "mcp__*" not in denied


@pytest.mark.parametrize("command", ["", "pytest *", "pytest; id", "pytest && id",
    "pytest | cat", "pytest > out", "pytest < in", "echo `id`", "echo $HOME",
    "pytest\nwhoami", "pytest\t-q", "python -c 'f()'", "pytest\\x", "pytest\x7f"])
def test_unsafe_allow_rule_is_refused(command):
    with pytest.raises(ProviderError, match="exact Claude Bash rule"):
        claude_check_rule(command)


def test_quoted_filename_is_literal():
    assert claude_check_rule(shlex.join(["pytest", "tests/a b.py"])) == "Bash(pytest 'tests/a b.py')"


@pytest.mark.parametrize("argv", [
    ("python", "/external/examiner/check.py"),
    ("python", "../examiner/check.py"),
    ("pytest", ".."),
    ("pytest", "--rootdir=.."),
    ("pytest", "--config=/external/examiner/config.ini"),
    ("pytest", "-I/external/examiner"),
    ("/external/examiner/check",),
])
def test_external_examiner_checks_remain_runner_only(tmp_path, argv):
    assert _project_check_command(argv, tmp_path) is None


def test_project_script_and_installed_interpreter_can_be_granted(tmp_path):
    assert _project_check_command(("/usr/bin/python3", "tests/check.py"), tmp_path) == '/usr/bin/python3 tests/check.py'


@pytest.mark.parametrize('script', ['check.py', './check.py'])
def test_symlinked_external_check_is_not_exposed(tmp_path, script):
    (tmp_path / 'check.py').symlink_to('/external/examiner/check.py')
    assert _project_check_command(("python", script), tmp_path) is None


def test_fleet_grants_check_only_to_authorized_write_view(tmp_path, monkeypatch):
    fleet = Fleet(Settings(backend="cli"), project=tmp_path, allow_writes=True,
                  check_commands=(CHECK,))
    provider = ClaudeCLIProvider("opus")
    monkeypatch.setattr(fleet, "provider_for", lambda _: provider)
    seen = []

    def generate(key, view, prompt, system):
        seen.append((view._workdir, view._build_argv(prompt, system), system))
        return 'CHANGED: []'

    monkeypatch.setattr(fleet, "_generate", generate)
    with invocation("t1", "lead"):
        fleet._invoke("claude:opus", "p", allow_writes=True)
    with invocation("t1", "verifier"):
        fleet._invoke("claude:opus", "p")
    assert seen[0][0] == str(tmp_path)
    assert CHECK in seen[0][2] and "--allowedTools" in seen[0][1]
    assert seen[1][0] != str(tmp_path) and "--allowedTools" not in seen[1][1]
    assert CHECK not in seen[1][2]
    assert provider.check_commands == () and not provider._allow_writes


def test_fleet_unsupported_checks_remain_runner_only(tmp_path):
    fleet = Fleet(Settings(), project=tmp_path, allow_writes=True,
                  check_commands=(CHECK, "python -c 'f()'", CHECK))
    assert fleet.check_commands == (CHECK,)
    assert Fleet(Settings(), project=tmp_path, check_commands=(CHECK,)).check_commands == ()


def test_parallel_copy_preserves_grants_on_its_own_tree(tmp_path):
    project = tmp_path / "source"
    child_root = tmp_path / "copy"
    project.mkdir()
    child_root.mkdir()
    fleet = Fleet(Settings(), project=project, allow_writes=True, check_commands=(CHECK,))
    session = new_session("goal", ArtifactStore(tmp_path / "artifacts"), fleet=fleet)
    invoke, close = session.config.fork(child_root)
    child = invoke.__self__
    assert child.project.root == child_root and child.check_commands == (CHECK,)
    assert child.allow_writes
    close()
    fleet.close()


@pytest.mark.parametrize("gates,expected", [
    (None, (CHECK,)),
    ([GateCommand("root", ("python", "-m", "pytest", "-q")),
      GateCommand("nested", ("npm", "test"), cwd="web"),
      GateCommand("skip", ("echo", "skip"), skip_reason="unavailable")], (CHECK,)),
])
def test_project_checks_are_plumbed_from_root_gates(tmp_path, monkeypatch, gates, expected):
    (tmp_path / "web").mkdir()
    seen = {}

    class FakeFleet:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

        def close(self):
            pass

    def factory(goal, store, *, config, **kw):
        from quadratus.session import Session
        session = Session(goal, store, lambda *a, **kw: "", config=config)

        def stop(**kw):
            raise ProviderError("offline stop")

        session.run = stop
        return session

    monkeypatch.setattr("quadratus.runtime.Fleet", FakeFleet)
    monkeypatch.setattr("quadratus.runtime.new_session", factory)
    result = run_project("goal", tmp_path, Settings(), allow_writes=True, check=CHECK, gates=gates)
    assert not result.completed
    assert seen["check_commands"] == expected and seen["project"].root == tmp_path
