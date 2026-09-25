"""Rendered evidence for design work: the lead captures it, the harness checks it.

Davis's ruling (2026-09-25): design work is verified by the model that did it,
and cross-checked by another. Codex's review of the first version: asking for
a look is not verification. So the lead captures the page at a desktop and a
mobile width with this module, and the harness checks the files -- real PNGs,
the right widths, written during this task -- before the task can count as
complete. The cross-vendor design reviewer is pointed at the same files.

    python -m quadratus.design_evidence <url-or-html-file> <task-id> [project-root]
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from typing import Optional, Tuple

VIEWPORTS = {"desktop": {"width": 1280, "height": 800}, "mobile": {"width": 390, "height": 844}}
EVIDENCE_DIR = Path(".quadratus") / "design-evidence"


def evidence_dir(root, task_id: str) -> Path:
    return Path(root) / EVIDENCE_DIR / task_id


def capture(target: str, task_id: str, root=".") -> dict:
    """Render ``target`` at each width into the task's evidence folder."""
    from .browser import render_page
    out = {}
    for name, viewport in VIEWPORTS.items():
        evidence = render_page(target, out_dir=evidence_dir(root, task_id) / name, viewport=viewport)
        out[name] = dict(screenshot=evidence.screenshot_path, clean=evidence.clean,
                         console_errors=evidence.console_errors[:10], failed_requests=evidence.failed_requests[:10])
    (evidence_dir(root, task_id) / "summary.json").write_text(json.dumps(out, indent=2))
    return out


def _png_width(path: Path) -> Optional[int]:
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">I", head[16:20])[0]


def check(root, task_id: str, since: float) -> Tuple[bool, str, list]:
    """Whether the task left fresh desktop and mobile renders. Never raises."""
    folder = evidence_dir(root, task_id)
    problems, shots = [], []
    for name, viewport in VIEWPORTS.items():
        shot = folder / name / "page.png"
        width = _png_width(shot)
        if width is None:
            problems.append(f"no {name} screenshot at {shot.relative_to(root) if shot.is_absolute() else shot}")
            continue
        if shot.stat().st_mtime < since:
            problems.append(f"the {name} screenshot predates this task")
            continue
        if abs(width - viewport["width"]) > 64:
            problems.append(f"the {name} screenshot is {width}px wide, not ~{viewport['width']}px")
            continue
        shots.append(str(shot))
    return not problems, "; ".join(problems), shots


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) not in (2, 3):
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    print(json.dumps(capture(argv[0], argv[1], argv[2] if len(argv) == 3 else "."), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
