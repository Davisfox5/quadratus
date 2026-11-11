"""Read one or more Quadratus scoreboards and print who held up.

A scoreboard is ``.quadratus/scoreboard.jsonl`` in a project: one JSON line
per contest (two models drafted the same task blind; one draft was merged)
and one per review a seat gave. This tool only reads. It imports nothing
from ``quadratus``, like ``tools/truth_check.py``: a bug in the engine's
accounting must not also be a bug in the reading of it. No model is called
and nothing is written.

Per model it prints contests entered, won and lost, and what decided each
(completed, scope, gate, judge, size, default). Per reviewer it prints
reviews given, complete verdicts, BLOCKING findings raised, and what the
re-check said of them. A reviewer answers the re-check for its findings as a
set, so "resolved" and "unresolved" count whole sets: RESOLVED counts every
finding of that review as resolved, UNRESOLVED counts every one as still
standing. A missing file, a line that is not a JSON object, or a line of an
unknown type is skipped and counted, never fatal.

These are counts from a few runs, not a ranking. A win decided by "default"
or "size" says little about quality; one decided by "gate" says a draft
passed the project's own checks where the other did not.

Usage: ``python -I tools/scoreboard.py [FILE_OR_PROJECT ...] [--json]``.
With no argument it reads ``.quadratus/scoreboard.jsonl`` under the current
folder. A folder argument is read as a project. Exit status 0, or 1 when
nothing could be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DECIDERS = ("completed", "scope", "gate", "judge", "size", "default")
#: What a contest records when the routed lead's own refusal or denied
#: capability stopped the run and no draft was taken (contest.ROUTED_STOP;
#: spelled here because this tool imports nothing from the engine).
ROUTED_STOP = "routed lead's stop"
#: How much of one scoreboard is read. A record far past this is not one a
#: run wrote.
MAX_BYTES = 64 * 1024 * 1024


def _path(argument) -> Path:
    path = Path(argument).expanduser()
    return path / ".quadratus" / "scoreboard.jsonl" if path.is_dir() else path


def read(paths):
    """``(rows, skipped)``: every well-formed contest or review line, and
    how many files and lines were skipped, by reason."""
    rows, skipped = [], {"missing_files": 0, "malformed_lines": 0, "unknown_lines": 0}
    for argument in paths:
        path = _path(argument)
        try:
            if not path.is_file() or path.is_symlink():
                raise OSError("not a regular file")
            with open(path, "rb") as handle:
                data = handle.read(MAX_BYTES + 1)
        except OSError:
            skipped["missing_files"] += 1
            continue
        if len(data) > MAX_BYTES:
            # The cut may fall inside a line; that line is dropped below.
            data = data[:MAX_BYTES].rsplit(b"\n", 1)[0]
            skipped["malformed_lines"] += 1
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                skipped["malformed_lines"] += 1
                continue
            if not isinstance(row, dict):
                skipped["malformed_lines"] += 1
            elif row.get("type") not in ("contest", "review"):
                skipped["unknown_lines"] += 1
            else:
                rows.append(row)
    return rows, skipped


def _count(value) -> int:
    return value if type(value) is int and value >= 0 else 0


def summarise(rows) -> dict:
    """Per-model contest results and per-reviewer findings, from ``rows``."""
    models, reviewers, malformed = {}, {}, 0
    for row in rows:
        if row.get("type") == "contest":
            drafts = [d for d in row.get("drafts") or [] if isinstance(d, dict) and isinstance(d.get("model"), str)]
            winner, decided = row.get("winner_model"), row.get("decided_by")
            if len(drafts) != 2:
                malformed += 1
                continue
            for draft in drafts:
                entry = models.setdefault(draft["model"], dict(
                    contests=0, won=0, lost=0, no_winner=0, routed_stop=0,
                    won_by={k: 0 for k in DECIDERS}, lost_by={k: 0 for k in DECIDERS}))
                entry["contests"] += 1
                if not isinstance(winner, str) or not winner:
                    # Two ways a contest has no winner: neither draft was
                    # usable, or the routed lead's own stop ended the run.
                    entry["routed_stop" if decided == ROUTED_STOP else "no_winner"] += 1
                    continue
                side = "won" if draft["model"] == winner else "lost"
                entry[side] += 1
                if decided in DECIDERS:
                    entry[side + "_by"][decided] += 1
        else:
            model = row.get("model")
            if not isinstance(model, str) or not model:
                malformed += 1
                continue
            entry = reviewers.setdefault(model, dict(
                reviews=0, complete=0, blocking_raised=0, blocking_resolved=0,
                blocking_unresolved=0, not_rechecked=0))
            raised = _count(row.get("blocking_raised"))
            entry["reviews"] += 1
            entry["complete"] += 1 if row.get("complete") is True else 0
            entry["blocking_raised"] += raised
            entry["blocking_resolved"] += _count(row.get("blocking_resolved"))
            entry["blocking_unresolved"] += _count(row.get("blocking_unresolved"))
            if raised and row.get("recheck") not in ("resolved", "unresolved"):
                entry["not_rechecked"] += raised
    return dict(models=models, reviewers=reviewers, malformed_rows=malformed)


def _how(decided: dict) -> str:
    parts = [f"{key} {n}" for key, n in decided.items() if n]
    return " (" + ", ".join(parts) + ")" if parts else ""


def render(summary, skipped) -> str:
    lines = ["Contests (two blind drafts of one task; one merged)"]
    if not summary["models"]:
        lines.append("  none recorded")
    for model in sorted(summary["models"]):
        entry = summary["models"][model]
        lines.append(f"  {model}: {entry['contests']} entered, {entry['won']} won{_how(entry['won_by'])}, "
                     f"{entry['lost']} lost{_how(entry['lost_by'])}"
                     + (f", {entry['no_winner']} with no usable draft" if entry["no_winner"] else "")
                     + (f", {entry['routed_stop']} ended by the routed lead's refusal or denied capability"
                        if entry["routed_stop"] else ""))
    lines += ["", "Reviews (BLOCKING findings raised, and what the re-check said of them)"]
    if not summary["reviewers"]:
        lines.append("  none recorded")
    for model in sorted(summary["reviewers"]):
        entry = summary["reviewers"][model]
        lines.append(f"  {model}: {entry['reviews']} reviews, {entry['complete']} with a complete verdict, "
                     f"{entry['blocking_raised']} blocking raised, {entry['blocking_resolved']} resolved at "
                     f"the re-check, {entry['blocking_unresolved']} still standing, "
                     f"{entry['not_rechecked']} not re-checked")
    dropped = dict(skipped, malformed_rows=summary["malformed_rows"])
    if any(dropped.values()):
        lines += ["", "Skipped: " + ", ".join(f"{n} {name.replace('_', ' ')}" for name, n in dropped.items() if n)]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", help="scoreboard files or project folders")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    paths = args.paths or ["."]
    rows, skipped = read(paths)
    summary = summarise(rows)
    if args.json:
        print(json.dumps(dict(summary, skipped=skipped, rows=len(rows)), indent=2, sort_keys=True))
    else:
        print(render(summary, skipped))
    return 1 if skipped["missing_files"] == len(paths) else 0


if __name__ == "__main__":
    sys.exit(main())
