"""Ask each installed CLI whether it can actually work here, before a run.

The container preflight used to check versions, authentication and a Chromium
launch. None of those touch the thing that broke attempts 3 and 5 of the blind
acceptance: the Codex CLI sandboxes model-run shell commands with bubblewrap,
which cannot create a user namespace inside our container, so every OpenAI seat
was blind to the project. Both runs spent a subscription window discovering it.

Two questions are asked here, and neither invokes a model:

* can the harness read the mounted work tree at all, and
* can each vendor's own sandbox start, where the vendor has one.

A vendor whose sandbox cannot start is only a blocker when we intend to rely on
it. Under ``QUADRATUS_CONTAINED=1`` the container is the boundary and that
vendor's sandbox is stood down deliberately, so the same fact is reported and
not treated as fatal. Run it inside ``run_isolated``; it refuses a host.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

#: Long enough for a cold binary to start, short enough that a hung CLI cannot
#: eat the run's wall clock before the supervisor notices.
TIMEOUT = 30


def _read_check(work: Path) -> dict:
    """The mount itself: an unreadable work tree makes every other answer moot."""
    try:
        names = sorted(p.name for p in work.iterdir())
    except OSError as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    for name in names:
        candidate = work / name
        if candidate.is_file():
            try:
                candidate.read_bytes()
            except OSError as exc:
                return {"ok": False, "entries": len(names),
                        "detail": f"{name} is listed but unreadable: {exc}"}
            return {"ok": True, "entries": len(names), "read": name}
    return {"ok": False, "entries": len(names), "detail": "no regular file to read"}


def _sandbox_check(vendor: str, spec) -> dict:
    """Does this vendor's own sandbox start? No model is invoked."""
    binary = shutil.which(spec.binary)
    if binary is None:
        return {"applicable": True, "ok": False, "detail": f"{spec.binary} is not installed"}
    if not spec.sandbox_selftest_args:
        return {"applicable": False,
                "detail": "this vendor has no inner sandbox to test; its restriction is a "
                          "tool denial, which needs no privilege to take effect"}
    argv = [binary, *spec.sandbox_selftest_args]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"applicable": True, "ok": False, "argv": argv,
                "detail": f"{type(exc).__name__}: {exc}"}
    # Vendor output can carry paths and session ids; keep one diagnostic line.
    detail = (done.stderr or done.stdout or "").strip().splitlines()
    return {"applicable": True, "ok": done.returncode == 0, "argv": argv,
            "exit_code": done.returncode,
            "detail": detail[0][:300] if detail else ""}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", default="/work", help="the mounted application tree")
    parser.add_argument("--output", help="write the report here as JSON")
    args = parser.parse_args()

    if not Path("/.dockerenv").exists():
        raise SystemExit("Run only inside run_isolated: a host preflight proves nothing "
                         "about the container the run will use")

    # Imported after the refusal, not before it: the engine lives at a path
    # that only exists inside the container, so importing first would fail on
    # a host for the wrong reason and hide the real message.
    sys.path.insert(0, "/opt/quadratus")
    from quadratus.cli_providers import CLI_SPECS, contained

    report = {
        "contained": contained(),
        "work_tree": _read_check(Path(args.work)),
        "vendors": {vendor: _sandbox_check(vendor, spec) for vendor, spec in CLI_SPECS.items()},
    }
    blockers = []
    if not report["work_tree"]["ok"]:
        blockers.append("the work tree cannot be read")
    for vendor, result in report["vendors"].items():
        if result.get("applicable") and not result["ok"]:
            if report["contained"]:
                result["stood_down"] = ("its sandbox is stood down under QUADRATUS_CONTAINED, "
                                        "so this is recorded, not fatal")
            else:
                blockers.append(f"{vendor}'s own sandbox cannot start, so its seats can run "
                                f"no command and will be blind to the project")
    report["blockers"] = blockers
    report["ok"] = not blockers

    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        os.chmod(target, 0o600)
    print(text, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
