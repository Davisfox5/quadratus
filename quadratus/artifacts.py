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
        """One-line-plus-preview form for inclusion in a prompt."""
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
        self.root.mkdir(parents=True, exist_ok=True)

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

    def _io(self, artifact_id, suffix, content=None):
        """Directory-relative I/O: neither reads nor writes follow a leaf symlink."""
        self._validate(artifact_id)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            name = artifact_id + suffix
            flags = os.O_NOFOLLOW | os.O_NONBLOCK
            if content is not None:
                try:
                    fd = os.open(name, flags | os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                 0o600, dir_fd=directory)
                except FileExistsError:
                    # Check even an existing record: never accept an unsafe link.
                    fd = os.open(name, flags | os.O_RDONLY, dir_fd=directory)
                    writing = False
                else:
                    writing = True
            else:
                fd = os.open(name, flags | os.O_RDONLY, dir_fd=directory)
                writing = False
            with os.fdopen(fd, 'w' if writing else 'r', encoding='utf-8') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError('artifact is not a regular file')
                if writing:
                    stream.write(content)
                    return content
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
        self._io(digest, ".json", metadata)  # original reference, for old callers
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
