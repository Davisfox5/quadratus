"""Prove each task is independently necessary, without invoking any model."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from prepare import HERE, hashes, prepare
from reference import repair


def main():
    records = []
    with tempfile.TemporaryDirectory(prefix="quadratus-fixture-v2-controls-") as directory:
        for name, lookup, security in [
            ("broken", False, False), ("lookup-only", True, False),
            ("security-only", False, True), ("reference", True, True),
        ]:
            project = Path(directory) / name
            prepare(project)
            before = hashes(project)
            repair(project, lookup=lookup, security=security)
            after = hashes(project)
            changed = sorted(k for k in before if before[k] != after[k])
            expected = (["presentation.py"] if lookup else []) + (
                ["access.py", "app.py"] if security else [])
            assert changed == sorted(expected), (name, changed)
            env = {**os.environ, "CANARY_PROJECT": str(project), "PYTHONDONTWRITEBYTECODE": "1"}
            for selection in ("preservation", "lookup", "security", ""):
                command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           str(HERE / "test_contract.py")]
                if selection:
                    command += ["-k", selection]
                run = subprocess.run(command, cwd=project, env=env, capture_output=True,
                                     text=True, timeout=30)
                should_pass = (selection == "preservation" or
                               selection == "lookup" and lookup or
                               selection == "security" and security or
                               selection == "" and lookup and security)
                assert run.returncode == (0 if should_pass else 1), (name, selection, run.stdout, run.stderr)
                records.append({"control": name, "selection": selection or "all",
                                "changed_paths": changed, "exit_code": run.returncode,
                                "tail": run.stdout.strip().splitlines()[-1]})
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
