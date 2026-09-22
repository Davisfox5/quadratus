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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowance-record")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--project", default="/work",
                        help="the disposable fixture copy the run may write to")
    args = parser.parse_args()
    project = Path(args.project)
    if not args.preflight:
        if not args.allowance_record:
            raise SystemExit("A direct Davis allowance record is required")
        allowance = json.loads(Path(args.allowance_record).read_text())
        if allowance.get("authorized_by") != "Davis" or not allowance.get("source"):
            raise SystemExit("A direct Davis allowance record is required")
    # Container environment contains no API keys or application .env files.
    from quadratus.config import Settings
    from quadratus.project_run import run_project
    from quadratus.run_budget import RunLimits
    from quadratus.scope import TaskScope

    settings = Settings(
        backend="cli",
        backend_overrides={},
        openai_api_key=None,
        anthropic_api_key=None,
        xai_api_key=None,
        max_retries=1,
        cli_timeout=840,
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
        max_calls=24, max_reported_tokens=500_000, wall_seconds=840, max_concurrent_workers=2
    )
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
