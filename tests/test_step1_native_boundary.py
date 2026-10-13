"""No model calls; retained native permissions plus stricter private reads."""

import importlib.util
import tomllib
from pathlib import Path

import pytest

_path = Path(__file__).parents[1] / "tools" / "step1_native_boundary.py"
_spec = importlib.util.spec_from_file_location("step1_boundary", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
protect = _module.protected_codex_argv


@pytest.fixture
def roots(tmp_path):
    work, private = tmp_path / "work", tmp_path / "private"
    work.mkdir()
    private.mkdir()
    return work.resolve(), private.resolve()


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_preserves_model_effort_and_controls_but_denies_private(roots, mode):
    work, private = roots
    args = ["codex", "exec", "--model", "gpt-5.6-sol", "-c", "model_reasoning_effort=low",
            "--sandbox", mode, "--json", "-"]
    result = protect(args, protected=[str(private)], cwd=str(work))
    assert result[-7:] == ["exec", "--model", "gpt-5.6-sol", "-c",
                           "model_reasoning_effort=low", "--json", "-"]
    assert "--sandbox" not in result
    value = next(item.split("=", 1)[1] for item in result if item.startswith("permissions."))
    profile = tomllib.loads("profile=" + value)["profile"]
    assert profile["filesystem"][str(private)] == "deny"
    assert profile["extends"] == (":read-only" if mode == "read-only" else ":workspace")


@pytest.mark.parametrize("extra", [
    ["--sandbox", "danger-full-access"], ["--sandbox", "read-only"],
    ["--permission-profile", "other"], ["--add-dir", "/"],
    ["--dangerously-bypass-approvals-and-sandbox"],
    ["-c", 'permissions.other.filesystem.":root"="write"'],
    ["-cpermissions.foo.extends=\":workspace\""],
    ["--sandbox=workspace-write"], ["-sworkspace-write"],
    ["-c", "sandbox_workspace_write.writable_roots=[\"/\"]"],
])
def test_conflicting_or_expanded_boundaries_refused(roots, extra):
    work, private = roots
    with pytest.raises(ValueError):
        protect(["codex", "exec", "--sandbox", "read-only", *extra],
                protected=[str(private)], cwd=str(work))


def test_copy_does_not_gain_external_tmp_writes(roots):
    work, private = roots
    result = protect(["codex", "exec", "--sandbox", "workspace-write", "-c",
                      "sandbox_workspace_write.exclude_slash_tmp=true", "-c",
                      "sandbox_workspace_write.exclude_tmpdir_env_var=true", "-c",
                      "sandbox_workspace_write.writable_roots=[]"],
                     protected=[str(private)], cwd=str(work), tempdir=str(private))
    value = next(item.split("=", 1)[1] for item in result if item.startswith("permissions."))
    filesystem = tomllib.loads("profile=" + value)["profile"]["filesystem"]
    assert filesystem[":slash_tmp"] == "read"
    assert filesystem[":tmpdir"] == "read"


def test_protected_source_inside_work_refused(roots):
    work, _ = roots
    with pytest.raises(ValueError):
        protect(["codex", "exec", "--sandbox", "workspace-write"],
                protected=[str(work / "hidden")], cwd=str(work))


def test_disposable_config_preserves_models_effort_and_other_profiles():
    original = ('model = "gpt-5.6-sol"\nsandbox_mode = "danger-full-access"\n'
                'model_reasoning_effort = "low"\n[profiles.other]\n'
                'sandbox_mode = "read-only"\n')
    result = _module.without_legacy_mode(original)
    assert result == ('model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\n'
                      '[profiles.other]\nsandbox_mode = "read-only"\n')


def test_unfamiliar_legacy_format_refused():
    with pytest.raises(ValueError):
        _module.without_legacy_mode('"sandbox_mode" = "danger-full-access"\n')


def test_ambient_readers_removed_without_changing_model_effort_or_approvals():
    source = ('model="gpt-5.6-sol"\nmodel_reasoning_effort="low"\napproval_policy="never"\n'
              'sandbox_mode="danger-full-access"\nnotify=["/some/ambient-hook"]\n'
              '[mcp_servers.ambient]\ncommand="node"\n[plugins.some]\nenabled=true\n'
              '[features]\nmemories=true\ncode_mode_host=true\n')
    result = tomllib.loads(_module.isolated_solver_config(source))
    assert result['model'] == 'gpt-5.6-sol'
    assert result['model_reasoning_effort'] == 'low' and result['approval_policy'] == 'never'
    assert not {'sandbox_mode', 'mcp_servers', 'plugins', 'notify'} & result.keys()
    assert result['features']['memories'] is False and result['features']['apps'] is False
    assert result['features']['code_mode_host'] is True


def test_runtime_exception_cannot_reopen_operator_root(roots):
    work, private = roots
    with pytest.raises(ValueError):
        protect(['codex', 'exec', '--sandbox', 'workspace-write'], protected=[str(private)],
                cwd=str(work), readonly_runtime=[str(private)])


@pytest.mark.parametrize('setting', [
    'mcp_servers.amb.command="node"', 'features.computer_use=true', 'notify=["/x"]',
    'profile="x"', 'profiles.x.sandbox_mode="danger-full-access"',
])
def test_config_override_cannot_restore_removed_reader(roots, setting):
    work, private = roots
    with pytest.raises(ValueError):
        protect(['codex', 'exec', '--sandbox', 'workspace-write', '-c', setting],
                protected=[str(private)], cwd=str(work))


@pytest.mark.parametrize('option', ['-C', '--cd'])
def test_cli_cwd_must_match_validated_workspace(roots, option):
    work, private = roots
    with pytest.raises(ValueError):
        protect(['codex', 'exec', '--sandbox', 'workspace-write', option, '/'],
                protected=[str(private)], cwd=str(work))


def test_selected_profile_refused_and_inactive_profiles_do_not_survive():
    with pytest.raises(ValueError):
        _module.isolated_solver_config('profile="x"\n[profiles.x]\nsandbox_mode="danger-full-access"\n')
    result = tomllib.loads(_module.isolated_solver_config(
        '[profiles.x]\nsandbox_mode="danger-full-access"\n[sandbox_workspace_write]\nwritable_roots=["/private/grader"]\n'))
    assert 'profiles' not in result and 'sandbox_workspace_write' not in result


def test_only_exact_held_bounded_bridge_override_is_allowed(roots):
    work, private = roots
    value = '"held-bridge-command"'
    key = 'mcp_servers.quadratus.command'
    original = ['codex', 'exec', '--sandbox', 'workspace-write', '-c', key+'='+value,
                '--disable', 'multi_agent', '--disable', 'multi_agent_v2', '-c', 'agents.enabled=false']
    result = protect(original, protected=[str(private)], cwd=str(work), trusted_worker_config={key:value})
    assert key+'='+value in result and 'agents.enabled=false' in result
    with pytest.raises(ValueError):
        protect(original, protected=[str(private)], cwd=str(work), trusted_worker_config={key:'"other"'})
