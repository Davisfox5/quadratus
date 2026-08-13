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

    def _path(self, artifact_id: str) -> Path:
        return self.root / f"{artifact_id}.txt"

    def _meta_path(self, artifact_id: str) -> Path:
        return self.root / f"{artifact_id}.json"

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
        path = self._path(digest)
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            self._meta_path(digest).write_text(json.dumps(asdict(ref)), encoding="utf-8")
        return ref

    def get(self, ref_or_id) -> str:
        """Fetch the full original. Raises KeyError if it is not stored."""
        artifact_id = ref_or_id.id if isinstance(ref_or_id, ArtifactRef) else ref_or_id
        path = self._path(artifact_id)
        if not path.exists():
            raise KeyError(f"no artifact {artifact_id!r} in {self.root}")
        return path.read_text(encoding="utf-8")

    def ref(self, artifact_id: str) -> Optional[ArtifactRef]:
        """Recover a stored reference by id, or None."""
        meta = self._meta_path(artifact_id)
        if not meta.exists():
            return None
        return ArtifactRef(**json.loads(meta.read_text(encoding="utf-8")))

    def ids(self) -> List[str]:
        return sorted(p.stem for p in self.root.glob("*.txt"))
