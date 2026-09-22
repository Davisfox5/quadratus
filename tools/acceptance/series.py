"""Run a canary series and aggregate its run records into one report.

NEXT.md asks for five runs per version before "live reliability not measured"
can come off, and a per-run table beside the pass rate. Assembling that by hand
from nine files per run is where numbers get invented: a missing budget read
as zero, cached input added to input that already contains it, a pass rate
quoted as a percentage of two. This tool reads the records as run_project
wrote them and says "missing" wherever a record is missing.

``aggregate`` never changes a run directory. ``run`` calls run_fixture.py once
per fresh fixture copy, sequentially, and never retries: a failed run is data.

Layout. Each run directory is a ``.quadratus/runs/<id>`` tree. Its label lives
in a sidecar ``series.json`` ({"version", "runtime_commit", "attempt"}) and the
external grader's output in ``grader.txt``; both are looked for in the run
directory, then beside it (the evidence bundles keep grader.txt one level up).
The report is an index: every row names the run directory it came from.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

VERSIONS = ("baseline", "candidate")
MAX_COUNT = 5
RELIABLE_N = 5
STDERR_PREVIEW = 200
GRADER_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")


def _read_json(path: Path, missing: list):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        missing.append(path.name)
    except (OSError, ValueError) as exc:
        missing.append(f"{path.name} (unreadable: {type(exc).__name__})")
    return None


def _read_jsonl(path: Path, missing: list):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        missing.append(path.name)
        return None
    rows = []
    for line in text.splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                missing.append(f"{path.name} (unparseable row)")
                return None
    return rows


def _beside(run_dir: Path, name: str):
    """The run directory first, then its parent unless the parent is the shared
    ``runs/`` folder, where a file could belong to any run."""
    bases = [run_dir] if run_dir.parent.name == "runs" else [run_dir, run_dir.parent]
    return next((b / name for b in bases if (b / name).is_file()), None)


def parse_grader(text: str):
    """The pytest summary line as counts. A line with neither count is not a
    result, and None says so rather than reporting 0 of 0."""
    for line in reversed(text.splitlines()):
        counts = {kind.rstrip("s"): int(n) for n, kind in GRADER_COUNT.findall(line)}
        if "passed" in counts or "failed" in counts:
            return {"passed": counts.get("passed", 0),
                    "failed": counts.get("failed", 0) + counts.get("error", 0),
                    "summary": line.strip(" =")}
    return None


def diff_paths(diff: str) -> list:
    """Paths a unified diff touches, from its ---/+++ header pairs. Only a
    ``---`` line directly followed by ``+++`` counts, so a removed line that
    happens to start with two dashes is not read as a path."""
    seen = []
    lines = diff.splitlines()
    for old, new in zip(lines, lines[1:], strict=False):
        if not (old.startswith("--- ") and new.startswith("+++ ")):
            continue
        for header in (old, new):
            path = re.sub(r"^[ab]/", "", header[4:].split("\t")[0].strip())
            if path != "/dev/null" and path not in seen:
                seen.append(path)
    return seen


def _input_split(rows: list):
    """Sum input over invoked rows, split into cached and fresh.

    The ledger normalises input to include cached input (budget.json says so),
    so fresh is input minus cached, never the input total. The top-level
    ``cached_input_tokens`` is read first; the Q9 records carry the vendor's
    figure only in ``diagnostics``, which delegation.py documents as the same
    subset. One row with no cached figure makes the run's fresh count unknown:
    a partial sum would read as a measured total.
    """
    total = cached = 0
    input_unknown = cached_unknown = 0
    for row in rows:
        if not row.get("invoked"):
            continue
        value = row.get("input_tokens")
        if value is None:
            input_unknown += 1
            continue
        total += value
        hit = row.get("cached_input_tokens")
        if hit is None:
            hit = (row.get("diagnostics") or {}).get("cached_input_tokens")
        if hit is None:
            cached_unknown += 1
        else:
            cached += hit
    known = input_unknown == 0 and cached_unknown == 0
    return {"input": total if input_unknown == 0 else None,
            "cached": cached if known else None,
            "fresh": total - cached if known else None,
            "rows_input_unknown": input_unknown,
            "rows_cached_unknown": cached_unknown}


def summarise_run(run_dir: Path) -> dict:
    """One run's facts. A field whose source file is missing is None, and the
    file is listed under ``missing``."""
    run_dir = Path(run_dir)
    missing: list = []
    side_path = _beside(run_dir, "series.json")
    sidecar = _read_json(side_path, missing) if side_path else None
    if side_path is None:
        missing.append("series.json")
    budget = _read_json(run_dir / "budget.json", missing) or {}
    result = _read_json(run_dir / "result.json", missing)
    rows = _read_jsonl(run_dir / "invocations.jsonl", missing)
    plan_path = run_dir / "policy-plan.json"
    plan = _read_json(plan_path, []) if plan_path.is_file() else None
    try:
        changed = diff_paths((run_dir / "changes.diff").read_text(encoding="utf-8"))
    except FileNotFoundError:
        missing.append("changes.diff")
        changed = None
    grader_path = _beside(run_dir, "grader.txt")
    grader = parse_grader(grader_path.read_text(encoding="utf-8")) if grader_path else None
    if grader_path is None:
        missing.append("grader.txt")

    if changed is None:
        declared = "missing changes.diff"
    elif plan is None:
        declared = "no policy plan"
    else:
        allowed = set(plan.get("declared_paths") or [])
        outside = [p for p in changed if p not in allowed]
        declared = {"ok": not outside, "undeclared": outside}

    notes = []
    for row in rows or []:
        if row.get("stderr_tail") or row.get("tool_failures"):
            notes.append({
                "role": row.get("role"),
                "model": row.get("resolved_model") or row.get("requested_model"),
                "stderr_tail": (row.get("stderr_tail") or "")[:STDERR_PREVIEW],
                "tool_failures": [{"command": f.get("command"), "exit_code": f.get("exit_code")}
                                  for f in row.get("tool_failures") or []],
            })
    return {
        "run_dir": str(run_dir),
        "version": (sidecar or {}).get("version"),
        "runtime_commit": (sidecar or {}).get("runtime_commit"),
        "attempt": (sidecar or {}).get("attempt"),
        "completed": result.get("completed") if result else None,
        "provider_attempts": budget.get("reserved_attempts"),
        "reported_tokens": budget.get("reported_tokens"),
        "unknown_usage_attempts": budget.get("unknown_usage_attempts"),
        "wall_seconds": budget.get("elapsed_seconds"),
        "input_tokens": _input_split(rows) if rows is not None else None,
        "grader": grader if grader_path else "missing",
        "only_declared_paths": declared,
        "changed_paths": changed,
        "roles": [r.get("role") for r in rows if r.get("invoked")] if rows is not None else None,
        "cli_evidence": notes if rows is not None else None,
        "policy_plan": plan_path.is_file(),
        "missing": missing,
    }


def _median(values: list):
    known = [v for v in values if v is not None]
    return {"value": statistics.median(known) if known else None,
            "known": len(known), "of": len(values)}


def label(n: int) -> str:
    if n >= RELIABLE_N:
        return f"live reliability: {n} runs per version"
    return "controller determinism, not live reliability"


def summarise_version(runs: list) -> dict:
    graded = [r for r in runs if isinstance(r["grader"], dict)]
    passed = [r for r in graded if r["grader"]["failed"] == 0 and r["grader"]["passed"] > 0]
    return {
        "runs": len(runs),
        "completed": sum(1 for r in runs if r["completed"] is True),
        "completed_unknown": sum(1 for r in runs if r["completed"] is None),
        "pass_rate": f"{len(passed)} of {len(runs)}",
        "ungraded": len(runs) - len(graded),
        "median_attempts": _median([r["provider_attempts"] for r in runs]),
        "median_reported_tokens": _median([r["reported_tokens"] for r in runs]),
        "label": label(len(runs)),
    }


def aggregate(run_dirs: list) -> dict:
    runs = [summarise_run(Path(d)) for d in run_dirs]
    groups = {}
    for run in runs:
        groups.setdefault(run["version"] or "unlabelled", []).append(run)
    versions = {name: summarise_version(group) for name, group in groups.items()}
    smallest = min((len(groups.get(v, [])) for v in VERSIONS), default=0)
    return {"label": label(smallest), "versions": versions, "runs": runs}


def _show(value, source: str) -> str:
    return f"missing ({source})" if value is None else str(value)


def render(report: dict) -> str:
    out = ["# Canary series", "", f"**{report['label']}**", "",
           "Each row indexes a run directory; the raw records there are the evidence.", "",
           "## Per version", "",
           "| version | runs | completed | pass rate | median attempts | median tokens | label |",
           "| --- | --- | --- | --- | --- | --- | --- |"]
    for name, v in report["versions"].items():
        med = [f"{_show(m['value'], 'no data')} ({m['known']} of {m['of']} known)"
               for m in (v["median_attempts"], v["median_reported_tokens"])]
        out.append(f"| {name} | {v['runs']} | {v['completed']} of {v['runs']} | "
                   f"{v['pass_rate']} ({v['ungraded']} ungraded) | {med[0]} | {med[1]} | "
                   f"{v['label']} |")
    out += ["", "## Per run", ""]
    for r in report["runs"]:
        split = r["input_tokens"]
        if split is None:
            tokens = "missing (invocations.jsonl)"
        else:
            tokens = (f"input {_show(split['input'], 'unknown')}, cached "
                      f"{_show(split['cached'], 'unknown')}, fresh "
                      f"{'unknown' if split['fresh'] is None else split['fresh']}"
                      f" ({split['rows_cached_unknown']} rows without a cached figure)")
        grader = r["grader"]
        if isinstance(grader, dict):
            grader = f"{grader['passed']} passed, {grader['failed']} failed"
        elif grader is None:
            grader = "grader.txt has no pytest summary line"
        roles = ("missing (invocations.jsonl)" if r["roles"] is None
                 else " > ".join(map(str, r["roles"])) or "none")
        declared = r["only_declared_paths"]
        if isinstance(declared, dict):
            declared = "yes" if declared["ok"] else f"no: {', '.join(declared['undeclared'])}"
        out += [f"### {r['version'] or 'unlabelled'} attempt {_show(r['attempt'], 'series.json')}",
                "", f"- run directory: `{r['run_dir']}`",
                f"- runtime commit: {_show(r['runtime_commit'], 'series.json')}",
                f"- completed: {_show(r['completed'], 'result.json')}",
                f"- provider attempts: {_show(r['provider_attempts'], 'budget.json')}",
                f"- reported tokens: {_show(r['reported_tokens'], 'budget.json')}",
                f"- input tokens: {tokens}",
                f"- unknown-usage attempts: {_show(r['unknown_usage_attempts'], 'budget.json')}",
                f"- wall seconds: {_show(r['wall_seconds'], 'budget.json')}",
                f"- grader: {grader}",
                f"- only declared paths changed: {declared}",
                f"- policy plan present: {'yes' if r['policy_plan'] else 'no'}",
                f"- roles invoked: {roles}"]
        if r["cli_evidence"]:
            out.append("- CLI evidence rows:")
            for row in r["cli_evidence"]:
                out.append(f"  - {row['role']} on {row['model']}: stderr `{row['stderr_tail']}`")
                for f in row["tool_failures"]:
                    out.append(f"    - tool failure: `{f['command']}` exit {f['exit_code']}")
        else:
            out.append("- CLI evidence rows: none")
        if r["missing"]:
            out.append(f"- missing files: {', '.join(r['missing'])}")
        out.append("")
    return "\n".join(out)


def cmd_aggregate(args) -> int:
    report = aggregate(args.runs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "series-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "series-report.md").write_text(render(report), encoding="utf-8")
    print(f"wrote {out / 'series-report.md'} and series-report.json ({len(report['runs'])} runs)")
    return 0


def check_allowance(path) -> dict:
    """The same test run_fixture.py applies, before any copy is made."""
    if not path:
        raise SystemExit("A direct Davis allowance record is required")
    try:
        allowance = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"A direct Davis allowance record is required ({exc})") from exc
    if allowance.get("authorized_by") != "Davis" or not allowance.get("source"):
        raise SystemExit("A direct Davis allowance record is required")
    return allowance


def _commit(runtime: Path) -> str:
    try:
        done = subprocess.run(["git", "-C", str(runtime), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return done.stdout.strip() if done.returncode == 0 else "unknown"


def cmd_run(args) -> int:
    check_allowance(args.allowance_record)
    if not 1 <= args.count <= MAX_COUNT:
        raise SystemExit(f"--count must be 1 to {MAX_COUNT} per invocation")
    runtime, fixture, out = Path(args.runtime).resolve(), Path(args.fixture), Path(args.out)
    launcher = Path(args.launcher or runtime / "docs" / "harness-canary" / "run_fixture.py")
    grader = shlex.split(args.grader_command) if args.grader_command else None
    commit = _commit(runtime)
    out.mkdir(parents=True, exist_ok=True)
    start = 1 + len(list(out.glob(f"{args.version}-*")))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        p for p in (str(runtime), os.environ.get("PYTHONPATH", "")) if p))
    collected = []
    for attempt in range(start, start + args.count):
        slot = out / f"{args.version}-{attempt}"
        project = slot / "project"
        shutil.copytree(fixture, project, ignore=shutil.ignore_patterns("runs"))
        argv = [args.python, str(launcher), "--project", str(project),
                "--allowance-record", str(Path(args.allowance_record).resolve())]
        print(f"{args.version} attempt {attempt}: {' '.join(argv)}", flush=True)
        try:
            done = subprocess.run(argv, cwd=runtime, env=env, capture_output=True,
                                  text=True, timeout=args.wall_seconds)
            code, text = done.returncode, done.stdout + done.stderr
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            code, text = None, f"launcher killed after {args.wall_seconds}s\n{partial}"
        (slot / "launcher.txt").write_text(text, encoding="utf-8")
        found = sorted((project / ".quadratus" / "runs").glob("*"))
        # No run tree means the launcher failed before run_project; the sidecar
        # still goes in the slot so aggregate lists this attempt as all-missing.
        run_dir = found[-1] if found else slot
        sidecar = {"version": args.version, "runtime_commit": commit, "attempt": attempt,
                   "launcher_exit_code": code, "run_trees_found": len(found)}
        (run_dir / "series.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
        if grader:
            genv = dict(os.environ, CANARY_PROJECT=str(project))
            graded = subprocess.run(grader, cwd=project, env=genv, capture_output=True, text=True)
            (run_dir / "grader.txt").write_text(graded.stdout + graded.stderr, encoding="utf-8")
        collected.append(str(run_dir))
    with (out / f"{args.version}-runs.txt").open("a", encoding="utf-8") as index:
        index.write("".join(f"{d}\n" for d in collected))
    print("\n".join(collected))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    agg = sub.add_parser("aggregate", help="report on existing run directories")
    agg.add_argument("--runs", nargs="+", required=True)
    agg.add_argument("--out", default=".")
    run = sub.add_parser("run", help="run the canary N times, sequentially, no retries")
    run.add_argument("--version", choices=VERSIONS, required=True)
    run.add_argument("--runtime", required=True, help="the checked-out Quadratus runtime")
    run.add_argument("--fixture", required=True, help="copied fresh for every run")
    run.add_argument("--count", type=int, required=True)
    run.add_argument("--allowance-record")
    run.add_argument("--out", required=True)
    run.add_argument("--grader-command", help="argv string, run in the fixture copy")
    run.add_argument("--python", default=sys.executable)
    run.add_argument("--launcher", help="defaults to <runtime>/docs/harness-canary/run_fixture.py")
    run.add_argument("--wall-seconds", type=int, default=900,
                     help="hard kill per run; matches the external wall in NEXT.md")
    args = parser.parse_args(argv)
    return cmd_aggregate(args) if args.command == "aggregate" else cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
