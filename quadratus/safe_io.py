"""Private, crash-tolerant writes for the harness's own records.

A run's records hold the goal, the diff, check output and model text. They
were written in place with whatever mode the umask allowed, inside a folder
that was closed to other users only when a run reached its end, so anyone on
the machine could read a run in progress and a crash could leave half a file
(whole-repo review, 2026-11-10). These helpers are the one way a record is
written:

* a folder the harness makes is owner-only from the moment it exists;
* a file is owner-only from creation;
* a replaced file is the old bytes or the new ones, never a mixture: the new
  bytes go to a temporary name beside it, reach the disk, and are renamed in;
* a link is never written through or tightened.

Nothing here knows what a record means. The monitor reads the same files as
the same user, so none of this changes what it sees.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path

__all__ = [
    "IGNORE_ALL",
    "append_private",
    "create_in",
    "ignore_everything",
    "make_private_dir",
    "prepare_state_dir",
    "replace_in",
    "tighten_files",
    "write_private",
]

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

#: What the state folder's own ``.gitignore`` holds. Everything the harness
#: writes there is a record. The one file an operator writes there by hand,
#: the repository policy (``.quadratus/policy.json``), stays visible to git:
#: hiding it would keep a new policy out of a commit with nothing said.
IGNORE_ALL = "# Quadratus run records and model transcripts. Not for commit.\n*\n!/policy.json\n"


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _flush(fd: int) -> None:
    """Push the bytes to disk. A filesystem that cannot do that is not a
    reason to lose the record; a real I/O error still is."""
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise


def _unlink(name, dir_fd) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _owned_and_open(info) -> bool:
    """Ours, and readable or writable by someone else."""
    return info.st_uid == os.geteuid() and bool(stat.S_IMODE(info.st_mode) & 0o077)


def _temp_in(dir_fd: int, name: str, data: bytes) -> str:
    """``data`` in a fresh owner-only file beside ``name``; its name."""
    temp = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        try:
            _write_all(fd, data)
            _flush(fd)
        finally:
            os.close(fd)
    except BaseException:
        _unlink(temp, dir_fd)
        raise
    return temp


def replace_in(dir_fd: int, name: str, data: bytes) -> None:
    """Make ``name`` in the open folder hold exactly ``data``, owner-only.

    Whatever was at the name, a link included, is replaced by the rename and
    never written through.
    """
    temp = _temp_in(dir_fd, name, data)
    try:
        os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        _unlink(temp, dir_fd)
        raise


def create_in(dir_fd: int, name: str, data: bytes) -> bool:
    """Create ``name`` holding ``data`` only when nothing is there.

    The name appears with all of its bytes or not at all, so a crash cannot
    leave a short file that later reads as the record. False when something
    already has the name; it is left untouched.
    """
    temp = _temp_in(dir_fd, name, data)
    try:
        try:
            os.link(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            return True
        except FileExistsError:
            return False
        except OSError:
            pass  # no hard links on this filesystem: create the name directly
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            return False
        try:
            _write_all(fd, data)
            _flush(fd)
        finally:
            os.close(fd)
        return True
    finally:
        _unlink(temp, dir_fd)


def write_private(path, data) -> None:
    """Replace ``path`` with ``data`` (text or bytes): owner-only, and whole
    or not at all. A reader sees the old file or the new one."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    dir_fd = os.open(path.parent, os.O_RDONLY | _DIRECTORY)
    try:
        replace_in(dir_fd, path.name, bytes(data))
        try:
            os.fsync(dir_fd)  # the rename itself, where the filesystem can
        except OSError:
            pass
    finally:
        os.close(dir_fd)


def append_private(path, text) -> None:
    """Append ``text`` to ``path``, which is created owner-only.

    A link, a FIFO or anything else that is not a regular file is refused
    with OSError. A record this user owns that an older version left open to
    others is closed to them.
    """
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW | _NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "record is not a regular file", str(path))
        if _owned_and_open(info):
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass  # the line still gets written
        _write_all(fd, data)
    finally:
        os.close(fd)


def _tighten_dir(path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError:
        return  # a link, or gone: left exactly as it is
    try:
        if _owned_and_open(os.fstat(fd)):
            os.fchmod(fd, 0o700)
    except OSError:
        pass
    finally:
        os.close(fd)


def make_private_dir(path, *, tighten: bool = True) -> bool:
    """Make ``path`` an owner-only folder. True when this call created it.

    Missing parents get the usual mode: they are not the harness's to close.
    With ``tighten``, a real folder this user already owns is closed to
    others too. A link is never followed or changed.
    """
    path = Path(path)
    created = False
    try:
        os.mkdir(path, 0o700)
        created = True
    except FileExistsError:
        pass
    except FileNotFoundError:
        os.makedirs(path.parent, exist_ok=True)
        try:
            os.mkdir(path, 0o700)
            created = True
        except FileExistsError:
            pass
    if not created and not path.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "exists and is not a folder", str(path))
    if created or tighten:
        _tighten_dir(path)
    return created


def ignore_everything(folder) -> bool:
    """Put a ``.gitignore`` holding ``*`` in ``folder`` unless one is there.

    ``git add -A`` in a project otherwise commits run records: prompts,
    transcripts, check output and absolute home paths. The project's own
    ``.gitignore`` is never touched, since that would be a source change. A
    linked folder is left alone. True when the file was written. The
    operator's own ``policy.json`` at the top of the folder is not ignored
    (``IGNORE_ALL``).
    """
    try:
        dir_fd = os.open(folder, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError:
        return False
    try:
        return create_in(dir_fd, ".gitignore", IGNORE_ALL.encode("utf-8"))
    except OSError:
        return False
    finally:
        os.close(dir_fd)


def prepare_state_dir(path) -> bool:
    """A state folder ready for records: owner-only and ignored by git.

    Both apply to a folder this call creates and to one named ``.quadratus``,
    which is the harness's by name. Any other folder that already exists was
    made by the operator for their own reasons, so it keeps its mode and gets
    no ignore file; the records inside it are still private one by one. True
    when this call created the folder.
    """
    path = Path(path)
    ours = path.name == ".quadratus"
    created = make_private_dir(path, tighten=ours)
    if created or ours:
        ignore_everything(path)
    return created


def tighten_files(folder) -> None:
    """Close every regular file directly in ``folder`` that this user owns to
    other users. Covers a record whose own writer has not been moved onto
    these helpers. Never follows a link and never raises."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        try:
            fd = os.open(os.path.join(folder, name), os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
        except OSError:
            continue
        try:
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and _owned_and_open(info):
                os.fchmod(fd, 0o600)
        except OSError:
            pass
        finally:
            os.close(fd)
