"""One approved canary run. The external supervisor enforces the hard wall.

Two lessons from the 2026-09-22 pair are wired in here rather than left in a
report. ``--preflight`` now runs the acceptance preflight (auth, work tree and
each vendor's inner sandbox in the mode a seat would really get), because the
pair's own preflight checked binaries and imports and never asked the one
question that killed both runs. And the console log goes under the state
directory, because a launcher that wrote it into the project root made the
controller report a source change no model had made.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONSOLE = Path(".quadratus") / "live-console.txt"


def _preflight(project: Path, output: Path) -> int:
    """Run tools/acceptance/preflight.py against ``project``; exit 1 on a blocker.

    Imported by path so the same file serves the container image and a native
    host. ``--host`` is passed only when this process is not in a container,
    which the preflight records in its report.
    """
    candidates = [HERE.parents[1] / "tools" / "acceptance" / "preflight.py",
                  Path("/opt/quadratus/tools/acceptance/preflight.py")]
    source = next((c for c in candidates if c.exists()), None)
    if source is None:
        print("preflight tool not found beside this launcher", file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location("acceptance_preflight", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    argv = ["--work", str(project), "--output", str(output)]
    if not Path("/.dockerenv").exists():
        argv.append("--host")
    saved = sys.argv
    sys.argv = ["preflight.py", *argv]
    try:
        return int(module.main() or 0)
    finally:
        sys.argv = saved


SCHEMA = "quadratus-canary-allowance/2"
ENVIRONMENTS = {"native-mac", "container-contained"}
VERSIONS = {"baseline", "candidate"}
_SHA = re.compile(r"[0-9a-f]{40}")
_LIMITS = ("max_calls_each", "max_reported_tokens_each", "internal_wall_seconds_each",
           "external_wall_seconds_each", "max_reported_tokens_batch")
_REQUIRED = ("schema", "approved", "authorized_by", "source", "instruction", "recorded_at",
             "environment", "baseline_sha", "candidate_sha", "runs", "runs_per_version",
             *_LIMITS)


def _refuse(field: str, why: str):
    raise SystemExit(f"allowance record refused at {field}: {why}")


def require_allowance_record(record: dict) -> None:
    """The same field rules as ``tools/acceptance/allowance.py`` on #27.

    Reimplemented here so the launcher checks the record itself. A missing
    key fails closed and the message names that field.
    """
    if not isinstance(record, dict):
        _refuse("record", "not a JSON object")
    for key in _REQUIRED:
        if key not in record:
            _refuse(key, "missing")
    if record["schema"] != SCHEMA:
        _refuse("schema", f"expected {SCHEMA!r}")
    if record["approved"] is not True:
        _refuse("approved", "must be true")
    if record["authorized_by"] != "Davis":
        _refuse("authorized_by", "must be 'Davis'")
    for key in ("source", "recorded_at", "instruction"):
        if not isinstance(record[key], str) or not record[key].strip():
            _refuse(key, "empty")
    if record["environment"] not in ENVIRONMENTS:
        _refuse("environment", f"must be one of {sorted(ENVIRONMENTS)}")
    for key in ("baseline_sha", "candidate_sha"):
        if not isinstance(record[key], str) or not _SHA.fullmatch(record[key]):
            _refuse(key, "must be 40 lowercase hex characters")
    if record["baseline_sha"] == record["candidate_sha"]:
        _refuse("candidate_sha", "equals baseline_sha")
    runs = record["runs"]
    if not isinstance(runs, list) or not runs or not all(r in VERSIONS for r in runs):
        _refuse("runs", f"must be a non-empty list drawn from {sorted(VERSIONS)}")
    per = record["runs_per_version"]
    if not isinstance(per, int) or isinstance(per, bool) or not 1 <= per <= 5:
        _refuse("runs_per_version", "must be an integer from 1 to 5")
    for key in _LIMITS:
        if isinstance(record[key], bool) or not isinstance(record[key], int) or record[key] <= 0:
            _refuse(key, "must be a positive integer")


def runtime_commit(cwd: Path | None = None) -> str:
    """``git rev-parse HEAD`` of the runtime, or ``unknown`` when it cannot be read."""
    try:
        done = subprocess.run(
            ["git", "-C", str(cwd or Path.cwd()), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    commit = done.stdout.strip() if done.returncode == 0 else ""
    return commit or "unknown"


def bind_runtime(record: dict, commit: str) -> str:
    """The version whose named SHA is this runtime. An unknown commit refuses."""
    if not isinstance(commit, str) or not commit.strip() or commit == "unknown":
        raise SystemExit("runtime commit unknown is not a commit the allowance names")
    matched = [v for v in ("baseline", "candidate") if record.get(f"{v}_sha") == commit]
    if len(matched) != 1:
        raise SystemExit(
            f"runtime commit {commit} is not a commit the allowance names "
            f"({record.get('baseline_sha')}, {record.get('candidate_sha')})"
        )
    version = matched[0]
    if version not in record.get("runs", []):
        raise SystemExit(f"the allowance record grants no {version} runs")
    return version


def require_allowance_preflight(allowance: dict, project: Path) -> None:
    """The report must be this launch: ok, no blockers, probe under ``project``.

    ``host`` must be true for ``native-mac`` and ``contained`` must be true for
    ``container-contained``. A blocker list is refused by its first entry.
    """
    raw = allowance.get("preflight_report") if isinstance(allowance, dict) else None
    if "preflight_report" not in allowance or not isinstance(raw, str) or not raw.strip():
        _refuse("preflight_report", "missing")
    path = Path(raw)
    if not path.is_file():
        _refuse("preflight_report", f"not found: {path}")
    try:
        report = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        _refuse("preflight_report", str(exc))
    if not isinstance(report, dict):
        _refuse("preflight_report", "not a JSON object")
    blockers = report.get("blockers")
    if not isinstance(blockers, list):
        _refuse("blockers", "must be a list")
    if blockers:
        _refuse("blockers", str(blockers[0]))
    if report.get("ok") is not True:
        _refuse("ok", "preflight report ok is false")
    probe = report.get("probe_file")
    if not isinstance(probe, str) or not probe.strip():
        _refuse("probe_file", "missing")
    try:
        Path(probe).resolve().relative_to(Path(project).resolve())
    except ValueError:
        _refuse("probe_file", f"{probe} is not under {project}")
    env = allowance.get("environment")
    if env == "native-mac" and report.get("host") is not True:
        _refuse("host", "native-mac requires host true")
    if env == "container-contained" and report.get("contained") is not True:
        _refuse("contained", "container-contained requires contained true")


def check_launcher_limits(record: dict, limits) -> None:
    """The RunLimits this process will enforce are the ones the record names."""
    pairs = (
        ("max_calls_each", limits.max_calls),
        ("max_reported_tokens_each", limits.max_reported_tokens),
        ("internal_wall_seconds_each", limits.wall_seconds),
    )
    for field, actual in pairs:
        if actual != record[field]:
            _refuse(field, f"launcher RunLimits uses {actual}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowance-record")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--project", default="/work",
                        help="the disposable fixture copy the run may write to")
    args = parser.parse_args()
    project = Path(args.project)
    allowance = None
    if not args.preflight:
        if not args.allowance_record:
            raise SystemExit("A direct Davis allowance record is required")
        try:
            allowance = json.loads(Path(args.allowance_record).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"A direct Davis allowance record is required ({exc})") from None
        require_allowance_record(allowance)
        bind_runtime(allowance, runtime_commit())
        require_allowance_preflight(allowance, project)
    # Container environment contains no API keys or application .env files.
    from quadratus.config import Settings
    from quadratus.project_run import run_project
    from quadratus.run_budget import RunLimits
    from quadratus.scope import TaskScope

    max_calls = allowance["max_calls_each"] if allowance else 24
    max_tokens = allowance["max_reported_tokens_each"] if allowance else 500_000
    internal_wall = allowance["internal_wall_seconds_each"] if allowance else 840
    settings = Settings(
        backend="cli",
        backend_overrides={},
        openai_api_key=None,
        anthropic_api_key=None,
        xai_api_key=None,
        max_retries=1,
        cli_timeout=internal_wall,
        claude_refusal_fallback_model="",
        claude_cli_refusal_fallback_model="",
    )
    goal = (
        "Fix GET /records/{record_id} in app.py so authenticated tenants can read "
        "only their own records. The owner is the tenant returned by current_tenant. "
        "Keep the Record(id,title) response, existing 401 behavior, and identical 404 "
        "responses for missing and other-tenant records. Ignore tenant spoofing via "
        "headers or query parameters. No feature gate is needed in this synthetic "
        "fixture. Edit only app.py; do not edit tests, auth token mappings, record "
        "fixtures or policy. Do not commit, push, contact external services or "
        "install anything. Run the provided check, preserve evidence and stop."
    )
    scope = TaskScope(permitted_paths=("app.py",), max_lines=40)
    limits = RunLimits(
        max_calls=max_calls, max_reported_tokens=max_tokens,
        wall_seconds=internal_wall, max_concurrent_workers=2,
    )
    if allowance is not None:
        check_launcher_limits(allowance, limits)
    if args.preflight:
        print(
            "CLI-only; 24 attempts; 500000 reported-token stop; "
            "840s internal deadline; external hard wall 900s; scope app.py"
        )
        report = project / ".quadratus" / "preflight.json"
        code = _preflight(project, report)
        print(f"preflight {'passed' if code == 0 else 'BLOCKED'}: {report}")
        return code

    console = project / CONSOLE
    console.parent.mkdir(parents=True, exist_ok=True)
    log = console.open("a", encoding="utf-8")

    def progress(message):
        print(message, flush=True)
        log.write(message + "\n")
        log.flush()

    try:
        result = run_project(
            goal,
            project,
            settings,
            allow_writes=True,
            check="python -m pytest -q -p no:cacheprovider /opt/quadratus/test_contract.py",
            max_tasks=2,
            state_dir=".quadratus",
            default_scope=scope,
            run_limits=limits,
            progress=progress,
        )
    finally:
        log.close()
    print(
        json.dumps(
            {"completed": result.completed, "run_dir": str(result.run_dir), "error": result.error}
        ),
        flush=True,
    )
    return 0 if result.completed else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    raise SystemExit(main())
