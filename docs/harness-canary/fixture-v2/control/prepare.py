"""Prepare a fresh solver tree without the external grader or reference repair."""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and ".quadratus" not in p.parts}


def prepare(target):
    target = Path(target).resolve()
    if target.exists():
        raise ValueError("Use a new directory; existing trials are never overwritten")
    shutil.copytree(FIXTURE / "project", target,
                    ignore=shutil.ignore_patterns("__pycache__", ".quadratus"))
    policy = json.loads((FIXTURE.parent / "policy.json").read_text())
    policy["repository_id"] = "fixture/q9-two-task-v2"
    policy["defaults"]["scope_max_lines"] = 60
    policy["defaults"]["family"] = "pure-logic"
    policy["capability_policy"]["deny_write"] += ["auth.py", "catalog.py"]
    policy["path_rules"] = [
        {"paths": ["presentation.py"], "families": ["pure-logic"]},
        {"paths": ["app.py", "access.py"], "families": ["scoped-endpoint"],
         "overlays": ["tenant-isolation"]},
    ]
    # The same common gate runs on both engines. Task-specific correctness is
    # measured by the external grader, including after controller completion.
    gate = policy["gates"][1]
    gate["argv"] = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    str(HERE / "test_contract.py"), "-k", "preservation"]
    gate["minimum_tests"] = 5
    for name in ("cross-tenant-test", "unit-tests"):
        policy["gate_bindings"][name] = {
            "absent": True,
            "because": "Task correctness is graded externally after both ordered tasks; "
                       "the common per-task gate checks preservation only.",
        }
    state = target / ".quadratus"
    state.mkdir()
    (state / "policy.json").write_text(json.dumps(policy, indent=2) + "\n")
    manifest = {"project": str(target), "source_sha256": hashes(target),
                "instrument_sha256": hashes(HERE),
                "policy_sha256": hashlib.sha256((state / "policy.json").read_bytes()).hexdigest(),
                "allowed_paths": ["presentation.py", "app.py", "access.py"],
                "max_tasks": 2, "live_authorized": False}
    (state / "fixture-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    print(json.dumps(prepare(parser.parse_args().target), indent=2))
