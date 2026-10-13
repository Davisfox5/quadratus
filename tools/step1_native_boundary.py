"""Development-only Codex boundary; no provider calls or global config writes.

Use one native permission profile instead of nesting macOS sandboxes. Other
vendors and scored benchmark adapters are deliberately outside this helper.
"""

from __future__ import annotations

import json
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


def protected_codex_argv(argv: Sequence[str], *, protected: Sequence[str],
                         cwd: str, tempdir: str | None = None) -> list[str]:
    """Narrow existing read-only/workspace-write commands without adding grants.

    The caller must separately review the trusted native parent and qualify
    actual model tools. Shell-only proof is insufficient for scored admission.
    """
    if not argv or not protected:
        raise ValueError("Exact client argv and protected roots required")
    root = Path(cwd).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace must be a directory")
    result = [argv[0]]
    body: list[str] = []
    mode = None
    excludes: dict[str, bool] = {}
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            body.extend(argv[i:])
            break
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
            if key == "sandbox_mode" or key.startswith("permissions.") or key == "default_permissions":
                raise ValueError("Conflicting boundary config")
            body.extend((arg, value))
            i += 2
            continue
        if arg.startswith(("--sandbox=", "--permission-profile=", "--profile=",
                           "--add-dir=", "--config=", "-c=")) or (
                arg.startswith(("-c", "-s", "-P", "-p")) and not arg.startswith("--")):
            raise ValueError("Unqualified attached boundary/config option")
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
    # exec on 0.154 has no -P option. default_permissions is the documented
    # selection route. A caller must refuse legacy sandbox_mode in loaded
    # configs, which otherwise takes precedence over this selection.
    result.extend(("-c", "default_permissions=" + json.dumps(name)))
    # Quoted dotted CLI keys are not TOML-parsed on 0.154; use an inline
    # table value so absolute paths, dots and spaces stay literal map keys.
    fields = ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in filesystem.items())
    table = "{extends=" + json.dumps(inherit) + ",filesystem={" + fields + "}}"
    result.extend(("-c", prefix + "=" + table))
    if body and body[0] == "sandbox":
        body[1:1] = ["--permission-profile", name]
    return [*result, *body]
