"""Check an installed Quadratus wheel from outside the source tree.

CI builds the wheel, installs it into a fresh virtual environment and runs
this file with that environment's interpreter, from a folder that is not the
checkout. The test suite imports the checkout, so it cannot show any of:

* the package that imports is the installed one;
* every package-data pattern in ``pyproject.toml`` matched files, and each of
  those files is in the installed package with the same bytes;
* the console commands ``pyproject.toml`` declares exist, load, and start:
  ``quadratus --help`` prints usage, and ``quadratus-gui --help`` exits
  without serving.

Run it isolated so the environment cannot put the checkout back on the path::

    /path/to/venv/bin/python -I tools/wheel_smoke.py --source .

``--site DIR`` checks a ``pip install --target DIR`` tree instead of the
running interpreter's own packages, and ``--commands skip`` leaves the
console commands alone; the offline regression test uses both.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: ``quadratus-gui --help`` printed usage and exited 0.
GUI_HELP = "help"
#: The command has no ``--help`` yet: it read the port, found it busy and
#: exited 2 without serving. The entry point still loaded and returned.
GUI_NO_HELP = "no-help"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pyproject(source: Path) -> dict:
    with open(source / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)


def expected_package_data(source: Path) -> Tuple[Dict[str, List[str]], List[str]]:
    """The files each package-data pattern names in the checkout.

    Returns ``({package: [relative paths]}, problems)``. A pattern that
    matches nothing is a problem: it promises data the wheel cannot hold.
    """
    patterns = _pyproject(source).get("tool", {}).get("setuptools", {}).get("package-data", {})
    expected: Dict[str, List[str]] = {}
    problems: List[str] = []
    if not patterns:
        problems.append("pyproject.toml lists no [tool.setuptools.package-data]")
    for package, globs in patterns.items():
        base = source / package.replace(".", "/")
        found: List[str] = []
        for pattern in globs:
            matched = sorted(p.relative_to(base).as_posix() for p in base.glob(pattern) if p.is_file())
            if not matched:
                problems.append(f"package-data pattern {package}:{pattern} matches no file in the checkout")
            found.extend(matched)
        expected[package] = sorted(set(found))
    return expected, problems


def check_package_data(source: Path, installed: Dict[str, Path]) -> Tuple[int, List[str]]:
    """Every expected data file is installed with the checkout's bytes."""
    expected, problems = expected_package_data(source)
    checked = 0
    for package, files in expected.items():
        root = installed.get(package)
        if root is None:
            problems.append(f"package {package} is not installed")
            continue
        base = source / package.replace(".", "/")
        for rel in files:
            target = root / rel
            if not target.is_file():
                problems.append(f"missing from the installed {package}: {rel}")
            elif _sha256(target) != _sha256(base / rel):
                problems.append(f"installed {package}/{rel} differs from the checkout")
            else:
                checked += 1
    return checked, problems


def installed_packages(source: Path, names) -> Tuple[Dict[str, Path], List[str]]:
    """Where each package imports from; the checkout is never an answer."""
    import importlib

    found: Dict[str, Path] = {}
    problems: List[str] = []
    checkout = source.resolve()
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 -- reported, never raised: this is the check
            problems.append(f"{name} does not import: {type(exc).__name__}: {exc}")
            continue
        root = Path(module.__file__).resolve().parent
        if root == checkout or checkout in root.parents:
            problems.append(f"{name} imported from the checkout ({root}), not from the installed wheel")
            continue
        found[name] = root
    return found, problems


def check_entry_points(source: Path) -> Tuple[List[str], List[str]]:
    """Each ``[project.scripts]`` command is installed and its target loads."""
    from importlib.metadata import PackageNotFoundError, distribution

    project = _pyproject(source).get("project", {})
    declared = project.get("scripts", {})
    problems: List[str] = []
    if not declared:
        return [], ["pyproject.toml declares no [project.scripts]"]
    try:
        dist = distribution(project["name"])
    except PackageNotFoundError:
        return [], [f"distribution {project.get('name')!r} is not installed"]
    scripts = {ep.name: ep for ep in dist.entry_points if ep.group == "console_scripts"}
    loaded: List[str] = []
    for name, target in declared.items():
        entry = scripts.get(name)
        if entry is None:
            problems.append(f"console command {name} is not in the installed metadata")
        elif entry.value != target:
            problems.append(f"console command {name} points at {entry.value}, pyproject says {target}")
        else:
            try:
                if not callable(entry.load()):
                    problems.append(f"console command {name}: {target} is not callable")
                else:
                    loaded.append(name)
            except Exception as exc:  # noqa: BLE001 -- reported, never raised
                problems.append(f"console command {name} does not load: {type(exc).__name__}: {exc}")
    return loaded, problems


def classify_gui_help(returncode: Optional[int], stdout: str, stderr: str) -> Optional[str]:
    """Read one ``quadratus-gui --help`` run made with its port held busy.

    ``GUI_HELP`` when it printed usage and exited 0. ``GUI_NO_HELP`` when it
    ignored ``--help``, found the port busy and exited 2 as its own message
    says. Anything else, a timeout included (``returncode`` None), is None.
    """
    if returncode == 0 and "usage" in (stdout or "").lower():
        return GUI_HELP
    if returncode == 2 and "already in use" in (stderr or ""):
        return GUI_NO_HELP
    return None


def probe_gui_help(command: List[str], *, timeout: float = 180.0, env: Optional[dict] = None,
                   cwd: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Run ``command --help`` so that it can never start serving.

    A loopback port is held open for the whole call and named in
    ``QUADRATUS_GUI_PORT``. A command with ``--help`` exits before it looks
    at the port; one without it finds the port busy and exits 2. Neither
    binds a server, so the probe is safe where nothing may be left running.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        child_env = dict(os.environ if env is None else env)
        child_env["QUADRATUS_GUI_PORT"] = str(held.getsockname()[1])
        try:
            done = subprocess.run([*command, "--help"], capture_output=True, text=True,
                                  timeout=timeout, env=child_env, cwd=cwd)
        except subprocess.TimeoutExpired:
            return None, f"no exit within {int(timeout)} s"
        outcome = classify_gui_help(done.returncode, done.stdout, done.stderr)
        detail = f"exit {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]}"
        return outcome, detail


def check_commands(bin_dir: Path, names, *, gui_help: str, cwd: str) -> Tuple[Dict[str, str], List[str]]:
    """Start each installed command far enough to prove it is wired up."""
    results: Dict[str, str] = {}
    problems: List[str] = []
    for name in names:
        command = bin_dir / name
        if not command.is_file():
            problems.append(f"{command} is not installed")
            continue
        if name.endswith("-gui"):
            outcome, detail = probe_gui_help([str(command)], cwd=cwd)
            if outcome is None or (outcome == GUI_NO_HELP and gui_help == "required"):
                problems.append(f"{name} --help did not print usage and exit 0 ({detail})")
            results[name] = outcome or "failed"
            continue
        try:
            done = subprocess.run([str(command), "--help"], capture_output=True, text=True,
                                  timeout=180, cwd=cwd)
        except subprocess.TimeoutExpired:
            problems.append(f"{name} --help did not exit within 180 s")
            results[name] = "failed"
            continue
        if done.returncode != 0 or "usage" not in done.stdout.lower():
            problems.append(f"{name} --help exited {done.returncode} without usage: "
                            f"{(done.stderr or done.stdout).strip()[-300:]}")
            results[name] = "failed"
        else:
            results[name] = GUI_HELP
    return results, problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, type=Path,
                        help="The checkout: its pyproject.toml and package folders say what to expect.")
    parser.add_argument("--site", type=Path, default=None,
                        help="A pip --target directory to check instead of this interpreter's own packages.")
    parser.add_argument("--commands", choices=("run", "skip"), default="run",
                        help="Whether to start the console commands beside this interpreter.")
    parser.add_argument("--gui-help", choices=("required", "if-supported"), default="required",
                        help="'if-supported' also accepts a quadratus-gui that has no --help yet, "
                             "as long as it exits without serving.")
    args = parser.parse_args(argv)

    source = args.source.resolve()
    if args.site is not None:
        sys.path.insert(0, str(args.site.resolve()))
    project = _pyproject(source)
    layout = project.get("tool", {}).get("setuptools", {})
    # The top-level modules are thin wrappers; they must come from the wheel too.
    installed, problems = installed_packages(
        source, [*layout.get("packages", []), *layout.get("py-modules", [])])
    checked, data_problems = check_package_data(source, installed)
    problems += data_problems
    loaded, entry_problems = check_entry_points(source)
    problems += entry_problems
    commands: Dict[str, str] = {}
    if args.commands == "run":
        with tempfile.TemporaryDirectory(prefix="quadratus-smoke-") as scratch:
            commands, command_problems = check_commands(
                Path(sys.executable).parent, list(project.get("project", {}).get("scripts", {})),
                gui_help=args.gui_help, cwd=scratch)
        problems += command_problems

    print(json.dumps({"installed": {k: str(v) for k, v in installed.items()},
                      "package_data_files": checked, "entry_points": loaded,
                      "commands": commands, "problems": problems}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
