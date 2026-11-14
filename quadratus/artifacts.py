"""Durable storage for raw model output, addressed by reference.

The rule this exists to enforce: a summary is an index, not a replacement.

An earlier design had every path into the orchestrator pass through a cheap
digesting model, with the raw output discarded. That is a mandatory lossy hop,
and it is the failure every published system names -- Anthropic calls it the
"game of telephone", and their own probe found that after compaction 3/3
high-level facts survived while 0/3 obscure specifics did. Once a detail is
dropped from a digest there is nowhere left to recover it from.

So raw output is written here first and never deleted. A digest carries
:class:`ArtifactRef` pointers alongside its prose, and any model that needs
the real thing can fetch it. Compression stays reversible: the content leaves
the context window, not the system.

Each artifact also keeps a short preview so a reader can judge whether a fetch
is worth it without paying for the whole file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from .safe_io import create_in, make_private_dir, replace_in

__all__ = ["ArtifactRef", "ArtifactStore", "PREVIEW_LINES"]

#: How much of an artifact to inline in a reference. Enough to decide whether
#: to fetch, not enough to substitute for fetching.
PREVIEW_LINES = 10


@dataclass(frozen=True)
class ArtifactRef:
    """A pointer to stored raw output.

    Small enough to sit in a prompt in quantity. Carries a preview so a reader
    can triage without a round trip, and ``lines``/``chars`` so it can judge
    the cost of fetching.
    """

    id: str
    kind: str
    author: str
    lines: int
    chars: int
    preview: str

    def render(self) -> str:
        """One-line-plus-preview form for inclusion in a prompt. A text no
        longer than the preview is shown in full and says so, with no fetch
        pointer: re-sent every round, the pointer was pure repetition."""
        if self.lines <= PREVIEW_LINES:
            return (f"[artifact {self.id} | {self.kind} | by {self.author} | "
                    f"{self.lines} lines, {self.chars} chars, shown in full]\n{self.preview}")
        return (
            f"[artifact {self.id} | {self.kind} | by {self.author} | "
            f"{self.lines} lines, {self.chars} chars]\n"
            f"{self.preview}\n"
            f"[...fetch artifact {self.id} for the full text]"
        )


class ArtifactStore:
    """Content-addressed store on disk. Append-only; nothing is ever removed.

    Content addressing means an identical output stored twice costs one file
    and yields one reference, which matters because several brain-trust members
    reviewing the same artifact will often quote it back verbatim.
    """

    def __init__(self, root: os.PathLike) -> None:
        self.root = Path(root)
        # Raw model output: closed to other users from the start, like the
        # files in it.
        make_private_dir(self.root)

    @staticmethod
    def _validate(artifact_id):
        if not isinstance(artifact_id, str) or not re.fullmatch(r"[0-9a-f]{12}", artifact_id):
            raise KeyError("invalid artifact id")

    def _path(self, artifact_id: str) -> Path:
        self._validate(artifact_id)
        return self.root / f"{artifact_id}.txt"

    def _meta_path(self, artifact_id: str) -> Path:
        self._validate(artifact_id)
        return self.root / f"{artifact_id}.json"

    @staticmethod
    def _stored(directory, name) -> Optional[bytes]:
        """The bytes at ``name``, or None when nothing is there. A link or
        anything but a regular file is an OSError: never accept an unsafe link."""
        try:
            fd = os.open(name, os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDONLY, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError('artifact is not a regular file')
            return stream.read()

    def _is_reference(self, artifact_id, stored: bytes) -> bool:
        """Whether ``stored`` is a whole reference to this artifact."""
        try:
            return ArtifactRef(**json.loads(stored.decode('utf-8'))).id == artifact_id
        except (ValueError, TypeError):
            return False

    def _io(self, artifact_id, suffix, content=None, *, first_wins=False):
        """Directory-relative I/O: neither reads nor writes follow a leaf symlink.

        A write leaves the name holding exactly ``content``. A new name
        appears whole or not at all. A name that already exists is compared,
        not trusted: a crash once left a short file at an artifact's id, and
        every later ``put`` took it for the content and ``get`` returned the
        fragment for good (whole-repo review, 2026-11-10). Bytes that differ
        are replaced through a temporary file and a rename. ``first_wins``
        is for the original reference, which names the first contributor on
        purpose: it is replaced only when it is not a readable reference.
        """
        self._validate(artifact_id)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            name = artifact_id + suffix
            if content is not None:
                data = content.encode('utf-8')
                stored = self._stored(directory, name)
                if stored is None and not create_in(directory, name, data):
                    stored = self._stored(directory, name)  # another writer got there first
                if stored is not None and stored != data and not (
                        first_wins and self._is_reference(artifact_id, stored)):
                    replace_in(directory, name, data)
                return content
            fd = os.open(name, os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDONLY, dir_fd=directory)
            with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError('artifact is not a regular file')
                return stream.read()
        finally:
            os.close(directory)

    def put(self, content: str, *, kind: str, author: str) -> ArtifactRef:
        """Store raw output and return a reference to it."""
        if content is None:
            content = ""
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        lines = content.splitlines()
        preview = "\n".join(lines[:PREVIEW_LINES])
        ref = ArtifactRef(
            id=digest,
            kind=kind,
            author=author,
            lines=len(lines),
            chars=len(content),
            preview=preview,
        )
        # Content is deduplicated; every distinct provenance is also durable.
        metadata = json.dumps(asdict(ref), ensure_ascii=False, sort_keys=True)
        source_id = hashlib.sha256(metadata.encode()).hexdigest()[:12]
        self._io(digest, ".txt", content)
        self._io(digest, ".json", metadata, first_wins=True)  # original reference, for old callers
        self._io(digest, f".{source_id}.json", metadata)
        return ref

    def get(self, ref_or_id) -> str:
        """Fetch the full original. Raises KeyError if it is not stored."""
        artifact_id = ref_or_id.id if isinstance(ref_or_id, ArtifactRef) else ref_or_id
        try:
            return self._io(artifact_id, ".txt")
        except (OSError, UnicodeError) as exc:
            raise KeyError(f"no safe artifact {artifact_id!r} in {self.root}") from exc

    def ref(self, artifact_id: str) -> Optional[ArtifactRef]:
        """Recover the original reference. Use refs() for all contributors."""
        try:
            return ArtifactRef(**json.loads(self._io(artifact_id, ".json")))
        except (KeyError, OSError, ValueError, TypeError):
            return None

    def refs(self, artifact_id: str) -> List[ArtifactRef]:
        """All recorded authors/kinds of this content, including old stores."""
        original = self.ref(artifact_id)
        if original is None:
            return []
        records = {json.dumps(asdict(original), ensure_ascii=False, sort_keys=True): original}
        for path in self.root.glob(f"{artifact_id}.*.json"):
            source_id = path.name.split('.')[1]
            try:
                self._validate(source_id)
                data = self._io(artifact_id, f".{source_id}.json")
                records[data] = ArtifactRef(**json.loads(data))
            except (KeyError, OSError, ValueError, TypeError):
                continue
        return list(records.values()) or [original]

    def ids(self) -> List[str]:
        return sorted(p.stem for p in self.root.glob("*.txt")
                      if re.fullmatch(r"[0-9a-f]{12}", p.stem)
                      and not p.is_symlink() and p.is_file())
