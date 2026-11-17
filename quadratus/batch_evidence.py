"""A merged parallel child's design evidence, brought into the project.

A parallel task runs in its own copy of the project (``Session._run_batch``).
Its renders land under that copy's ``.quadratus/design-evidence/<task>``.
That folder is harness state, not source, so the merge never carried it, and
the copy is deleted when the batch ends. The parent was left holding the
child's digests and screenshot paths for files that no longer existed: an
approved parallel UI task could never complete a run, and the audit was
shown paths into a deleted folder (review 1110, session second half, item 3).

:func:`adopt` copies the task's evidence set, and only that set, into the
project: the summary and each view's screenshot and record. Those are the
files a reviewer is handed and the only ones a delivery records digests for
(``runtime._EVIDENCE_PATH``), under the same size bounds.

The copy never follows a link. Every folder on both sides is opened through
the handle of the folder above it with ``O_NOFOLLOW``, only regular files
are read, and a file is published by renaming a private temporary name over
the declared one, so nothing is ever written through a link that was
already there. Bytes are copied as they are: the digests a design review
approved are of these bytes, and :meth:`Session._note_replaced_evidence`
still compares them at completion.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from typing import Dict, List, Optional, Tuple

from .design_evidence import EVIDENCE_DIR, VIEW_FILES, VIEWPORTS

#: The evidence set, relative to a task's evidence folder.
EVIDENCE_NAMES: Tuple[str, ...] = ("summary.json",) + tuple(
    f"{view}/{leaf}" for view in VIEWPORTS for leaf in VIEW_FILES)

_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
# Non-blocking, so a FIFO in a file's place cannot stall the merge.
_FILE = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _bounds() -> Tuple[int, int]:
    """Per-file and total byte bounds: the ones a review delivery is held to,
    read from where they are defined so the two cannot drift."""
    from .runtime import _MAX_EVIDENCE_BYTES, _MAX_EVIDENCE_TOTAL
    return _MAX_EVIDENCE_BYTES, _MAX_EVIDENCE_TOTAL


def _close(fds: List[int]) -> None:
    for fd in reversed(fds):
        try:
            os.close(fd)
        except OSError:
            pass


def _descend(root, parts, fds: List[int], *, create: bool = False) -> int:
    """Open ``root`` and then each of ``parts`` beneath it, no link followed.
    Every handle is appended to ``fds`` for the caller to close."""
    fds.append(os.open(str(root), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)))
    for part in parts:
        try:
            fd = os.open(part, _DIR, dir_fd=fds[-1])
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(part, 0o755, dir_fd=fds[-1])
            fd = os.open(part, _DIR, dir_fd=fds[-1])
        fds.append(fd)
    return fds[-1]


def _read_set(folder: int, problems: List[str]) -> Dict[str, Tuple[bytes, os.stat_result]]:
    """The evidence files present under the open task folder, by name."""
    limit, total_limit = _bounds()
    found: Dict[str, Tuple[bytes, os.stat_result]] = {}
    total = 0
    for name in EVIDENCE_NAMES:
        *above, leaf = name.split("/")
        fds: List[int] = []
        try:
            where = folder
            for part in above:
                where = os.open(part, _DIR, dir_fd=where)
                fds.append(where)
            fd = os.open(leaf, _FILE, dir_fd=where)
            fds.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                problems.append(f"{name} is not a regular file")
                continue
            if info.st_size > limit:
                problems.append(f"{name} is larger than the evidence bound")
                continue
            data = b""
            while len(data) <= limit:
                chunk = os.read(fd, min(1 << 20, limit + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) > limit:
                problems.append(f"{name} is larger than the evidence bound")
                continue
            total += len(data)
            if total > total_limit:
                problems.append(f"{name} takes the set over the total evidence bound")
                continue
            found[name] = (data, info)
        except FileNotFoundError:
            continue        # not captured; mirrored as absent
        except OSError as exc:
            problems.append(f"{name} could not be read without following a link "
                            f"({exc.__class__.__name__})")
        finally:
            _close(fds)
    return found


def _publish(folder: int, name: str, data: bytes, info: os.stat_result) -> None:
    """Write ``data`` as ``name`` under the open task folder. The bytes go to
    a private temporary name, created exclusively, and that name is renamed
    over the declared one: a link already at the declared name is replaced
    as a name, never written through."""
    *above, leaf = name.split("/")
    fds: List[int] = []
    try:
        where = folder
        for part in above:
            try:
                where = os.open(part, _DIR, dir_fd=where)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=where)
                where = os.open(part, _DIR, dir_fd=where)
            fds.append(where)
        temp = f".merge-{os.getpid()}-{secrets.token_hex(6)}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o644, dir_fd=where)
        fds.append(fd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            # The capture's own times: a later reader compares a render's
            # age with the edit it follows.
            os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))
            os.fsync(fd)
            os.replace(temp, leaf, src_dir_fd=where, dst_dir_fd=where)
        except BaseException:
            # Whatever stopped the write, the private name does not stay.
            try:
                os.unlink(temp, dir_fd=where)
            except OSError:
                pass
            raise
    finally:
        _close(fds)


def _retire(folder: int, name: str) -> bool:
    """Remove ``name`` under the open task folder when it is a file or a
    link left by an earlier run (task ids restart at t1 each run), so the
    project's set is the child's set. True when something was removed."""
    *above, leaf = name.split("/")
    fds: List[int] = []
    try:
        where = folder
        for part in above:
            where = os.open(part, _DIR, dir_fd=where)
            fds.append(where)
        info = os.stat(leaf, dir_fd=where, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            raise IsADirectoryError(name)
        os.unlink(leaf, dir_fd=where)
        return True
    except FileNotFoundError:
        return False
    finally:
        _close(fds)


def adopt(child_root, project_root, task_id: str) -> dict:
    """Copy ``task_id``'s evidence set from ``child_root`` into ``project_root``.

    Returns ``{"present", "copied", "removed", "problems"}``: whether the
    child copy holds an evidence folder for the task at all; the sha256 of
    each file copied, by project-relative path; the stale project files
    removed because the child has none by that name; and why the set could
    not be brought over, if it could not. Never raises. Nothing is written
    when the child's set cannot be read whole and clean, so a problem never
    leaves a mixed set behind on its own.
    """
    result: dict = dict(present=False, copied={}, removed=[], problems=[])
    problems: List[str] = result["problems"]
    if not _TASK_NAME.fullmatch(task_id or ""):
        problems.append("the task id is not a plain folder name")
        return result
    parts = (*EVIDENCE_DIR.parts, task_id)
    relative = "/".join(parts)
    source: List[int] = []
    target: List[int] = []
    try:
        try:
            folder = _descend(child_root, parts, source)
        except FileNotFoundError:
            return result       # the child captured nothing: nothing to bring
        except OSError as exc:
            result["present"] = True
            problems.append(f"the copy's {relative} is a link or not a folder ({exc.__class__.__name__})")
            return result
        result["present"] = True
        found = _read_set(folder, problems)
        if problems:
            return result
        try:
            home = _descend(project_root, parts, target, create=True)
        except OSError as exc:
            problems.append(f"the project's {relative} is a link or not a folder ({exc.__class__.__name__})")
            return result
        for name in EVIDENCE_NAMES:
            try:
                if name in found:
                    data, info = found[name]
                    _publish(home, name, data, info)
                    result["copied"][f"{relative}/{name}"] = hashlib.sha256(data).hexdigest()
                elif _retire(home, name):
                    result["removed"].append(f"{relative}/{name}")
            except OSError as exc:
                problems.append(f"{name} could not be written into the project ({exc.__class__.__name__})")
        return result
    except (OSError, NotImplementedError, ValueError) as exc:
        # A platform without directory handles, or anything unforeseen: the
        # set did not come over, and the caller records that. Never a crash
        # in the middle of a merge.
        problems.append(f"the evidence could not be copied without following a link ({exc.__class__.__name__})")
        return result
    finally:
        _close(source)
        _close(target)


def moved_path(shot, child_root, project_root) -> Optional[str]:
    """``shot`` re-based from the child copy to the project when it is a path
    under the copy; None when it is anything else (a target or steps line)."""
    text = str(shot)
    base = str(child_root).rstrip(os.sep)
    if text == base or not text.startswith(base + os.sep):
        return None
    return os.path.join(str(project_root), text[len(base) + 1:])
