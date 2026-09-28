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
import hashlib
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
_SHA256 = re.compile(r"[0-9a-f]{64}")
LAUNCHER_CALLS = 24
LAUNCHER_TOKENS = 500_000
LAUNCHER_WALL = 840
_LIMITS = ("max_calls_each", "max_reported_tokens_each", "internal_wall_seconds_each",
           "external_wall_seconds_each", "max_reported_tokens_batch")
_REQUIRED = ("schema", "approved", "batch_id", "authorized_by", "fixture", "grader_sha256",
             "source", "instruction", "recorded_at", "environment", "baseline_sha",
             "candidate_sha", "runs", "runs_per_version", *_LIMITS)


API_CREDENTIAL_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GROK_API_KEY",
    "GROK_DEPLOYMENT_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "ANTHROPIC_AUTH_TOKEN", "OPENAI_AUTH_TOKEN", "XAI_AUTH_TOKEN",
})
API_CREDENTIAL_SUFFIXES = ("_API_KEY", "_DEPLOYMENT_KEY", "_API_TOKEN", "_AUTH_TOKEN")


def scrub_api_credentials(env: dict) -> tuple:
    """A copy of ``env`` without API transport credentials, and the names removed.

    A canary runs on subscription CLIs only. ``Settings(*_api_key=None)`` keeps
    the engine off billed transport, but the vendor CLIs read their own
    variables (grok answers on ``XAI_API_KEY`` when it is set), so the child
    environment must not carry them. Sign-in stores on disk are untouched.
    Values are never returned or printed, only names."""
    removed = sorted(name for name in env
                     if name in API_CREDENTIAL_NAMES or name.endswith(API_CREDENTIAL_SUFFIXES))
    return {k: v for k, v in env.items() if k not in removed}, removed


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
    for key in ("batch_id", "source", "recorded_at", "instruction", "fixture"):
        if not isinstance(record[key], str) or not record[key].strip():
            _refuse(key, "empty")
    if not isinstance(record["grader_sha256"], str) or not _SHA256.fullmatch(record["grader_sha256"]):
        _refuse("grader_sha256", "must be the 64 hex sha256 of the grader file")
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


def runtime_root() -> Path:
    """The checkout whose ``quadratus`` package this process imports, never the
    caller's cwd and not necessarily this file's checkout.

    The series runner hands both versions the same launcher file and points
    ``PYTHONPATH`` at the runtime under test, so the code that runs is the
    imported package's checkout; binding to ``HERE`` would call a baseline run
    a candidate run. ``HERE.parents[1]`` and ``/opt/quadratus`` are fallbacks
    for a launcher run without the package importable.
    """
    try:
        import quadratus
        package_root = Path(quadratus.__file__).resolve().parents[1]
        if (package_root / ".git").exists():
            return package_root
    except ImportError:
        pass
    checkout = HERE.parents[1]
    if (checkout / ".git").exists():
        return checkout
    image = Path("/opt/quadratus")
    if (image / ".git").exists():
        return image
    return checkout


def runtime_commit() -> str:
    """``git rev-parse HEAD`` of the launcher checkout, or ``unknown``."""
    try:
        done = subprocess.run(
            ["git", "-C", str(runtime_root()), "rev-parse", "HEAD"],
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


def require_allowance_preflight(allowance: dict, project, report_path=None) -> None:
    """The report must be this launch: ok, no blockers, probe under ``project``.

    ``host`` must be true for ``native-mac`` and ``contained`` must be true for
    ``container-contained``. A blocker list is refused by its first entry.

    ``report_path`` overrides the record's ``preflight_report``: the launcher
    passes the report it just wrote for this exact project copy. ``project``
    may be None for the record's own pre-batch report, which was made against
    the prepared fixture rather than the copy being launched; every other
    check still applies to it.
    """
    raw = allowance.get("preflight_report") if isinstance(allowance, dict) else None
    if "preflight_report" not in allowance or not isinstance(raw, str) or not raw.strip():
        _refuse("preflight_report", "missing")
    path = Path(report_path) if report_path is not None else Path(raw)
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
    if project is not None:
        try:
            Path(probe).resolve().relative_to(Path(project).resolve())
        except ValueError:
            _refuse("probe_file", f"{probe} is not under {project}")
    env = allowance.get("environment")
    if env == "native-mac" and report.get("host") is not True:
        _refuse("host", "native-mac requires host true")
    if env == "container-contained" and report.get("contained") is not True:
        _refuse("contained", "container-contained requires contained true")


def check_grader(record: dict, project: Path) -> None:
    """``grader_sha256`` must equal the fixture manifest's ``test_contract.py`` hash.

    ``prepare.py`` writes ``.quadratus/fixture-manifest.json`` under ``--project``.
    A missing manifest, or a different grader, refuses.
    """
    path = Path(project) / ".quadratus" / "fixture-manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"fixture has no readable manifest at {path} ({exc})") from exc
    hashes = manifest.get("instrument_sha256") if isinstance(manifest, dict) else None
    claimed = hashes.get("test_contract.py") if isinstance(hashes, dict) else None
    if claimed != record["grader_sha256"]:
        raise SystemExit(
            f"fixture grader sha256 {claimed} is not the allowance's "
            f"grader_sha256 {record['grader_sha256']}"
        )
    # The manifest's figure is a claim; the file's bytes are the check.
    grader = manifest.get("grader") if isinstance(manifest, dict) else None
    if not isinstance(grader, str) or not grader.strip():
        raise SystemExit("fixture manifest names no grader file")
    try:
        actual = hashlib.sha256(Path(grader).read_bytes()).hexdigest()
    except OSError as exc:
        raise SystemExit(f"fixture grader unreadable at {grader} ({exc})") from exc
    if actual != record["grader_sha256"]:
        raise SystemExit(
            f"grader file {grader} hashes to {actual}, not the allowance's "
            f"grader_sha256 {record['grader_sha256']}"
        )


def check_launcher_limits(record: dict, limits) -> None:
    """The Q9 ceiling is hardcoded. A record cannot raise it, and RunLimits cannot either."""
    ceiling = (
        ("max_calls_each", LAUNCHER_CALLS, limits.max_calls),
        ("max_reported_tokens_each", LAUNCHER_TOKENS, limits.max_reported_tokens),
        ("internal_wall_seconds_each", LAUNCHER_WALL, limits.wall_seconds),
    )
    for field, fixed, actual in ceiling:
        if record[field] != fixed:
            _refuse(field, f"launcher ceiling is {fixed}")
        if actual != fixed:
            _refuse(field, f"launcher RunLimits uses {actual}")


Q9_GOAL = (
    "Fix GET /records/{record_id} in app.py so authenticated tenants can read "
    "only their own records. The owner is the tenant returned by current_tenant. "
    "Keep the Record(id,title) response, existing 401 behavior, and identical 404 "
    "responses for missing and other-tenant records. Ignore tenant spoofing via "
    "headers or query parameters. No feature gate is needed in this synthetic "
    "fixture. Edit only app.py; do not edit tests, auth token mappings, record "
    "fixtures or policy. Do not commit, push, contact external services or "
    "install anything. Run the provided check, preserve evidence and stop."
)

# The fixture-v2 goal is the project README's task statement, verbatim in
# substance, so both engines are asked the same thing and the README stays the
# reference a model can read.
V2_GOAL = (
    "Complete exactly two tasks, in order, without changing their boundaries. "
    "1. Non-security display task, scope presentation.py: make /caption use the "
    "existing display helper in catalog.py. Look up that module's helper name and "
    "behavior before editing. Reuse the helper rather than copying its algorithm. "
    "Preserve the public adapter signature and endpoint response. "
    "2. Security task, scope app.py and access.py: use the shared visible_to "
    "predicate at the records endpoint and repair the predicate so only an "
    "authenticated tenant's own records are visible. Missing records and other "
    "tenants' records must both return exactly 404 {\"detail\":\"Not found\"}. "
    "Preserve the Record(id,title) response, existing 401 behavior, and reject "
    "tenant spoofing through headers or query parameters. "
    "Authentication comes only from auth.current_tenant. Do not edit auth.py, "
    "catalog.py, README.md, tests, or policy. There is no feature entitlement. "
    "No installs, commits, pushes, external services, or production data. "
    "The common per-task integration gate checks preservation invariants only; "
    "the operator runs the full grader afterwards. Workers are available but not "
    "mandatory. Report actual lookup evidence and actual worker invocations. "
    "Run the provided check, preserve evidence and stop."
)


def resolve_fixture(project: Path) -> dict:
    """What this project copy is: the Q9 single-file fixture, or a prepared
    fixture-v2 copy whose ``.quadratus/fixture-manifest.json`` names the grader.

    In v2 the grader file's bytes must hash to the manifest's figure, the scope
    is the manifest's allowed paths, the check is the isolated grader's
    preservation subset, and ``CANARY_PROJECT`` is set so every gate process the
    engine spawns finds the project (the grader reads only that variable).
    """
    manifest_path = Path(project) / ".quadratus" / "fixture-manifest.json"
    if not manifest_path.is_file():
        return {"mode": "q9", "goal": Q9_GOAL, "scope_paths": ("app.py",), "max_lines": 40,
                "check": "python -m pytest -q -p no:cacheprovider /opt/quadratus/test_contract.py",
                "max_tasks": 2, "grader": None}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    grader = Path(manifest["grader"])
    want = manifest["instrument_sha256"]["test_contract.py"]
    try:
        have = hashlib.sha256(grader.read_bytes()).hexdigest()
    except OSError as exc:
        raise SystemExit(f"fixture grader unreadable at {grader} ({exc})") from exc
    if have != want:
        raise SystemExit(f"fixture grader at {grader} hashes to {have}, manifest says {want}")
    if Path(project).resolve() in grader.resolve().parents:
        raise SystemExit(f"fixture grader {grader} sits inside the solver tree")
    return {"mode": "v2", "goal": V2_GOAL,
            "scope_paths": tuple(manifest["allowed_paths"]), "max_lines": 60,
            "check": f"{sys.executable} -m pytest -q -p no:cacheprovider {grader} -k preservation",
            "max_tasks": int(manifest["max_tasks"]), "grader": str(grader)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowance-record")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--project", default="/work",
                        help="the disposable fixture copy the run may write to")
    args = parser.parse_args()
    project = Path(args.project)
    # Before the preflight probes or any seat: the CLIs must see no API key.
    scrubbed, removed = scrub_api_credentials(dict(os.environ))
    if removed:
        os.environ.clear()
        os.environ.update(scrubbed)
        print(f"scrubbed API credentials from this process: {', '.join(removed)}", flush=True)
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
        # The record's report is the operator's pre-batch evidence, made against
        # the prepared fixture. This copy gets its own preflight now, in the
        # exact tree the seats will read, and that report is what binds.
        require_allowance_preflight(allowance, None)
        fresh = project / ".quadratus" / "preflight.json"
        if _preflight(project, fresh) != 0:
            raise SystemExit(f"preflight refused this copy: {fresh}")
        require_allowance_preflight(allowance, project, report_path=fresh)
        check_grader(allowance, project)
    # API credentials were scrubbed above; sign-in stores on disk are the
    # only transport left, which is what "subscription CLI only" means.
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
        cli_timeout=LAUNCHER_WALL,
        claude_refusal_fallback_model="",
        claude_cli_refusal_fallback_model="",
    )
    fixture = resolve_fixture(project)
    goal = fixture["goal"]
    scope = TaskScope(permitted_paths=fixture["scope_paths"], max_lines=fixture["max_lines"])
    limits = RunLimits(
        max_calls=LAUNCHER_CALLS, max_reported_tokens=LAUNCHER_TOKENS,
        wall_seconds=LAUNCHER_WALL, max_concurrent_workers=2,
    )
    if allowance is not None:
        check_launcher_limits(allowance, limits)
    if args.preflight:
        print(
            "CLI-only; 24 attempts; 500000 reported-token stop; "
            f"840s internal deadline; external hard wall 900s; fixture {fixture['mode']}; "
            f"scope {', '.join(fixture['scope_paths'])}"
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

    os.environ["CANARY_PROJECT"] = str(project.resolve())
    try:
        result = run_project(
            goal,
            project,
            settings,
            allow_writes=True,
            check=fixture["check"],
            # Exactly the fixture's task count. A run that closes every task
            # still reads incomplete until the engine asks one terminal,
            # completion-only question after the cap (open, sensitive path).
            # Raising this cap would permit a further task, not a confirmation.
            max_tasks=fixture["max_tasks"],
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
