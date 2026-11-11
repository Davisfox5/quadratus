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
  everything under ``hooks/`` and ``info/``, and the same inside
  ``modules/``; a ``.git`` file (a worktree or submodule pointer);
* every ``.env*`` file but ``.env.example``, the credential files and the
  vendor agent configuration (``project.withheld``, names compared without
  case), and a link named ``.claude``, which can lead to settings anywhere;
* ``.quadratus/policy.json`` and ``.quadratus/source-excludes.json``;
* every symlink in a walked directory, recorded by its target;
* a Python file under a report or cache folder (the build-output names
  below other than ``project.PYTHON_OUTPUT_NAMES``): no tool writes one
  there, and pytest collects a ``conftest.py`` from ``test-results/``.

A sensitive change that is still in place stops the run: Fleet makes no
further call for the project but the orchestrator's until the path is back
as it was (:class:`SensitiveChangeInPlace`). The file is invisible to the
measured diff and to every review copy, so a check that passes on top of it
proves nothing about the source.

Build-output class (lstat signature only; a change is an observation on the
call's record, never a failure, because a lead may legitimately run a
build): everything under a folder named in ``project.OUTPUT_NAMES``, a file
so named, and a ``.quadratus`` or ``.multi_llm`` folder or file that is not
the project's own state.

Not watched: git internals a plain ``git status`` rewrites (the index,
``HEAD``, refs, objects); dependency trees, which :mod:`quadratus.deptree`
identifies; the harness's own state under the top-level ``.quadratus``
other than the two files above; ``__pycache__``, ``.pytest_cache``,
``.ruff_cache``, ``.coverage`` and ``.DS_Store``, which every test run
rewrites; paths the operator excluded; anything outside the project.

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
           "SensitiveChangeInPlace", "SENSITIVE_RULE", "ALLOW_AGENT_CONFIG_ENV", "agent_config", "agent_config_permitted",
           "describe"]

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

#: The harness's own files under the project's ``.quadratus`` that decide
#: what a later run trusts or measures.
STATE_FILES = ("policy.json", "source-excludes.json")
#: Caches every test run rewrites. Not source and not watched.
_CACHES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".coverage", ".DS_Store"})
#: Report and cache folders: output no tool fills with Python.
_PYTHON_FREE = OUTPUT_NAMES - PYTHON_OUTPUT_NAMES
#: Folders named like harness state that are not the project's own state.
_STRAY_STATE = frozenset({".quadratus", ".multi_llm"})
_GIT_FILES = frozenset({"config", "config.worktree"})
_GIT_TREES = frozenset({"hooks", "info"})
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
    + "); " + " and ".join(".quadratus/" + name for name in STATE_FILES)
    + "; Python files inside a report or cache folder ("
    + ", ".join(sorted(_PYTHON_FREE))
    + "). A change to one fails the task as out of scope, with the work kept and the path named, "
    "and no later task runs while it is in place."
)


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
    #: agent-config, policy, symlink or report-code.
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
                "Remove build output the project does not need, or raise "
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
                if not parts and name == ".quadratus":
                    if self.state_files:
                        self._state(fd, name, st, state)
                    continue
                if self._exclude and f"{self.root}/{rel}" in self._exclude:
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
                    if name in _CACHES:
                        continue
                    with self._dir(fd, name, rel) as sub:
                        if sub is None:
                            continue
                        if name.lower() in AGENT_CONFIG_DIRS:
                            self._tree(sub, child, state, kind="agent-config")
                        elif name in OUTPUT_NAMES or name in _STRAY_STATE:
                            self._tree(sub, child, state, kind=None,
                                       python_free=name in _PYTHON_FREE)
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

    def _tree(self, fd, parts, state, *, kind, python_free=False):
        """Every entry below one folder: hashed into the sensitive class when
        ``kind`` names why it is watched, else signed into the output class.
        ``python_free`` marks a report or cache folder, where a Python file
        is sensitive: nothing writes one there but a call hiding code."""
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
                if stat.S_ISDIR(st.st_mode):
                    with self._dir(fd, name, rel) as sub:
                        if sub is not None:
                            self._tree(sub, child, state, kind=kind, python_free=python_free)
                elif python_free and name.lower().endswith(".py"):
                    self._sensitive(fd, name, rel, st, state, "report-code")
                elif kind is None:
                    target = self._target(fd, name, rel) if stat.S_ISLNK(st.st_mode) else ""
                    if target is not None:
                        state["output"][rel] = self._signature(st) + ((target,) if target else ())
                else:
                    self._sensitive(fd, name, rel, st, state, kind)

    def _state(self, parent_fd, name, st, state):
        """The project's own ``.quadratus``: only the files a later run would
        trust or measure by. The run's records change during every call."""
        if stat.S_ISLNK(st.st_mode):
            self._sensitive(parent_fd, name, name, st, state, "policy")
            return
        if not stat.S_ISDIR(st.st_mode):
            return
        with self._dir(parent_fd, name, name) as fd:
            for leaf in STATE_FILES if fd is not None else ():
                rel = f"{name}/{leaf}"
                self._tick(state, rel)
                try:
                    found = os.lstat(leaf, dir_fd=fd)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise WatchUnavailable(f"could not stat {rel}: {exc}") from exc
                self._sensitive(fd, leaf, rel, found, state, "policy")

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
        """One git directory: its config, hooks and info. Under ``modules``
        every folder may be a submodule's git directory or a path component
        above one, so each is read the same way."""
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
                    below = name == "modules" or (modules and name not in _GIT_INTERNALS)
                    if name not in _GIT_TREES and not below:
                        continue
                    with self._dir(fd, name, rel) as sub:
                        if sub is None:
                            continue
                        if name in _GIT_TREES:
                            self._tree(sub, child, state, kind="git")
                        else:
                            self._git_dir(sub, child, state, modules=True)
                elif stat.S_ISREG(st.st_mode) and name in _GIT_FILES:
                    self._sensitive(fd, name, rel, st, state, "git")


def agent_config(listing: WatchListing) -> Dict[str, tuple]:
    """The vendor agent configuration a project carries: path to the record
    the listing took of it, in path order."""
    return {path: listing.sensitive[path] for path, kind in sorted(listing.kinds.items())
            if kind == "agent-config"}


def describe(changes: Iterable[Tuple[str, str]], limit: int = _NAMED) -> str:
    """Sensitive changes as one bounded line: ``path (what), ...``."""
    changes = list(changes)
    named = ", ".join(f"{path} ({what})" for path, what in changes[:limit])
    return named + (f" and {len(changes) - limit} more" if len(changes) > limit else "")
