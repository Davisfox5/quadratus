"""What an editing call changed outside the measured source.

``Project.files()`` is the measured source set: scope, the gate's source
hash, review copies and evidence identity all read it. It skips ``.git``,
``.quadratus``, build output, tool caches, every symlink and every secret,
so the measured diff says nothing about a write there. Whole-repo review,
2026-11-10: an editing call wrote a git hook, a ``build/`` shim an
application module imported, ``.env.local`` and a symlink to the home
folder, and the diff listed none of them. The built-in write denials for
``.git/**``, ``.env*`` and ``.quadratus/**`` could not be enforced by a diff
that never contains those paths.

This is the second listing. Fleet takes it before and after every editing
call, beside the source diff, and compares the two.

Sensitive class (content hashed; any add, change, retarget or delete by an
editing call fails the task as a scope violation, and nothing is reverted):

* in a ``.git`` directory at any depth: ``config``, ``config.worktree``,
  everything under ``hooks/`` and ``info/`` (but ``info/refs``, which git
  itself rewrites on every gc and repack), and the same inside
  ``modules/``; a ``.git`` file (a worktree or submodule pointer);
* every ``.env*`` file but ``.env.example``, the credential files and the
  vendor agent configuration (``project.withheld``, names compared without
  case), and a link named ``.claude``, which can lead to settings anywhere;
* ``.quadratus/policy.json`` and ``.quadratus/source-excludes.json``, and
  the two records the harness keeps across runs and writes only between
  calls, ``.quadratus/codebase-map.jsonl`` (rendered into every later
  orchestrator and lead prompt) and ``.quadratus/scoreboard.jsonl``. The
  same four names are read in the run's state folder when the operator put
  it somewhere else inside the project (``--state-dir``): that folder is
  the one path a run excludes from its source, so an excluded folder is
  read as harness state;
* every symlink in a walked directory, recorded by its target;
* a Python file under a report or cache folder (the build-output names
  below other than ``project.PYTHON_OUTPUT_NAMES``): no tool writes one
  there, and pytest collects a ``conftest.py`` from ``test-results/``;
* what a test runner or an interpreter loads because of where it sits
  (``LOADED_NAMES``, ``LOADED_SUFFIXES``, ``LOADED_PREFIXES`` and everything
  under a ``__mocks__`` folder), in the places no other listing reads:
  harness state, the tool caches (``__pycache__``, ``.pytest_cache``,
  ``.ruff_cache``, ``.coverage``, ``.DS_Store``), the whole of a ``.git``
  directory, and a stray ``.quadratus`` or ``.multi_llm`` folder. Final
  review, 2026-11-11: with only ``.quadratus/__mocks__/mylib.js`` written,
  a failing jest check passed, and a ``conftest.py`` in ``.pytest_cache/``
  or ``.git/`` did the same to pytest under a project config that collects
  dot folders; no listing showed either. Inside ``dist`` and ``build``,
  where a real build writes Python and JavaScript, only the part of that
  class no build has a reason to write is held to it
  (``BUILD_LOADED_NAMES``, ``.pth`` files and ``__mocks__``); in a report
  or cache folder, Python and ``.pth`` files and ``__mocks__``; in a
  ``.tox`` or ``.nox`` environment, which a tool fills with installed
  packages and their own ``conftest.py`` and ``.pth`` files, ``__mocks__``
  alone (no installed package brings one, and jest's default configuration
  loads a manual mock from there as from anywhere). In the
  folders walked by name only, a link counts when it is so named or leads
  to a folder, which pytest collects through; a link to a file under any
  other name loads nothing, and the evidence check has its own rule for a
  linked screenshot;
* under a state folder's ``runs``: every run folder (by presence) and its
  ``result.json`` (by lstat signature). The next run's preflight reads
  those records, so an editing call that adds, removes or rewrites one is
  changing what that preflight will be told. The harness writes a run's
  own ``result.json`` once, after its last call;
* in a run folder that has its ``result.json`` (a run that ended): the
  state a resume and the monitor read (``RUN_STATE``: the manifest, the
  checkpoint and budget exports, the resume marker, the lineage and the
  diff, each by lstat signature), and in its ``records`` and ``artifacts``
  folders every link and everything else that is not a regular file. A
  record's bytes are held by the manifest's digest of it and an artifact's
  by its id, so regular files there are not read. The harness writes none
  of this once the
  run's own record is written, so an editing call that plants or rewrites
  one is changing what a later resume would carry as the job's record. A
  run still going has no ``result.json`` and is left to its own store,
  which refuses a manifest it did not write (``jobstore.JobStore``). A
  folder with no ``result.json`` that this process did not make, beside one
  it did, is a run whose process died, and is listed like an ended one.

A sensitive change that is still in place stops the run: Fleet makes no
further call for the project but the orchestrator's until the path is back
as it was (:class:`SensitiveChangeInPlace`). The file is invisible to the
measured diff and to every review copy, so a check that passes on top of it
proves nothing about the source.

The finding outlives the run. Fleet keeps what each changed path held after
the call (``watch_records``), the run's ``result.json`` records it, and the
next run on the project is refused before any model call while a recorded
path still holds that content (:func:`still_in_place`,
``project_run._refuse_standing_watch_findings``): started again without
that, the new run took the planted file as its baseline and it worked
again. The operator removes or restores the path, or accepts it
(``accept_watched_paths``, ``QUADRATUS_ACCEPT_WATCHED_PATHS=1``). An
acceptance lasts: it is written to the accepting run's record, and later
runs read it there and do not ask about the same path again. The
map and the scoreboard, which the harness itself appends to, stand until
they are gone or back as they were before the call, whatever was written
to them since.

Build-output class (lstat signature only; a change is an observation on the
call's record, never a failure, because a lead may legitimately run a
build): everything under a folder named in ``project.OUTPUT_NAMES``, a file
so named, and a ``.quadratus`` or ``.multi_llm`` folder or file that is not
the project's own state.

Not recorded: the content of git internals a plain ``git status`` rewrites
(the index, ``HEAD``, refs, objects), of the harness's own state (run
records, the activity log, artifacts, capture evidence, fixtures) and of
the tool caches every test run rewrites. Those folders are still walked,
by name only, for the loaded-by-position class above; the harness itself
writes no file so named, so it never trips its own watch. Not watched at
all: dependency trees, which :mod:`quadratus.deptree` identifies; a
``.tox`` or ``.nox`` environment beyond the build-output signature and its
``__mocks__`` folders (it is a dependency tree nobody identifies yet, so a
``conftest.py`` or ``.pth`` file written into one is an observation);
compiled ``.pyc`` files, which no check reads (``integration`` runs every
check with a fresh ``PYTHONPYCACHEPREFIX``); a test file of another
language (``x.test.js``) in a folder walked by name only, which adds cases
to a default jest run and replaces none;
anything outside the project, which includes the real git directory of a
linked worktree.

Bounds are finite and fail closed, as in deptree: entries seen, bytes
hashed and wall time, counted while walking. An overrun or a folder that
cannot be listed is :class:`WatchUnavailable`, so no editing call runs
unwatched. A watched file that cannot be opened is recorded by its lstat
signature instead, which still shows a rewrite. An entry that vanishes
between being listed and being read is simply not there: a leftover process
deleting its own build output must not stop a run.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Tuple

from .deptree import DEPENDENCY_DIRS
from .project import (
    AGENT_CONFIG_DIRS,
    AGENT_CONFIG_NAMES,
    AGENT_CONFIG_PARENTS,
    AGENT_CONFIG_TAILS,
    CREDENTIAL_NAMES,
    CREDENTIAL_TAILS,
    OUTPUT_NAMES,
    PYTHON_OUTPUT_NAMES,
    withheld,
)

__all__ = ["ProjectWatch", "WatchListing", "WatchUnavailable", "AgentConfigRefused",
           "ACCEPT_WATCHED_PATHS_ENV", "WATCH_RECORD_LIMIT", "still_in_place", "watched_paths_accepted",
           "SensitiveChangeInPlace", "SENSITIVE_RULE", "ALLOW_AGENT_CONFIG_ENV", "agent_config", "agent_config_permitted",
           "describe", "loaded_by_position"]

MAX_ENTRIES = 250_000
MAX_BYTES = 64 * 1024 * 1024
DEADLINE_SECONDS = 60.0
MAX_DEPTH = 256
#: Names kept on one call's record; the count is kept whole.
UNMEASURED_LIMIT = 50
#: Sensitive changes kept per finding on the Fleet's record, with the count.
WATCH_FINDING_LIMIT = 50
_CHUNK = 1 << 20
_NAMED = 8

#: Changed paths whose content is kept for the next run's preflight.
WATCH_RECORD_LIMIT = 200

#: The harness's own files under the project's ``.quadratus`` that decide
#: what a later run trusts, measures or is told. The codebase map and the
#: scoreboard are written by the harness between calls and never while an
#: editing call is in flight: the map is amended from an orchestrator's
#: reply and from a close-out's, both after their call returned, and a
#: parallel child's close-out writes the run's map while its siblings edit
#: copies of the project, whose own ``.quadratus`` is not the run's. The
#: scoreboard is appended once, when a run ends, after its last call
#: (``project_run``). So an editing call that writes
#: either wrote what every later prompt would be handed as codebase
#: knowledge, and fails its task like any other watched path.
STATE_FILES = ("policy.json", "source-excludes.json", "codebase-map.jsonl", "scoreboard.jsonl")
#: Why each is watched, for the listing's ``kinds``.
_STATE_KINDS = {"codebase-map.jsonl": "memory", "scoreboard.jsonl": "memory"}
#: Caches every test run rewrites. Not source; walked by name only (``_unlisted``).
_CACHES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".coverage", ".DS_Store"})
#: What a test runner or an interpreter loads because of where it sits, by
#: lowercased name: pytest's conftest and any module it can collect or
#: import, the interpreter's start-up files, and the files jest, vitest and
#: pytest take their configuration (or a package's identity) from.
LOADED_NAMES = frozenset({"conftest.py", "sitecustomize.py", "usercustomize.py", "package.json",
                          "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"})
LOADED_SUFFIXES = (".py", ".pth")
LOADED_PREFIXES = ("jest.config.", "vitest.config.")
#: A folder whose files a JavaScript runner loads in place of the modules
#: they are named after. Everything below one is in the class.
MOCKS_DIR = "__mocks__"
#: The part of the class no build writes, held to it inside a build folder.
BUILD_LOADED_NAMES = frozenset({"conftest.py", "sitecustomize.py", "usercustomize.py"})
BUILD_LOADED_SUFFIXES = (".pth",)
#: Environments a tool fills with installed packages: dependency trees in
#: all but name, and still only signed as build output.
_ENVIRONMENTS = frozenset({".tox", ".nox"})
#: Build folders, where a real build writes Python and JavaScript.
_BUILD = PYTHON_OUTPUT_NAMES - _ENVIRONMENTS
#: Under a state folder: the run records the next run's preflight reads.
RUNS_DIR = "runs"
RUN_RECORD = "result.json"
#: In a run folder whose record is written: the files a resume or the
#: monitor reads, and the folders whose entries they read.
RUN_STATE = ("manifest.json", "checkpoint.json", "budget.json", "resumed-by.json", "lineage.json",
             "changes.diff")
RUN_STATE_DIRS = ("records", "artifacts")
#: Run folders this process made (``made_here``). Where it made one, every
#: other folder beside it with no ``result.json`` is a run whose process
#: died: nothing writes there any more, so it is listed like an ended run.
_MADE_HERE: set = set()
_MADE_IN: set = set()


def made_here(run_dir) -> None:
    """Note a run folder this process just made and will write to."""
    path = os.path.realpath(run_dir)
    _MADE_HERE.add(path)
    _MADE_IN.add(os.path.dirname(path))
#: Report and cache folders: output no tool fills with Python.
_PYTHON_FREE = OUTPUT_NAMES - PYTHON_OUTPUT_NAMES
#: Folders named like harness state that are not the project's own state.
_STRAY_STATE = frozenset({".quadratus", ".multi_llm"})
_GIT_FILES = frozenset({"config", "config.worktree"})
_GIT_TREES = frozenset({"hooks", "info"})
#: Git's own housekeeping inside a watched tree, by tree: ``info/refs`` is
#: rewritten by every gc and repack (repack.updateServerInfo), which an
#: editor's background fetch can start in the middle of an editing call, and
#: no local command reads it. ``info/exclude``, ``info/attributes`` and the
#: rest stay watched.
_GIT_HOUSEKEEPING = {"info": frozenset({"refs"})}
#: Git's ledgers. A branch can be named ``fix-setup.py``, so a file below
#: one that holds what git itself writes there is not code (``_git_ledger``).
_GIT_LEDGERS = frozenset({"refs", "logs"})
_REF = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\n?")
_REFLOG = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64}) (?:[0-9a-f]{40}|[0-9a-f]{64}) ")
#: Git's own storage inside a module's git directory: large, and rewritten
#: by ordinary git commands.
_GIT_INTERNALS = frozenset({"objects", "refs", "logs", "branches", "worktrees", "lfs", "rr-cache"})

#: The rule an editing call is held to, stated to it from the same tables the
#: listing reads (tests pin the two together).
SENSITIVE_RULE = (
    "Do not add, change, retarget or delete any of these, which the harness lists before and "
    "after this call: symbolic links; .env files other than .env.example; .git hooks, config or "
    "info; credential files ("
    + ", ".join(sorted(CREDENTIAL_NAMES) + ["/".join(t) for t in CREDENTIAL_TAILS])
    + "); agent configuration ("
    + ", ".join(["/".join(t) for t in AGENT_CONFIG_TAILS] + sorted(AGENT_CONFIG_NAMES)
                + [d + "/" for d in sorted(AGENT_CONFIG_DIRS)])
    + "); " + ", ".join(".quadratus/" + name for name in STATE_FILES)
    + "; Python files inside a report or cache folder ("
    + ", ".join(sorted(_PYTHON_FREE))
    + "); another run's folder or " + RUN_RECORD + " under .quadratus/" + RUNS_DIR
    + "; files a test runner or interpreter loads because of where they sit ("
    + ", ".join(sorted(LOADED_NAMES) + ["any *" + s for s in LOADED_SUFFIXES]
                + [p + "*" for p in LOADED_PREFIXES] + ["anything under a " + MOCKS_DIR + " folder"])
    + ") anywhere inside .quadratus, a .git folder or a tool cache ("
    + ", ".join(sorted(_CACHES))
    + "); and inside " + " or ".join(sorted(_BUILD)) + ", only "
    + ", ".join(sorted(BUILD_LOADED_NAMES) + ["any *" + s for s in BUILD_LOADED_SUFFIXES]
                + ["anything under a " + MOCKS_DIR + " folder"])
    + " (in a report or cache folder, *.pth and " + MOCKS_DIR + " as well; in "
    + " or ".join(sorted(_ENVIRONMENTS)) + ", " + MOCKS_DIR + " alone)"
    ". A change to one fails the task as out of scope, with the work kept and the path named, "
    "and no later task runs while it is in place."
)


def _loaded(name: str) -> str:
    """Why a file so named is loaded by position, or ''. ``name`` is lowercased."""
    return ("loaded-code" if name in LOADED_NAMES or name.endswith(LOADED_SUFFIXES)
            or name.startswith(LOADED_PREFIXES) else "")


def loaded_by_position(parts) -> bool:
    """Whether a path is in the class a test runner or an interpreter loads
    because of where it sits: its name is one of the class, or a folder
    above it is ``__mocks__``. For a caller that writes a file where no
    listing reads (harness state), on a model's word, between calls."""
    parts = tuple(str(part).lower() for part in parts)
    return bool(parts) and (MOCKS_DIR in parts[:-1] or bool(_loaded(parts[-1])))


def _build_loaded(name: str) -> str:
    """The same inside a build folder: only what no build writes."""
    return "loaded-code" if name in BUILD_LOADED_NAMES or name.endswith(BUILD_LOADED_SUFFIXES) else ""


def _report_loaded(name: str) -> str:
    """The same inside a report or cache folder: no tool writes Python there."""
    return "report-code" if name.endswith(".py") else "loaded-code" if name.endswith(".pth") else ""


def _environment_loaded(name: str) -> str:
    """The same inside a ``.tox`` or ``.nox`` environment: no file is in the
    class by its name, because installed packages bring their own
    ``conftest.py`` and ``.pth`` files. A ``__mocks__`` folder still counts
    (``_tree`` asks any rule about those), since none is ever installed."""
    return ""


def _output_rule(name: str):
    """The loaded-by-position test for one build-output or stray-state folder."""
    if name in _STRAY_STATE:
        return _loaded
    if name in _BUILD:
        return _build_loaded
    return _report_loaded if name in _PYTHON_FREE else _environment_loaded


#: The operator's standing permission to run in a project that carries
#: vendor agent configuration.
ALLOW_AGENT_CONFIG_ENV = "QUADRATUS_ALLOW_PROJECT_AGENT_CONFIG"


class AgentConfigRefused(RuntimeError):
    """An editing call would start a vendor CLI in a project that carries
    agent configuration the operator has not allowed for this run. Raised
    before the call. A run stop: no re-plan can make a project file trusted."""


def agent_config_permitted(flag: bool = False) -> bool:
    """Whether the operator allowed project agent configuration: the run's
    own flag, or the standing environment permission."""
    return bool(flag) or os.environ.get(ALLOW_AGENT_CONFIG_ENV, "").strip().lower() in {"1", "true", "yes"}


#: The operator's acceptance of watched paths an earlier run left changed.
ACCEPT_WATCHED_PATHS_ENV = "QUADRATUS_ACCEPT_WATCHED_PATHS"


def watched_paths_accepted(flag: bool = False) -> bool:
    """Whether the operator accepted the watched paths an earlier run left
    changed: the run's own flag, or the environment permission."""
    return bool(flag) or os.environ.get(ACCEPT_WATCHED_PATHS_ENV, "").strip().lower() in {"1", "true", "yes"}


class SensitiveChangeInPlace(RuntimeError):
    """A change an earlier editing call of this run made to a watched path is
    still there. Raised before any further call for the project other than
    the orchestrator's. A run stop: the path is in no measured diff and no
    review copy, so nothing checked on top of it says anything about the
    source, and no model can undo it without changing a watched path again."""


class WatchUnavailable(RuntimeError):
    """The watched paths could not be listed within the bounds. Raised before
    an editing call, so none runs unwatched, and after one, so its changes
    are never accepted unlisted."""


@dataclass(frozen=True)
class WatchListing:
    #: path -> record, for the sensitive class.
    sensitive: Dict[str, tuple] = field(default_factory=dict)
    #: path -> lstat signature, for the build-output class.
    output: Dict[str, tuple] = field(default_factory=dict)
    #: path -> why a sensitive path is watched: git, secret, credential,
    #: agent-config, policy, memory, symlink, report-code, loaded-code or
    #: run-record.
    kinds: Dict[str, str] = field(default_factory=dict)
    seen: int = 0
    read_bytes: int = 0


def _bound(name: str, default, cast):
    """An operator override of one bound, or the default when unset or unusable."""
    try:
        value = cast(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if 0 < value < float("inf") else default


class ProjectWatch:
    """Lists one project's watched paths, within bounds."""

    def __init__(self, root, *, exclude: Iterable = (), max_entries=None, max_bytes=None,
                 deadline=None, clock: Callable[[], float] = time.monotonic, state_files: bool = True):
        self.root = Path(root)
        self._exclude = frozenset(str(Path(p)) for p in exclude)
        #: False leaves the project's own ``.quadratus`` unopened, for a
        #: caller that only asks what else the project carries.
        self.state_files = state_files
        self.max_entries = max_entries or _bound("QUADRATUS_WATCH_MAX_ENTRIES", MAX_ENTRIES, int)
        self.max_bytes = max_bytes or _bound("QUADRATUS_WATCH_MAX_BYTES", MAX_BYTES, int)
        self.deadline = deadline or _bound("QUADRATUS_WATCH_SECONDS", DEADLINE_SECONDS, float)
        self.clock = clock

    # -- one listing --------------------------------------------------------------
    def listing(self) -> WatchListing:
        state = dict(seen=0, read=0, stop=self.clock() + self.deadline,
                     sensitive={}, output={}, kinds={})
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as exc:
            raise WatchUnavailable(f"the project root could not be opened: {exc}") from exc
        try:
            self._source(fd, (), state)
        except OSError as exc:
            # Anything the walk did not already name: still no partial listing.
            raise WatchUnavailable(f"could not list watched paths: {exc}") from exc
        finally:
            os.close(fd)
        return WatchListing(state["sensitive"], state["output"], state["kinds"],
                            state["seen"], state["read"])

    @staticmethod
    def changes(before: WatchListing, after: WatchListing) -> Tuple[List[Tuple[str, str]], List[str]]:
        """``(sensitive changes, build-output paths changed)``. A sensitive
        change is ``(path, what)`` with ``what`` one of added, changed,
        retargeted, deleted. Both sorted by path."""
        sensitive = []
        for path in sorted(before.sensitive.keys() | after.sensitive.keys()):
            old, new = before.sensitive.get(path), after.sensitive.get(path)
            if old == new:
                continue
            what = ("added" if old is None else "deleted" if new is None
                    else "retargeted" if old[0] == new[0] == "link" else "changed")
            sensitive.append((path, what))
        output = sorted(p for p in before.output.keys() | after.output.keys()
                        if before.output.get(p) != after.output.get(p))
        return sensitive, output

    # -- bounds -------------------------------------------------------------------
    def _tick(self, state, rel):
        state["seen"] += 1
        if state["seen"] > self.max_entries:
            raise WatchUnavailable(
                f"more than {self.max_entries} entries while listing watched paths (at {rel}). "
                "Remove build output or old run records the project does not need, or raise "
                "QUADRATUS_WATCH_MAX_ENTRIES.")
        if self.clock() > state["stop"]:
            raise WatchUnavailable(
                f"listing watched paths took longer than {self.deadline:g}s (at {rel}). "
                "Raise QUADRATUS_WATCH_SECONDS if the project is really that large.")

    def _scan(self, fd, rel):
        try:
            return os.scandir(fd)
        except OSError as exc:
            raise WatchUnavailable(f"could not list {rel or '.'}: {exc}") from exc

    def _lstat(self, entry, rel):
        """The entry's own stat, or None when it vanished since it was listed."""
        try:
            return entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WatchUnavailable(f"could not stat {rel}: {exc}") from exc

    @contextmanager
    def _dir(self, parent_fd, name, rel):
        """An open folder below ``parent_fd``, never through a link, or None
        when it vanished since it was listed."""
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=parent_fd)
        except FileNotFoundError:
            yield None
            return
        except OSError as exc:
            raise WatchUnavailable(f"could not open {rel}: {exc}") from exc
        try:
            yield fd
        finally:
            os.close(fd)

    def _deep(self, parts):
        if len(parts) > MAX_DEPTH:
            raise WatchUnavailable(f"directory nesting deeper than {MAX_DEPTH} at {'/'.join(parts)}")

    # -- records ------------------------------------------------------------------
    def _target(self, parent_fd, name, rel):
        """A symlink's target text, or None when it vanished since it was listed."""
        try:
            return os.readlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WatchUnavailable(f"could not read symlink {rel}: {exc}") from exc

    def _file(self, parent_fd, name, rel, st, state) -> tuple:
        """``("file", mode, sha256)``. A file that cannot be opened or read is
        recorded by its lstat signature, which a rewrite still changes."""
        fallback = ("unreadable", st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        except OSError:
            return fallback
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return fallback
            digest = hashlib.sha256()
            while True:
                if self.clock() > state["stop"]:
                    raise WatchUnavailable(
                        f"listing watched paths took longer than {self.deadline:g}s (reading {rel}). "
                        "Raise QUADRATUS_WATCH_SECONDS if the project is really that large.")
                chunk = os.read(fd, min(_CHUNK, self.max_bytes - state["read"] + 1))
                if not chunk:
                    break
                state["read"] += len(chunk)
                if state["read"] > self.max_bytes:
                    raise WatchUnavailable(
                        f"more than {self.max_bytes} bytes to hash in watched paths (at {rel}). "
                        "Raise QUADRATUS_WATCH_MAX_BYTES if these files are really that large.")
                digest.update(chunk)
        except OSError:
            return fallback
        finally:
            os.close(fd)
        return ("file", st.st_mode, digest.hexdigest())

    def _sensitive(self, parent_fd, name, rel, st, state, kind):
        """Record one sensitive entry that is not a directory."""
        if stat.S_ISLNK(st.st_mode):
            target = self._target(parent_fd, name, rel)
            if target is None:
                return
            state["sensitive"][rel] = ("link", st.st_mode, target)
        elif stat.S_ISREG(st.st_mode):
            state["sensitive"][rel] = self._file(parent_fd, name, rel, st, state)
        else:
            # FIFOs, sockets and devices are recorded, never opened.
            state["sensitive"][rel] = ("other", st.st_mode)
        state["kinds"][rel] = kind

    @staticmethod
    def _signature(st) -> tuple:
        return (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    # -- traversal ----------------------------------------------------------------
    def _source(self, fd, parts, state):
        """Walk source, recording what the measured diff cannot see."""
        self._deep(parts)
        with self._scan(fd, "/".join(parts)) as listing:
            for entry in listing:
                name = entry.name
                child = (*parts, name)
                rel = "/".join(child)
                self._tick(state, rel)
                if name in DEPENDENCY_DIRS:
                    continue  # deptree identifies these, links so named included
                st = self._lstat(entry, rel)
                if st is None:
                    continue
                if (not parts and name == ".quadratus") or (
                        self._exclude and f"{self.root}/{rel}" in self._exclude):
                    # Harness state: the project's own .quadratus, and the
                    # run's state folder when the operator put it elsewhere
                    # in the project, which is the one path a run excludes.
                    if self.state_files:
                        self._state(fd, name, child, st, state)
                    continue
                if name == ".git":
                    self._git(fd, name, child, st, state)
                elif stat.S_ISLNK(st.st_mode):
                    # A link named .claude can lead to a settings.json kept
                    # under any other name: the link is the configuration.
                    kind = withheld(child) or (
                        "agent-config" if name.lower() in AGENT_CONFIG_PARENTS else "symlink")
                    self._sensitive(fd, name, rel, st, state, kind)
                elif stat.S_ISDIR(st.st_mode):
                    with self._dir(fd, name, rel) as sub:
                        if sub is None:
                            continue
                        if name in _CACHES:
                            self._unlisted(sub, child, state)
                        elif name.lower() in AGENT_CONFIG_DIRS:
                            self._tree(sub, child, state, kind="agent-config")
                        elif name in OUTPUT_NAMES or name in _STRAY_STATE:
                            self._tree(sub, child, state, kind=None, loads=_output_rule(name))
                        else:
                            self._source(sub, child, state)
                elif stat.S_ISREG(st.st_mode) and (name in OUTPUT_NAMES or name in _STRAY_STATE):
                    # A file named like harness state is in the measured
                    # source's skip set too, so it is listed here or nowhere.
                    state["output"][rel] = self._signature(st)
                else:
                    # A regular file, or a FIFO, socket or device in its
                    # place: one named like a watched file is recorded.
                    kind = withheld(child)
                    if kind:
                        self._sensitive(fd, name, rel, st, state, kind)

    def _tree(self, fd, parts, state, *, kind, loads=None, mocks=False, skip=frozenset()):
        """Every entry below one folder: hashed into the sensitive class when
        ``kind`` names why it is watched, else signed into the output class.
        ``loads`` is the folder's loaded-by-position test (``_output_rule``):
        a file it names, and everything under a ``__mocks__`` folder
        (``mocks``), is sensitive, since nothing writes one there but a call
        hiding code. ``skip`` names regular files directly in this folder
        that are left out (git's own housekeeping)."""
        self._deep(parts)
        with self._scan(fd, "/".join(parts)) as listing:
            for entry in listing:
                name = entry.name
                child = (*parts, name)
                rel = "/".join(child)
                self._tick(state, rel)
                st = self._lstat(entry, rel)
                if st is None:
                    continue
                lower = name.lower()
                why = ("loaded-code" if mocks else loads(lower)) if loads is not None else ""
                if stat.S_ISDIR(st.st_mode):
                    with self._dir(fd, name, rel) as sub:
                        if sub is not None:
                            self._tree(sub, child, state, kind=kind, loads=loads,
                                       mocks=mocks or (loads is not None and lower == MOCKS_DIR))
                elif name in skip and stat.S_ISREG(st.st_mode):
                    continue
                elif why:
                    self._sensitive(fd, name, rel, st, state, why)
                elif kind is None:
                    target = self._target(fd, name, rel) if stat.S_ISLNK(st.st_mode) else ""
                    if target is not None:
                        state["output"][rel] = self._signature(st) + ((target,) if target else ())
                else:
                    self._sensitive(fd, name, rel, st, state, kind)

    def _named(self, fd, leaf, rel, state):
        """The lstat of one name in an open folder, or None when it is not there."""
        self._tick(state, rel)
        try:
            return os.lstat(leaf, dir_fd=fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WatchUnavailable(f"could not stat {rel}: {exc}") from exc

    def _state(self, parent_fd, name, parts, st, state):
        """One harness state folder: the files a later run would trust or
        measure by, the run records the next preflight reads, and whatever
        else in it a runner loads by position. The rest of a run's records
        change during every call and are not recorded."""
        rel = "/".join(parts)
        if stat.S_ISLNK(st.st_mode):
            self._sensitive(parent_fd, name, rel, st, state, "policy")
            return
        if not stat.S_ISDIR(st.st_mode):
            return
        with self._dir(parent_fd, name, rel) as fd:
            if fd is None:
                return
            for leaf in STATE_FILES:
                found = self._named(fd, leaf, f"{rel}/{leaf}", state)
                if found is not None:
                    self._sensitive(fd, leaf, f"{rel}/{leaf}", found, state,
                                    _STATE_KINDS.get(leaf, "policy"))
            self._unlisted(fd, parts, state, own=frozenset(STATE_FILES), runs=True)

    def _unlisted(self, fd, parts, state, *, mocks=False, link="symlink", ledger="",
                  own=frozenset(), runs=False, ended=False, plain=False):
        """A folder no other listing reads (harness state, a tool cache, git's
        own storage): walked by name, never through a link, recording only
        what a runner loads by position and every link that leads to a
        folder. Everything else there is rewritten by tools and the harness
        during a call, so it is neither hashed nor signed, and costs one
        directory entry.

        ``own`` names entries of this folder already recorded; ``runs``
        marks a state folder, whose ``runs`` holds the run records;
        ``ledger`` is ``refs`` or ``logs`` below one of git's. ``ended``
        marks the folder of a run whose record is written, and ``plain`` its
        ``records`` or ``artifacts`` folder, where the harness keeps regular
        files only: a link to a file or a FIFO there is recorded too, since
        a resume reads those folders."""
        self._deep(parts)
        with self._scan(fd, "/".join(parts)) as listing:
            for entry in listing:
                name = entry.name
                child = (*parts, name)
                rel = "/".join(child)
                self._tick(state, rel)
                lower = name.lower()
                if lower in own:
                    continue
                try:
                    is_link = entry.is_symlink()
                    is_dir = not is_link and entry.is_dir(follow_symlinks=False)
                except OSError as exc:
                    raise WatchUnavailable(f"could not stat {rel}: {exc}") from exc
                if is_dir:
                    with self._dir(fd, name, rel) as sub:
                        if sub is None:
                            continue
                        if runs and lower == RUNS_DIR:
                            self._runs(sub, child, state)
                        else:
                            self._unlisted(sub, child, state, mocks=mocks or lower == MOCKS_DIR,
                                           link=link, ledger=ledger,
                                           plain=plain or (ended and name in RUN_STATE_DIRS))
                elif mocks or _loaded(lower) or (is_link and (plain or self._leads_to_folder(entry))):
                    st = self._lstat(entry, rel)
                    if st is None or (ledger and not is_link and self._git_ledger(fd, name, st, ledger, state)):
                        continue
                    self._sensitive(fd, name, rel, st, state, link if is_link else "loaded-code")
                elif plain:
                    st = self._lstat(entry, rel)
                    if st is not None and not stat.S_ISREG(st.st_mode):
                        self._sensitive(fd, name, rel, st, state, "run-record")

    @staticmethod
    def _leads_to_folder(entry) -> bool:
        """Whether a link in an unlisted folder leads to a folder right now.
        pytest collects through such a link, so it is recorded like the code
        it can deliver. A link to a file is loaded only under a name in the
        class, and is otherwise left to whoever owns the folder: the
        evidence check stops a run on a linked screenshot by its own rule.
        The target is asked about and never opened; one that cannot be
        asked about counts."""
        try:
            return entry.is_dir()
        except OSError:
            return True

    def _runs(self, fd, parts, state):
        """A state folder's ``runs``: each run folder by presence and its
        ``result.json`` by lstat signature, which any rewrite changes. A
        record can be megabytes and there is one per run ever made here, so
        none is hashed. The current run's folder is there before its first
        call and its record is written after its last, so neither changes
        under an editing call unless the call did it."""
        self._deep(parts)
        with self._scan(fd, "/".join(parts)) as listing:
            for entry in listing:
                name = entry.name
                child = (*parts, name)
                rel = "/".join(child)
                self._tick(state, rel)
                st = self._lstat(entry, rel)
                if st is None:
                    continue
                if not stat.S_ISDIR(st.st_mode):
                    if stat.S_ISLNK(st.st_mode) or _loaded(name.lower()):
                        self._sensitive(fd, name, rel, st, state,
                                        "symlink" if stat.S_ISLNK(st.st_mode) else "loaded-code")
                    continue
                with self._dir(fd, name, rel) as sub:
                    if sub is None:
                        continue
                    state["sensitive"][rel] = ("dir",)
                    state["kinds"][rel] = "run-record"
                    record = f"{rel}/{RUN_RECORD}"
                    found = self._named(sub, RUN_RECORD, record, state)
                    if found is not None and stat.S_ISREG(found.st_mode):
                        state["sensitive"][record] = ("stat", *self._signature(found))
                        state["kinds"][record] = "run-record"
                    elif found is not None:
                        self._sensitive(sub, RUN_RECORD, record, found, state, "run-record")
                    ended = found is not None or self._died(rel)
                    if ended:
                        self._run_state(sub, rel, state)
                    self._unlisted(sub, child, state, own=frozenset({RUN_RECORD}), ended=ended)

    def _died(self, rel) -> bool:
        """Whether a run folder with no record is one no process writes to:
        this process made a run folder beside it and did not make this one.
        A process that made none (a reader, a test) cannot tell, and leaves
        the folder to its store as before."""
        path = os.path.realpath(self.root / rel)
        return path not in _MADE_HERE and os.path.dirname(path) in _MADE_IN

    def _run_state(self, fd, rel, state):
        """What a run that ended committed: each ``RUN_STATE`` file by lstat
        signature. The harness writes none of them after the run's record,
        so a difference across an editing call is the call's. The
        ``RUN_STATE_DIRS`` folders are walked by ``_unlisted``."""
        for leaf in RUN_STATE:
            path = f"{rel}/{leaf}"
            found = self._named(fd, leaf, path, state)
            if found is None:
                continue
            if stat.S_ISREG(found.st_mode):
                state["sensitive"][path] = ("stat", *self._signature(found))
                state["kinds"][path] = "run-record"
            else:
                self._sensitive(fd, leaf, path, found, state, "run-record")

    def _git_ledger(self, fd, name, st, ledger, state) -> bool:
        """Whether a file below git's ``refs`` or ``logs`` holds what git
        writes there, so that a branch named like code is not taken for it.
        A ref is one object name and nothing else; a reflog starts with two.
        Neither can run: the first is one bare name or number, and two names
        side by side are a syntax error in any file a runner would load. Nor
        can an empty file, which is what git leaves of a reflog whose every
        entry expired (a gc does that, and can run during a call); one that
        is filled later is read again then."""
        if not stat.S_ISREG(st.st_mode) or (ledger == "refs" and st.st_size > 65):
            return False
        try:
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
        except OSError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                return False
            head = os.read(handle, 160)
        except OSError:
            return False
        finally:
            os.close(handle)
        state["read"] += len(head)
        return not head or bool(_REF.fullmatch(head) if ledger == "refs" else _REFLOG.match(head))

    def _git(self, parent_fd, name, parts, st, state):
        rel = "/".join(parts)
        if not stat.S_ISDIR(st.st_mode):
            # A link, or the pointer file of a worktree or submodule.
            self._sensitive(parent_fd, name, rel, st, state, "git")
            return
        with self._dir(parent_fd, name, rel) as fd:
            if fd is not None:
                self._git_dir(fd, parts, state, modules=False)

    def _git_dir(self, fd, parts, state, *, modules):
        """One git directory: its config, hooks and info, and by name only
        the rest of it, where pytest collects a ``conftest.py`` as readily
        as anywhere once a project's config lets it into dot folders. Under
        ``modules`` every folder may be a submodule's git directory or a
        path component above one, so each is read the same way."""
        self._deep(parts)
        with self._scan(fd, "/".join(parts)) as listing:
            for entry in listing:
                name = entry.name
                child = (*parts, name)
                rel = "/".join(child)
                self._tick(state, rel)
                st = self._lstat(entry, rel)
                if st is None:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    self._sensitive(fd, name, rel, st, state, "git")
                elif stat.S_ISDIR(st.st_mode):
                    with self._dir(fd, name, rel) as sub:
                        if sub is None:
                            continue
                        if name in _GIT_TREES:
                            self._tree(sub, child, state, kind="git",
                                       skip=_GIT_HOUSEKEEPING.get(name, frozenset()))
                        elif name == "modules" or (modules and name not in _GIT_INTERNALS):
                            self._git_dir(sub, child, state, modules=True)
                        else:
                            self._unlisted(sub, child, state, mocks=name.lower() == MOCKS_DIR, link="git",
                                           ledger=name if name in _GIT_LEDGERS else "")
                elif stat.S_ISREG(st.st_mode) and name in _GIT_FILES:
                    self._sensitive(fd, name, rel, st, state, "git")
                elif _loaded(name.lower()):
                    self._sensitive(fd, name, rel, st, state, "loaded-code")


def agent_config(listing: WatchListing) -> Dict[str, tuple]:
    """The vendor agent configuration a project carries: path to the record
    the listing took of it, in path order."""
    return {path: listing.sensitive[path] for path, kind in sorted(listing.kinds.items())
            if kind == "agent-config"}


def _as_record(value):
    """A listing record as it comes back from JSON: a list again a tuple."""
    return tuple(value) if isinstance(value, (list, tuple)) else None


def still_in_place(recorded: Iterable, listing: WatchListing) -> List[dict]:
    """The recorded paths that still hold what an earlier run's finding
    recorded, as ``dict(path, what, kind, by, since)``.

    ``recorded`` is ``project_watch.paths`` from that run's ``result.json``:
    read back from a file, so anything that is not a path with a record is
    skipped, never raised on. A path the call added, changed or retargeted is
    in place while its record is the listing's; one it deleted, while it is
    still absent. A later record for the same path replaces an earlier one.

    The codebase map and the scoreboard (kind ``memory``) are held to a
    different test, because the harness appends to them itself between
    calls: one note the orchestrator wrote after the finding made the file
    differ from its record, the finding stopped standing, and the next run
    loaded the line the editing call had planted. Such a file is in place
    until it is gone or back as it was before the call (``before``);
    ``since`` then says it has been written to after the record. A file
    that only differs proves nothing about the planted lines, and the
    operator's acceptance is the way past it.
    """
    latest: Dict[str, dict] = {}
    for entry in recorded if isinstance(recorded, (list, tuple)) else ():
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"]:
            latest[entry["path"]] = entry
    found = []
    for path in sorted(latest):
        entry = latest[path]
        what = str(entry.get("what") or "")
        kind = str(entry.get("kind") or "")
        record = _as_record(entry.get("record"))
        now = listing.sensitive.get(path)
        if what == "deleted":
            standing = now is None
        elif record is None:
            continue    # no record of what the call left: nothing to compare
        elif kind == "memory":
            standing = now is not None and now != _as_record(entry.get("before"))
        else:
            standing = now == record
        if standing:
            found.append(dict(path=path, what=what or "changed", kind=kind,
                              by=str(entry.get("by") or "")[:200],
                              since=bool(now is not None and record is not None and now != record)))
    return found


def describe(changes: Iterable[Tuple[str, str]], limit: int = _NAMED) -> str:
    """Sensitive changes as one bounded line: ``path (what), ...``."""
    changes = list(changes)
    named = ", ".join(f"{path} ({what})" for path, what in changes[:limit])
    return named + (f" and {len(changes) - limit} more" if len(changes) > limit else "")
