"""Run a canary series and aggregate its run records into one report.

NEXT.md asks for five runs per version before "live reliability not measured"
can come off, and a per-run table beside the pass rate. Assembling that by hand
from nine files per run is where numbers get invented: a missing budget read
as zero, cached input added to input that already contains it, a pass rate
quoted as a percentage of two. This tool reads the records as run_project
wrote them and says "missing" wherever a record is missing.

``aggregate`` never changes a run directory. ``run`` calls run_fixture.py once
per fresh fixture copy, sequentially, and never retries: a failed run is data.
It admits a run only against a Davis allowance record (``allowance.py``): the
runtime commit and wall must match the record, and each run claims a durable
slot beside it, so neither ``--count`` nor a re-run can exceed the record's
runs per version or its batch token ceiling.

Layout. Each run directory is a ``.quadratus/runs/<id>`` tree. Its label lives
in a sidecar ``series.json`` ({"version", "runtime_commit", "attempt"}) and the
external grader's output in ``grader.txt``; both are looked for in the run
directory, then beside it (the evidence bundles keep grader.txt one level up).
The report is an index: every row names the run directory it came from.
"""

from __future__ import annotations

import argparse
import importlib.util
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

# Loaded by path so the tool works both as a script and when a test imports
# this file directly, without either putting tools/acceptance on sys.path.
_allowance_spec = importlib.util.spec_from_file_location(
    "canary_allowance", Path(__file__).resolve().with_name("allowance.py"))
allowance = importlib.util.module_from_spec(_allowance_spec)
_allowance_spec.loader.exec_module(allowance)


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
    if text.startswith("REFUSED:"):
        return {"refused": text.splitlines()[0][len("REFUSED:"):].strip()}
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
        "launcher_exit_code": (sidecar or {}).get("launcher_exit_code"),
        "launched": "launcher_exit_code" in (sidecar or {}),
        "provenance": provenance(sidecar),
        "completed": result.get("completed") if result else None,
        "provider_attempts": budget.get("reserved_attempts"),
        "reported_tokens": budget.get("reported_tokens"),
        "token_stop": (budget.get("limits") or {}).get("max_reported_tokens"),
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
    """Sample size only. Provenance (live launcher run or not) is per run."""
    if n >= RELIABLE_N:
        return f"live reliability: {n} runs per version"
    return f"live sample: {n} runs per version, below the {RELIABLE_N}-run reliability threshold"


def provenance(sidecar) -> str:
    """What made this run. Only a series.json written by ``run`` marks a live
    launcher run; a replay or a hand-built tree has none and reads unknown."""
    if isinstance(sidecar, dict) and "launcher_exit_code" in sidecar:
        return "live launcher run (series.json)"
    return "unknown (no series.json from the runner)"


def summarise_version(runs: list) -> dict:
    """Three facts kept apart, never folded into one rate: whether the launch
    and the instrument were sound (launcher exit 0, grader not refused),
    whether the controller completed (result.json), and what the grader
    measured (its own passed/failed, over graded runs only). The grader
    column is measurement only: a pass means correct code on disk whatever the
    launcher did, and the launch column says separately whether the run that
    put it there was sound. A refused instrument is never graded."""
    sound = [r for r in runs if r["launched"] and r["launcher_exit_code"] == 0
             and not (isinstance(r["grader"], dict) and "refused" in r["grader"])]
    graded = [r for r in runs if isinstance(r["grader"], dict) and "passed" in r["grader"]]
    grader_passed = [r for r in graded if r["grader"]["failed"] == 0 and r["grader"]["passed"] > 0]
    return {
        "runs": len(runs),
        "launch_sound": len(sound),
        "launcher_failed": sum(1 for r in runs if r["launched"] and r["launcher_exit_code"] != 0),
        "launcher_unknown": sum(1 for r in runs if not r["launched"]),
        "instrument_refused": sum(1 for r in runs
                                  if isinstance(r["grader"], dict) and "refused" in r["grader"]),
        "completed": sum(1 for r in runs if r["completed"] is True),
        "completed_unknown": sum(1 for r in runs if r["completed"] is None),
        "graded": len(graded),
        "grader_passed": f"{len(grader_passed)} of {len(graded)} graded",
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


def _stop_note(run: dict) -> str:
    """The run's token figure beside its stop threshold. The threshold is
    checked after each call returns, so it is a stop, not a ceiling: one call
    can carry a run well past it (Q9-v2 rerun, 2026-09-23: a 1,000,000 stop,
    1,701,844 spent)."""
    stop, spent = run.get("token_stop"), run.get("reported_tokens")
    if stop is None or spent is None:
        return ""
    over = f"; {spent - stop} over it" if spent > stop else ""
    return f" (stop threshold {stop}, checked after each call returns, not a ceiling{over})"


def _show(value, source: str) -> str:
    return f"missing ({source})" if value is None else str(value)


def render(report: dict) -> str:
    out = ["# Canary series", "", f"**{report['label']}**", "",
           "Each row indexes a run directory; the raw records there are the evidence.", "",
           "## Per version", "",
           "Three outcomes per version, kept apart: launch and instrument (launcher exit 0,",
           "grader not refused), controller completion (result.json), and what the grader",
           "measured. None of them is an overall success on its own.", "",
           "| version | runs | launch sound | launcher failed | instrument refused | "
           "controller completed | grader passed | median attempts | median tokens | label |",
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, v in report["versions"].items():
        med = [f"{_show(m['value'], 'no data')} ({m['known']} of {m['of']} known)"
               for m in (v["median_attempts"], v["median_reported_tokens"])]
        out.append(f"| {name} | {v['runs']} | {v['launch_sound']} of {v['runs']} | "
                   f"{v['launcher_failed']} ({v['launcher_unknown']} unknown) | "
                   f"{v['instrument_refused']} | "
                   f"{v['completed']} of {v['runs']} ({v['completed_unknown']} unknown) | "
                   f"{v['grader_passed']} ({v['ungraded']} ungraded) | {med[0]} | {med[1]} | "
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
        if isinstance(grader, dict) and "refused" in grader:
            grader = f"refused, not run: {grader['refused']}"
        elif isinstance(grader, dict):
            grader = f"{grader['passed']} passed, {grader['failed']} failed"
        elif grader is None:
            grader = "grader.txt has no pytest summary line"
        if r["launched"]:
            code = r["launcher_exit_code"]
            launch = "killed at the wall" if code is None else f"exit {code}"
        else:
            launch = "missing (series.json)"
        roles = ("missing (invocations.jsonl)" if r["roles"] is None
                 else " > ".join(map(str, r["roles"])) or "none")
        declared = r["only_declared_paths"]
        if isinstance(declared, dict):
            declared = "yes" if declared["ok"] else f"no: {', '.join(declared['undeclared'])}"
        out += [f"### {r['version'] or 'unlabelled'} attempt {_show(r['attempt'], 'series.json')}",
                "", f"- run directory: `{r['run_dir']}`",
                f"- runtime commit: {_show(r['runtime_commit'], 'series.json')}",
                f"- launcher: {launch}",
                f"- provenance: {r['provenance']}",
                f"- completed: {_show(r['completed'], 'result.json')}",
                f"- provider attempts: {_show(r['provider_attempts'], 'budget.json')}",
                f"- reported tokens: {_show(r['reported_tokens'], 'budget.json')}{_stop_note(r)}",
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


def _commit(runtime: Path) -> str:
    try:
        done = subprocess.run(["git", "-C", str(runtime), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return done.stdout.strip() if done.returncode == 0 else "unknown"


def _launch(args, runtime: Path, launcher: Path, project: Path, record_path: Path, env):
    """One launcher call under the external wall. Returns (exit code, output)."""
    argv = [args.python, str(launcher), "--project", str(project),
            "--allowance-record", str(record_path)]
    print(f"{args.version}: {' '.join(argv)}", flush=True)
    # The launcher is the head of a process tree (the vendor CLIs are its
    # children), so the wall kills the whole session group, not only the
    # head; a killed head with live CLI children would keep spending the
    # window after the run was declared over.
    proc = subprocess.Popen(argv, cwd=runtime, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        text, _ = proc.communicate(timeout=args.wall_seconds)
        return proc.returncode, text
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        partial, _ = proc.communicate()
        return None, f"launcher killed after {args.wall_seconds}s\n{partial or ''}"


def cmd_run(args) -> int:
    record = allowance.load_record(args.allowance_record)
    record_path = Path(args.allowance_record).resolve()
    if not 1 <= args.count <= MAX_COUNT:
        raise SystemExit(f"--count must be 1 to {MAX_COUNT} per invocation")
    runtime, fixture, out = Path(args.runtime).resolve(), Path(args.fixture), Path(args.out)
    grader_file = allowance.check_grader(record, fixture)
    launcher = Path(args.launcher or runtime / "docs" / "harness-canary" / "run_fixture.py")
    # The final grader is the manifest's file and nothing else: a supplied
    # command must name it, and the default is built from it.
    if args.grader_command:
        grader = shlex.split(args.grader_command)
        # Compared by resolved path, token by token: on macOS /tmp is a symlink
        # to /private/tmp, and a string match refused a correct command.
        named = False
        for token in grader:
            try:
                named = named or Path(token).resolve() == grader_file
            except (OSError, ValueError):
                continue
        if not named:
            raise SystemExit(f"--grader-command must run the fixture's grader {grader_file}")
    else:
        grader = [args.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(grader_file)]
    commit = _commit(runtime)
    allowance.check_runtime(record, args.version, commit)
    allowance.check_wall(record, args.wall_seconds)
    # The slots, not --count, are the real cap: refuse a count the ledger
    # cannot cover before anything is copied or launched.
    left = allowance.slots_left(record_path, record, args.version)
    total = record["runs_per_version"]
    if left == 0:
        raise SystemExit(f"no run slot left for {args.version}: {total} of {total} consumed")
    if args.count > left:
        raise SystemExit(f"--count {args.count} exceeds the {left} {args.version} run slot(s) "
                         f"left of {total} on this allowance record")
    record_digest = allowance.record_sha256(record_path)
    out.mkdir(parents=True, exist_ok=True)
    start = 1 + sum(1 for p in out.glob(f"{args.version}-[0-9]*") if p.is_dir())
    env, removed = allowance.scrub_api_credentials(dict(os.environ, PYTHONPATH=os.pathsep.join(
        p for p in (str(runtime), os.environ.get("PYTHONPATH", "")) if p)))
    if removed:
        print(f"scrubbed API credentials from the child environment: {', '.join(removed)}",
              flush=True)
    collected = []
    failed = None  # (attempt, launcher exit code or None for a wall kill)
    try:
        for attempt in range(start, start + args.count):
            # Claimed and written to the ledger before anything is copied or
            # launched; closed below whatever happens to the launch.
            claim = allowance.claim_slot(record_path, record, args.version)
            slot = out / f"{args.version}-{attempt}"
            project = slot / "project"
            run_dir = slot
            try:
                shutil.copytree(fixture, project, ignore=shutil.ignore_patterns("runs"))
                code, text = _launch(args, runtime, launcher, project, record_path, env)
                (slot / "launcher.txt").write_text(text, encoding="utf-8")
                found = sorted((project / ".quadratus" / "runs").glob("*"))
                # No run tree means the launcher failed before run_project; the
                # sidecar still goes in the slot so aggregate lists this attempt
                # as all-missing, and the slot closes with unknown usage.
                run_dir = found[-1] if found else slot
                sidecar = {"version": args.version, "runtime_commit": commit,
                           "attempt": attempt, "slot": claim["attempt"],
                           "allowance_sha256": record_digest,
                           "launcher_exit_code": code, "run_trees_found": len(found)}
                (run_dir / "series.json").write_text(json.dumps(sidecar, indent=2),
                                                     encoding="utf-8")
            finally:
                allowance.close_slot(record_path, claim, run_dir)
            # Hashed again right before it runs: a grader that changed since
            # admission is not run, and the run reads as ungraded.
            try:
                allowance.verify_grader_bytes(record, grader_file)
            except SystemExit as exc:
                # An instrument that changed is an integrity failure, not a
                # measurement: recorded, and it ends the series below.
                (run_dir / "grader.txt").write_text(f"REFUSED: {exc}\n", encoding="utf-8")
                refused = str(exc)
            else:
                refused = None
                genv = dict(env, CANARY_PROJECT=str(project))
                graded = subprocess.run(grader, cwd=project, env=genv,
                                        capture_output=True, text=True)
                (run_dir / "grader.txt").write_text(graded.stdout + graded.stderr,
                                                    encoding="utf-8")
            collected.append(str(run_dir))
            # A launcher that did not exit 0 (a refusal, an engine error, a
            # wall kill) ends the series here: its records are kept, no next
            # launch happens, and the command's own exit says so.
            if code != 0:
                failed = (attempt, "killed at the wall" if code is None else f"launcher exit {code}")
                break
            if refused is not None:
                failed = (attempt, f"grader integrity refused: {refused}")
                break
    finally:
        if collected:
            with (out / f"{args.version}-runs.txt").open("a", encoding="utf-8") as index:
                index.write("".join(f"{d}\n" for d in collected))
    print("\n".join(collected))
    if failed is not None:
        attempt, why = failed
        print(f"{args.version} attempt {attempt}: {why}; series stopped", flush=True)
        return 1
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
    run.add_argument("--grader-command",
                     help="argv string naming the manifest's grader; default runs it with --python")
    run.add_argument("--python", default=sys.executable)
    run.add_argument("--launcher", help="defaults to <runtime>/docs/harness-canary/run_fixture.py")
    run.add_argument("--wall-seconds", type=int, default=900,
                     help="hard kill per run; matches the external wall in NEXT.md")
    args = parser.parse_args(argv)
    return cmd_aggregate(args) if args.command == "aggregate" else cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
