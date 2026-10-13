"""Development-only Codex boundary; no provider calls or global config writes.

Use one native permission profile instead of nesting macOS sandboxes. Other
vendors and scored benchmark adapters are deliberately outside this helper.
"""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from pathlib import Path
from typing import Sequence


def without_legacy_mode(config: str) -> str:
    """Prepare a disposable config; preserve every other parsed setting.

    Never edits the operator's config. Native exec selects legacy sandbox_mode
    ahead of default_permissions, so retaining it would silently drop denials.
    Refuse unfamiliar formatting rather than guessing how to rewrite it.
    """
    original = tomllib.loads(config)
    if "sandbox_mode" not in original:
        return config
    if original["sandbox_mode"] not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError("Unknown legacy mode")
    prefix, separator, sections = config.partition("\n[")
    cleaned, count = re.subn(r"(?m)^sandbox_mode\s*=\s*[^\n]+(?:\n|$)", "", prefix)
    if count != 1:
        raise ValueError("Legacy mode cannot be safely isolated")
    result = cleaned + separator + sections
    expected = dict(original)
    del expected["sandbox_mode"]
    if tomllib.loads(result) != expected:
        raise ValueError("Non-permission settings changed")
    return result


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return "{" + ",".join(json.dumps(k) + "=" + _toml_value(v) for k, v in value.items()) + "}"
    raise ValueError("Unsupported config value; no guessing or coercion")


def isolated_solver_config(config: str) -> str:
    """Narrow a disposable worker's ambient tools, preserving model settings.

    Native profiles govern shell execution, not arbitrary MCP/plugin children
    or parent-side memories. A private-evidence worker must not inherit those
    uncontained readers. The harness's explicit bounded worker bridge is
    supplied separately by its own trusted argv. No operator file is edited.
    """
    document = tomllib.loads(without_legacy_mode(config))
    if document.get("profile"):
        raise ValueError("A selected user profile requires an explicitly reviewed flattened config")
    if document.get("model_providers") or document.get("model_provider") not in (None, "openai"):
        raise ValueError("Custom provider definitions require separate subscription/parent-reader review")
    # The native parent can open instruction/config paths before its shell
    # sandbox exists. Preserve the actual model/effort/approval scalars, not
    # arbitrary path-valued user settings or project trust layers.
    safe = {"model", "model_provider", "model_reasoning_effort", "model_reasoning_summary",
            "model_verbosity", "model_context_window", "model_auto_compact_token_limit",
            "approval_policy", "approvals_reviewer", "service_tier", "cli_auth_credentials_store"}
    narrowed = {k: v for k, v in document.items() if k in safe}
    if any(not isinstance(v, (str, int, float, bool)) for v in narrowed.values()):
        raise ValueError("Model/approval settings must be scalar")
    features = dict(document.get("features", {}))
    for feature in ("apps", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
                    "computer_use", "memories", "chronicle"):
        features[feature] = False
    narrowed["features"] = features
    # The controller pins the child's environment; shells do not inherit
    # arbitrary API keys or startup script paths from the operator.
    narrowed["shell_environment_policy"] = {"inherit": "core"}
    document = narrowed
    result = "\n".join(json.dumps(k) + "=" + _toml_value(v) for k, v in document.items()) + "\n"
    if tomllib.loads(result) != document:
        raise ValueError("Configuration round trip failed")
    return result


def require_no_worker_project_config(cwd: str) -> Path:
    """Call before launch and after shutdown; preserve a violating result.

    Exec can mark the disposable config's workspace trusted. Never reuse that
    config, and refuse a worker-owned .codex tree rather than letting a later
    invocation load additional parent tools or settings from it.
    """
    root = Path(cwd).resolve(strict=True)
    if not root.is_dir() or os.path.lexists(root / ".codex"):
        raise ValueError("Worker project .codex configuration must be absent")
    return root


def protected_codex_argv(argv: Sequence[str], *, protected: Sequence[str],
                         cwd: str, tempdir: str | None = None,
                         readonly_runtime: Sequence[str] = (),
                         trusted_worker_config: dict[str, str] | None = None) -> list[str]:
    """Narrow existing read-only/workspace-write commands without adding grants.

    The caller must separately review the trusted native parent and qualify
    actual model tools. Shell-only proof is insufficient for scored admission.
    """
    if not argv or not protected:
        raise ValueError("Exact client argv and protected roots required")
    root = require_no_worker_project_config(cwd)
    result = [argv[0]]
    body: list[str] = []
    mode = None
    excludes: dict[str, bool] = {}
    trusted_worker_config = dict(trusted_worker_config or {})
    if any(not k.startswith("mcp_servers.quadratus.") for k in trusted_worker_config):
        raise ValueError("Only the separately held bounded Quadratus bridge may be declared trusted")
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            body.extend(argv[i:])
            break
        if arg in {"--model", "-m"}:
            if i + 1 == len(argv) or not argv[i + 1] or argv[i + 1].startswith("-"):
                raise ValueError("Missing held model value")
            body.extend(argv[i:i + 2])
            i += 2
            continue
        if arg in {"-C", "--cd"}:
            if i + 1 == len(argv) or Path(argv[i + 1]).resolve() != root:
                raise ValueError("CLI working directory differs from the held workspace")
            body.extend(argv[i:i + 2])
            i += 2
            continue
        if arg == "--enable" or arg.startswith("--enable=") or arg.startswith("--dangerously-"):
            raise ValueError("Feature enablement would reopen a removed surface")
        if arg == "--disable":
            if i + 1 == len(argv) or argv[i + 1] not in {"multi_agent", "multi_agent_v2"}:
                raise ValueError("Only unchanged mandatory native fanout denials are accepted")
            body.extend(argv[i:i + 2])
            i += 2
            continue
        if arg in {"--sandbox", "-s"}:
            if mode is not None or i + 1 == len(argv):
                raise ValueError("Exactly one known sandbox mode required")
            mode = argv[i + 1]
            i += 2
            continue
        if arg in {"--permission-profile", "-P", "--profile", "-p", "--add-dir",
                   "--full-auto", "--yolo", "--approve-for-me",
                   "--dangerously-bypass-approvals-and-sandbox"}:
            raise ValueError("Conflicting boundary or profile option")
        if arg in {"-c", "--config"}:
            if i + 1 == len(argv):
                raise ValueError("Missing config value")
            value = argv[i + 1]
            key, _, setting = value.partition("=")
            key = ".".join(part.strip().strip('"\'').strip() for part in key.split("."))
            if key.startswith("sandbox_workspace_write."):
                if key.endswith(".writable_roots") and setting == "[]":
                    i += 2
                    continue
                for suffix in ("exclude_slash_tmp", "exclude_tmpdir_env_var"):
                    if key == "sandbox_workspace_write." + suffix and setting == "true":
                        excludes[suffix] = True
                        break
                else:
                    raise ValueError("Unqualified workspace boundary override")
                i += 2
                continue
            if key in {"sandbox_mode", "sandbox", "permissions", "default_permissions"} or key.startswith("permissions."):
                raise ValueError("Conflicting boundary config")
            if key in trusted_worker_config and setting == trusted_worker_config[key]:
                body.extend((arg, value))
                i += 2
                continue
            if any(key == prefix or key.startswith(prefix + ".") for prefix in
                   ("mcp_servers", "plugins", "hooks", "notify", "features", "profile", "profiles")):
                raise ValueError("Config override would restore an ambient reader or selected profile")
            if key in {"model_instructions_file", "experimental_instructions_file"} or any(
                    key == prefix or key.startswith(prefix + ".") for prefix in
                    ("projects", "model_providers", "shell_environment_policy", "skills", "desktop", "marketplaces")):
                raise ValueError("Unreviewed native-parent reader or environment override")
            if (key == "agents" or key.startswith("agents.")) and (key != "agents.enabled" or setting != "false"):
                raise ValueError("Native fanout override")
            if key not in {"model_reasoning_effort", "agents.enabled"}:
                raise ValueError("Only original role effort, fanout denial or exact held bridge config is accepted")
            body.extend((arg, value))
            i += 2
            continue
        if arg.startswith(("--sandbox=", "--permission-profile=", "--profile=",
                           "--add-dir=", "--config=", "--cd=", "--disable=", "-c=")) or (
                arg.startswith(("-c", "-s", "-P", "-p", "-C")) and not arg.startswith("--")):
            raise ValueError("Unqualified attached boundary/config option")
        if arg not in {"exec", "sandbox", "--json", "--skip-git-repo-check", "-"}:
            raise ValueError("Only the held worker option surface is supported")
        if arg in {"exec", "sandbox"} and i != 1:
            raise ValueError("Unexpected subcommand")
        body.append(arg)
        i += 1
    if mode not in {"read-only", "workspace-write"}:
        raise ValueError("Existing restrictive sandbox mode required")
    name = "quadratus-step1-" + mode
    prefix = "permissions." + name
    inherit = ":read-only" if mode == "read-only" else ":workspace"
    filesystem = {":root": "read"}
    if excludes.get("exclude_slash_tmp"):
        filesystem[":slash_tmp"] = "read"
    if excludes.get("exclude_tmpdir_env_var"):
        scratch = Path(tempdir or os.environ.get("TMPDIR", "/tmp")).resolve()
        filesystem[":tmpdir"] = "write" if scratch.is_relative_to(root) else "read"
    for path in protected:
        candidate = Path(path)
        if not candidate.is_absolute() or candidate.resolve() != candidate:
            raise ValueError("Protected roots must be canonical absolute paths")
        if candidate == root or candidate.is_relative_to(root):
            raise ValueError("Protected roots cannot be inside the workspace")
        filesystem[str(candidate)] = "deny"
    for path in readonly_runtime:
        candidate = Path(path)
        if not candidate.is_absolute() or candidate.resolve(strict=True) != candidate:
            raise ValueError("Runtime roots must exist and be canonical")
        if any(candidate == Path(p) or Path(p).is_relative_to(candidate) for p in protected):
            raise ValueError("Runtime exception would reopen protected evidence")
        filesystem[str(candidate)] = "read"
    # exec on 0.154 has no -P option. default_permissions is the documented
    # selection route. A caller must refuse legacy sandbox_mode in loaded
    # configs, which otherwise takes precedence over this selection.
    profile_args = ["-c", "default_permissions=" + json.dumps(name)]
    # Quoted dotted CLI keys are not TOML-parsed on 0.154; use an inline
    # table value so absolute paths, dots and spaces stay literal map keys.
    fields = ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in filesystem.items())
    table = "{extends=" + json.dumps(inherit) + ",filesystem={" + fields + "}}"
    profile_args.extend(("-c", prefix + "=" + table))
    if body and body[0] == "exec":
        # The installed 0.154 exec parser drops root -c flags when exec also
        # carries -c. Keep the selection and definition in the exec scope.
        # Actual empty-stdin configuration probes pin this without generation.
        return [*result, "exec", *profile_args, *body[1:]]
    if body and body[0] == "sandbox":
        body[1:1] = ["--permission-profile", name]
    return [*result, *profile_args, *body]
