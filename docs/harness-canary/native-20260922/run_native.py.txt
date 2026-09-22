"""One approved canary run. The external supervisor enforces the hard wall."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowance-record")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
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
        return 0
    result = run_project(
        goal,
        '/Users/davisfox/Documents/Codex/2026-09-21/referenced-chatgpt-conversation-this-is-an/work/q9-native-20260922/project',
        settings,
        allow_writes=True,
        check="/tmp/quadratus-harness-env/bin/python -m pytest -q -p no:cacheprovider /Users/davisfox/Documents/Codex/2026-09-21/referenced-chatgpt-conversation-this-is-an/work/q9-native-20260922/examiner/test_contract.py",
        max_tasks=2,
        state_dir=".quadratus",
        default_scope=scope,
        run_limits=limits,
        progress=lambda message: print(message, flush=True),
    )
    print(
        json.dumps(
            {"completed": result.completed, "run_dir": str(result.run_dir), "error": result.error}
        ),
        flush=True,
    )
    return 0 if result.completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
