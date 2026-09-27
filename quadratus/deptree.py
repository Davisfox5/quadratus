"""Content identity of a project's runtime-dependency trees.

Run 19 (2026-09-27): an editing lead wrote a ``node_modules`` shim of a
browser package into the project. The source checks never saw it, because
``node_modules`` is excluded from source, and the project's own check then
resolved the shim ahead of the real package. A tree a check or a preview can
execute from is part of what the check proves, so its identity is taken at
run start and must hold at every point the harness runs or accepts anything
(contract v2 on #25, accepted by Codex).

What counts: every directory named ``node_modules``, ``.venv``, ``venv`` or
``env`` anywhere in the project, and the absence of one. A tree that appears
later is a change. Symlinks are recorded by their target text and never
followed; a symlink met while looking for trees is recorded the same way when
it is named like a tree or resolves to a directory, because a tree behind it
would otherwise be invisible.

The identity is a manifest of ``(path, type, mode, size, sha256)`` for files,
``(path, type, mode, target)`` for symlinks and ``(path, type, mode)`` for
directories. Content is really hashed. A per-guard cache keyed by the full
lstat, ``ctime_ns`` included, skips re-reading a file whose metadata is
identical; ``ctime`` changes on every write and on ``utime`` and cannot be set
back by an ordinary process, so a same-length rewrite with its mtime restored
is re-read. That cache is an optimisation resting on the filesystem keeping
``ctime`` honestly; a process able to change the system clock could defeat
it. It is not a defence against a concurrent attacker.

Bounds are finite and fail closed: entries seen, bytes read and wall time,
all counted while walking and reading, never after listing everything. Any
overrun, unreadable entry, I/O error or race is
:class:`DependencyIdentityUnavailable`; no partial identity is accepted.

Not guarded: anything outside the project (a global ``NODE_PATH``, system
``site-packages``, ``~/.cache``), user-level configuration a runtime reads,
and trees under the top-level ``.git`` or ``.quadratus`` directories, which
are not walked.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Tuple

DEPENDENCY_DIRS = frozenset({"node_modules", ".venv", "venv", "env"})
MAX_ENTRIES = 250_000
MAX_BYTES = 2 * 1024 ** 3
DEADLINE_SECONDS = 120.0
MAX_DEPTH = 256
#: Top-level directories never walked: version control and the harness's own
#: state. Only at the project root; the same names deeper are walked.
SKIP_TOP = frozenset({".git", ".quadratus"})
_CHUNK = 1 << 20
_NAMED = 8


class DependencyIdentityUnavailable(RuntimeError):
    """The dependency trees could not be identified within the bounds; the
    run stops before the next call or check rather than proceeding unwatched."""


class DependencyTreeChanged(RuntimeError):
    """A runtime-dependency tree differs from its run-start identity."""

    def __init__(self, window: str, changed: Iterable[str] = (), detail: str = ""):
        self.window = window
        self.changed = list(changed)
        named = ", ".join(self.changed[:_NAMED]) + (
            f" and {len(self.changed) - _NAMED} more" if len(self.changed) > _NAMED else "")
        super().__init__(
            f"a runtime-dependency tree changed {window}"
            + (f": {named}" if named else "") + (f" ({detail})" if detail else "")
            + ". Edits are preserved; nothing was reverted or replayed.")


@dataclass(frozen=True)
class TreeIdentity:
    digest: str
    entries: Dict[str, tuple]
    exempt: Dict[str, tuple] = field(default_factory=dict)
    seen: int = 0
    read_bytes: int = 0


def check_exemptions(root, paths: Iterable[str]) -> Tuple[str, ...]:
    """Validate operator cache exemptions, fixed before any call.

    Each must be a relative path strictly inside a dependency tree, one level
    below its root, whose first component there is a hidden directory other
    than ``.bin`` (``node_modules/.cache``, not ``node_modules`` itself and
    not a package such as ``node_modules/react``), with no symlinked component.
    """
    root = Path(root)
    kept = []
    for raw in paths:
        text = str(raw)
        parts = tuple(text.split("/"))
        if (not text or text.startswith("/") or "\\" in text
                or any(p in ("", ".", "..") for p in parts)):
            raise ValueError(f"cache exemption must be a plain relative path: {text!r}")
        index = next((i for i, p in enumerate(parts) if p in DEPENDENCY_DIRS), None)
        if index is None or len(parts) < index + 2:
            raise ValueError(f"cache exemption must lie inside a dependency tree: {text!r}")
        below = parts[index + 1]
        if not below.startswith(".") or below == ".bin":
            raise ValueError(f"cache exemption must be a hidden cache directory, not a "
                             f"dependency root or package tree: {text!r}")
        path = root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ValueError(f"cache exemption has a symlinked component: {text!r}")
        kept.append(PurePosixPath(*parts).as_posix())
    return tuple(dict.fromkeys(kept))


class DependencyGuard:
    """Takes and compares tree identities for one project, within bounds."""

    def __init__(self, root, *, exempt: Iterable[str] = (), max_entries: int = MAX_ENTRIES,
                 max_bytes: int = MAX_BYTES, deadline: float = DEADLINE_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.root = Path(root)
        self.exempt = check_exemptions(self.root, exempt)
        self.max_entries, self.max_bytes, self.deadline = max_entries, max_bytes, deadline
        self.clock = clock
        self._cache: Dict[tuple, str] = {}
        self.lock = threading.Lock()

    # -- one identity pass ------------------------------------------------------
    def identity(self) -> TreeIdentity:
        state = dict(seen=0, read=0, stop=self.clock() + self.deadline)
        entries: Dict[str, tuple] = {}
        exempt: Dict[str, tuple] = {}
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as exc:
            raise DependencyIdentityUnavailable(f"the project root could not be opened: {exc}") from exc
        try:
            self._discover(fd, "", 0, state, entries)
        finally:
            os.close(fd)
        for name in [n for n in entries if self._exempted(n)]:
            exempt[name] = entries.pop(name)
        digest = hashlib.sha256(json.dumps(sorted(entries.items()), separators=(",", ":"))
                                .encode()).hexdigest()
        return TreeIdentity(digest, entries, exempt, state["seen"], state["read"])

    def changes(self, before: TreeIdentity, after: TreeIdentity) -> Tuple[List[str], List[str]]:
        """(guarded paths changed, exempt paths changed), sorted."""
        def diff(a, b):
            return sorted(p for p in a.keys() | b.keys() if a.get(p) != b.get(p))
        return diff(before.entries, after.entries), diff(before.exempt, after.exempt)

    # -- traversal --------------------------------------------------------------
    def _exempted(self, rel: str) -> bool:
        return any(rel == e or rel.startswith(e + "/") for e in self.exempt)

    def _tick(self, state, rel):
        state["seen"] += 1
        if state["seen"] > self.max_entries:
            raise DependencyIdentityUnavailable(
                f"more than {self.max_entries} entries while identifying dependency trees (at {rel})")
        if self.clock() > state["stop"]:
            raise DependencyIdentityUnavailable(
                f"identifying dependency trees took longer than {self.deadline:g}s (at {rel})")

    def _scan(self, fd, rel):
        try:
            return os.scandir(fd)
        except OSError as exc:
            raise DependencyIdentityUnavailable(f"could not list {rel or '.'}: {exc}") from exc

    def _open_dir(self, parent_fd, name, rel, expected):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=parent_fd)
        except OSError as exc:
            raise DependencyIdentityUnavailable(f"could not open {rel}: {exc}") from exc
        now = os.fstat(fd)
        if (now.st_dev, now.st_ino) != (expected.st_dev, expected.st_ino):
            os.close(fd)
            raise DependencyIdentityUnavailable(f"{rel} was replaced while being identified")
        return fd

    def _lstat(self, entry, rel):
        try:
            return entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise DependencyIdentityUnavailable(f"could not stat {rel}: {exc}") from exc

    def _link(self, parent_fd, name, rel, st):
        try:
            target = os.readlink(name, dir_fd=parent_fd)
        except OSError as exc:
            raise DependencyIdentityUnavailable(f"could not read symlink {rel}: {exc}") from exc
        return ("link", st.st_mode, target)

    def _discover(self, fd, rel, depth, state, entries):
        """Walk source looking for trees; only symlinks and trees are recorded."""
        if depth > MAX_DEPTH:
            raise DependencyIdentityUnavailable(f"directory nesting deeper than {MAX_DEPTH} at {rel}")
        with self._scan(fd, rel) as listing:
            for entry in listing:
                name = entry.name
                child = f"{rel}/{name}" if rel else name
                self._tick(state, child)
                if depth == 0 and name in SKIP_TOP and not entry.is_symlink():
                    continue
                st = self._lstat(entry, child)
                if stat.S_ISLNK(st.st_mode):
                    # Never followed. One named like a tree, or resolving to a
                    # directory a tree could sit behind, is recorded by its
                    # target text; a link to a file is source, not a boundary.
                    if name in DEPENDENCY_DIRS or _points_at_directory(fd, name):
                        entries[child] = self._link(fd, name, child, st)
                elif stat.S_ISDIR(st.st_mode):
                    sub = self._open_dir(fd, name, child, st)
                    try:
                        if name in DEPENDENCY_DIRS:
                            entries[child] = ("dir", st.st_mode)
                            self._tree(sub, child, depth + 1, state, entries)
                        else:
                            self._discover(sub, child, depth + 1, state, entries)
                    finally:
                        os.close(sub)

    def _tree(self, fd, rel, depth, state, entries):
        """Record every entry of one dependency tree."""
        if depth > MAX_DEPTH:
            raise DependencyIdentityUnavailable(f"directory nesting deeper than {MAX_DEPTH} at {rel}")
        with self._scan(fd, rel) as listing:
            for entry in listing:
                name = entry.name
                child = f"{rel}/{name}"
                self._tick(state, child)
                st = self._lstat(entry, child)
                if stat.S_ISLNK(st.st_mode):
                    entries[child] = self._link(fd, name, child, st)
                elif stat.S_ISDIR(st.st_mode):
                    entries[child] = ("dir", st.st_mode)
                    sub = self._open_dir(fd, name, child, st)
                    try:
                        self._tree(sub, child, depth + 1, state, entries)
                    finally:
                        os.close(sub)
                elif stat.S_ISREG(st.st_mode):
                    entries[child] = ("file", st.st_mode, st.st_size, self._hash(fd, name, child, st, state))
                else:
                    # FIFOs, sockets and devices are recorded, never opened.
                    entries[child] = ("other", st.st_mode)

    def _hash(self, parent_fd, name, rel, st, state) -> str:
        key = (rel, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        ident = key[1:]
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        except OSError as exc:
            raise DependencyIdentityUnavailable(f"could not read {rel}: {exc}") from exc
        try:
            first = os.fstat(fd)
            if not stat.S_ISREG(first.st_mode) or _ident(first) != ident:
                raise DependencyIdentityUnavailable(f"{rel} changed while being identified")
            digest, total = hashlib.sha256(), 0
            while True:
                if self.clock() > state["stop"]:
                    raise DependencyIdentityUnavailable(
                        f"identifying dependency trees took longer than {self.deadline:g}s (reading {rel})")
                want = min(_CHUNK, self.max_bytes - state["read"] + 1)
                try:
                    chunk = os.read(fd, want)
                except OSError as exc:
                    raise DependencyIdentityUnavailable(f"could not read {rel}: {exc}") from exc
                if not chunk:
                    break
                state["read"] += len(chunk)
                total += len(chunk)
                if state["read"] > self.max_bytes:
                    raise DependencyIdentityUnavailable(
                        f"more than {self.max_bytes} bytes to hash in dependency trees (at {rel})")
                digest.update(chunk)
            if total != st.st_size or _ident(os.fstat(fd)) != ident:
                raise DependencyIdentityUnavailable(f"{rel} changed while being identified")
        finally:
            os.close(fd)
        value = digest.hexdigest()
        self._cache[key] = value
        return value


def _points_at_directory(parent_fd, name) -> bool:
    """Whether a symlink currently resolves to a directory (stat only, nothing read)."""
    try:
        return stat.S_ISDIR(os.stat(name, dir_fd=parent_fd).st_mode)
    except OSError:
        return False


def _ident(st) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class DependencyWatch:
    """A run's baseline and every check against it, with the run record.

    ``verify`` raises on any difference or failure, and after one it keeps
    raising: a run whose trees changed or could not be identified never
    accepts another receipt. ``after_failure`` records instead of raising, so
    the primary outcome of a failed call is what the run reports.
    """

    def __init__(self, guard: DependencyGuard):
        self.guard = guard
        self.baseline: Optional[TreeIdentity] = None
        self.failure: str = ""
        self._exempt_seen: Optional[Dict[str, tuple]] = None
        self.record = dict(status="unverified", digest=None, entries=None, roots=[],
                           exemptions=list(guard.exempt), events=[],
                           boundary=("dependency trees named " + ", ".join(sorted(DEPENDENCY_DIRS))
                                     + " inside the project; nothing outside the project is guarded"))

    def start(self) -> None:
        with self.guard.lock:
            try:
                self.baseline = self.guard.identity()
            except DependencyIdentityUnavailable as exc:
                self._fail("at run start", str(exc), status="unverified")
                raise
            self._exempt_seen = self.baseline.exempt
            self.record.update(status="unchanged", digest=self.baseline.digest,
                               entries=len(self.baseline.entries),
                               roots=sorted(p for p, v in self.baseline.entries.items()
                                            if PurePosixPath(p).name in DEPENDENCY_DIRS))

    def verify(self, window: str) -> None:
        if self.baseline is None:
            return
        with self.guard.lock:
            if self.failure:
                raise DependencyTreeChanged(window, detail=f"already stopped: {self.failure[:200]}")
            try:
                now = self.guard.identity()
            except DependencyIdentityUnavailable as exc:
                self._fail(window, str(exc), status="unverified")
                raise
            changed, exempt = self.guard.changes(self.baseline, now)
            if exempt and now.exempt != self._exempt_seen:
                self._exempt_seen = now.exempt
                self.record["events"].append(dict(window=window, exempt_changed=exempt[:50]))
            if changed:
                self._fail(window, f"changed: {', '.join(changed[:_NAMED])}", status="changed",
                           changed=changed[:50])
                raise DependencyTreeChanged(window, changed)

    def after_failure(self, window: str, primary: BaseException) -> None:
        """Re-check after a failed call without masking its outcome."""
        if self.baseline is None or self.failure:
            return
        if not isinstance(primary, Exception):
            self._fail(window, f"not re-checked after {type(primary).__name__}", status="unverified")
            return
        try:
            self.verify(window)
        except (DependencyTreeChanged, DependencyIdentityUnavailable):
            pass

    def _fail(self, window, reason, *, status, changed=None):
        self.failure = f"{window}: {reason}"
        self.record["status"] = status
        event = dict(window=window, reason=reason[:400])
        if changed:
            event["changed"] = changed
        self.record["events"].append(event)
