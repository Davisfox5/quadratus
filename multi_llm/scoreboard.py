"""The operator's scoreboard: whose feedback actually holds up.

Reviewer attribution was recorded from the start -- critiques are anonymous
to the lead but filed under their author's real name (``review:<key>``,
``recheck:<key>``) -- precisely so this report could exist. The registry says
quality ordering within a capability "is what the run scoreboard is for";
this is that scoreboard.

What it counts, and what each count means:

* **reviews / no-findings** -- how often a reviewer was drawn, and how often
  it found nothing. A reviewer that always has findings and one that never
  does are both suspicious, in opposite directions.
* **blocking raised** -- how often it staked a must-fix claim.
* **rechecks resolved / upheld** -- when the lead revised, did the reviewer
  accept the fix (``RESOLVED``) or stand its ground? A reviewer whose
  blocking findings routinely evaporate on first recheck was raising noise;
  one that stands its ground past the fix cap was either right and ignored
  or wrong and stubborn -- the artifacts, which the ids point to, settle
  which.

Deliberately observational, like the meter: this report ranks nothing
automatically and feeds no routing decision by itself. It gives the operator
measured evidence for the pin/unpin decisions that ``task_kinds`` records --
which is where a conclusion drawn from this data belongs, with its evidence
named.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from .registry import resolve

__all__ = ["ReviewerStats", "build_scoreboard", "render_scoreboard"]


@dataclass
class ReviewerStats:
    key: str
    reviews: int = 0
    no_findings: int = 0
    blocking_raised: int = 0
    rechecks: int = 0
    rechecks_resolved: int = 0
    consults_answered: int = 0
    artifact_ids: List[str] = field(default_factory=list)


def _iter_meta(store_root: Path):
    for meta_path in sorted(store_root.glob("*.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(meta, dict) and "kind" in meta and "id" in meta:
            yield meta


def build_scoreboard(store_root) -> Dict[str, ReviewerStats]:
    """Aggregate reviewer behaviour from the artifact store's metadata.

    Reads only the sidecar metadata plus the review/recheck bodies it needs;
    the full artifacts stay where they are, fetchable by id when a number
    here needs its evidence examined.
    """
    root = Path(store_root)
    stats: Dict[str, ReviewerStats] = {}

    def _for(key: str) -> ReviewerStats:
        return stats.setdefault(key, ReviewerStats(key=key))

    for meta in _iter_meta(root):
        kind = str(meta["kind"])
        if ":" not in kind:
            continue
        role, key = kind.split(":", 1)
        try:
            body = (root / f"{meta['id']}.txt").read_text(encoding="utf-8")
        except OSError:
            body = ""
        if role == "review":
            entry = _for(key)
            entry.reviews += 1
            entry.artifact_ids.append(meta["id"])
            if body.strip().upper().rstrip(".") == "NO FINDINGS":
                entry.no_findings += 1
            elif "BLOCKING" in body.upper():
                entry.blocking_raised += 1
        elif role == "recheck":
            entry = _for(key)
            entry.rechecks += 1
            entry.artifact_ids.append(meta["id"])
            if body.strip().upper().startswith("RESOLVED"):
                entry.rechecks_resolved += 1
        elif role == "consult":
            _for(key).consults_answered += 1
    return stats


def render_scoreboard(stats: Dict[str, ReviewerStats]) -> str:
    if not stats:
        return (
            "No reviewer activity recorded yet. The scoreboard fills as runs "
            "complete; artifacts of kind review:<model> and recheck:<model> "
            "are its input."
        )
    lines = ["# Reviewer scoreboard (from the artifact record)", ""]
    for key in sorted(stats, key=lambda k: -stats[k].reviews):
        s = stats[key]
        spec = resolve(key)
        label = spec.label if spec else key
        lines.append(f"## {label} ({key})")
        lines.append(
            f"- reviews: {s.reviews} ({s.no_findings} clean, "
            f"{s.blocking_raised} with blocking findings)"
        )
        if s.rechecks:
            lines.append(
                f"- rechecks: {s.rechecks}, accepted the fix in "
                f"{s.rechecks_resolved} ({s.rechecks_resolved / s.rechecks:.0%})"
            )
        if s.consults_answered:
            lines.append(f"- consults answered: {s.consults_answered}")
        lines.append("")
    lines.append(
        "_Counts, not verdicts: before pinning or unpinning a reviewer, read "
        "the artifacts behind the numbers -- every id is in the store._"
    )
    return "\n".join(lines)
