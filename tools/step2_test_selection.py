"""Opt-in complete file operands for the fixed Step 2 pytest check.

This operator helper changes selection evidence, not NOT RUN policy. The
marker belongs to operator configuration, never model-selected commands.
Only pytest default discovery in one project-local directory is supported.
"""
import os
from contextlib import contextmanager
from dataclasses import replace
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from quadratus.integration import GateSuite as BaseGateSuite
from quadratus.integration import model_facing as base_model_facing

MARKER = "@all-pytest-files:"
# Pinned pytest default discovery. Custom selection is refused, not guessed.
IGNORE_DIRS = (".*", "*.egg", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "__pycache__")
CONFIGS = ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")


def whole_files(project, cwd, directory):
    project = Path(project).resolve(strict=True)
    relative = PurePosixPath(directory)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("test directory must be project-relative")
    base = project / cwd
    if not base.resolve(strict=True).is_relative_to(project):
        raise ValueError("check cwd escapes project")
    target = base / directory
    for path in [base, target, *target.parents]:
        if path == project.parent:
            break
        if path.is_symlink():
            raise ValueError("test directory symlink refused")
    if not target.is_dir() or not target.resolve(strict=True).is_relative_to(project):
        raise ValueError("test directory unavailable or outside project")
    if any((project / name).exists() for name in CONFIGS):
        raise ValueError("custom pytest discovery configuration is unsupported")
    files = []
    for parent, dirs, names in os.walk(target, followlinks=False):
        parent = Path(parent)
        for name in dirs:
            if (parent / name).is_symlink():
                raise ValueError("test directory symlink refused")
        dirs[:] = sorted(name for name in dirs if not any(fnmatch(name, pattern) for pattern in IGNORE_DIRS))
        if any(name in CONFIGS for name in names):
            raise ValueError("custom pytest discovery configuration is unsupported")
        if "conftest.py" in names:
            raise ValueError("custom pytest collection hooks are unsupported")
        for name in sorted(names):
            if not (fnmatch(name, "test_*.py") or fnmatch(name, "*_test.py")):
                continue
            path = parent / name
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(project):
                raise ValueError("test file symlink or escape refused")
            files.append(str(path.relative_to(base)))
    if not files:
        raise ValueError("no pytest files selected; no partial or empty success")
    return sorted(files)


def render(command, project):
    argv = list(command.argv)
    tokens = [arg for arg in argv if arg.startswith(MARKER)]
    if not tokens:
        return command
    if len(tokens) != 1 or len(argv) < 4 or argv[1:3] != ["-m", "pytest"]:
        raise ValueError("whole-file marker requires one Python pytest command")
    allowed = {"-q", "-v", tokens[0], "--quadratus-report={report}"}
    if any(arg not in allowed for arg in argv[3:]):
        raise ValueError("filtered or custom pytest selection is unsupported")
    files = whole_files(project, command.cwd, tokens[0][len(MARKER):])
    result = []
    for arg in argv:
        if arg == "-q":
            continue
        result.extend(files if arg == tokens[0] else [arg])
    if "-v" not in result:
        result.insert(3, "-v")
    return replace(command, argv=tuple(result))


def model_facing(argv):
    """Workers run ordinary discovery; only the trusted runner expands files.

    This preserves the same population without leaking an operator marker
    into the exact command workers are told and granted permission to run.
    """
    return base_model_facing([arg[len(MARKER):] if arg.startswith(MARKER) else arg for arg in argv])


class WholeFileGateSuite(BaseGateSuite):
    """Logical marker stays stable across checkpoints; each executed receipt
    records the actual complete current file operands. Source changes and
    runner identity still use BaseGateSuite's ordinary checks and nonce.
    Helper bytes and logical argv are held in the once-only job manifest.
    """
    def cheap(self):
        view = WholeFileGateSuite([c for c in self.commands if c.cheap], cwd=self.cwd,
                                  exclude=self.project.exclude)
        view._cache = self._cache
        return view

    def run(self):
        if not any(any(a.startswith(MARKER) for a in c.argv) for c in self.commands):
            return super().run()
        before = self.project.fingerprint()
        commands = tuple(render(c, self.cwd) if not c.skip_reason else c for c in self.commands)
        if self.project.fingerprint() != before:
            raise ValueError("source changed during test selection")
        view = BaseGateSuite(commands, cwd=self.cwd, exclude=self.project.exclude)
        view._cache = self._cache
        return view.run()


@contextmanager
def installed():
    """Explicit operator opt-in for one worker episode; restore on every exit.
    Ordinary suites and all commands without the marker retain their bytes.
    No model tools, native argv, permissions or environment are changed.
    """
    import quadratus.integration as integration
    import quadratus.project_run as project_run
    previous = integration.GateSuite, project_run.GateSuite, integration.model_facing
    if previous != (BaseGateSuite, BaseGateSuite, base_model_facing):
        raise RuntimeError("nested or unexpected selector installation")
    integration.GateSuite = project_run.GateSuite = WholeFileGateSuite
    integration.model_facing = model_facing
    try:
        yield
    finally:
        integration.GateSuite, project_run.GateSuite, integration.model_facing = previous
