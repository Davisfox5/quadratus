"""Ask each installed CLI whether it can actually work here, before a run.

The container preflight used to check versions, authentication and a Chromium
launch. None of those touch the thing that broke attempts 3 and 5 of the blind
acceptance: the Codex CLI sandboxes model-run shell commands with bubblewrap,
which cannot create a user namespace inside our container, so every OpenAI seat
was blind to the project. Both runs spent a subscription window discovering it.

Two questions are asked here, and neither invokes a model:

* can the harness read the mounted work tree at all, and
* can a seat on each vendor read one real file out of it.

The second is asked with the vendor's sandbox configured exactly as this run
will configure it -- including the contained substitution, when the launcher
has asserted one -- so a pass is about the run's own configuration and not some
other one. A failure is therefore always a blocker: it says the seats will be
blind to the project, which is what attempts 3, 5 and 6 each spent a
subscription window discovering. Run it inside ``run_isolated``; it refuses a
host.
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


def _sandbox_check(vendor: str, spec, probe_file: str) -> dict:
    """Can a seat on this vendor read ``probe_file``? No model is invoked.

    The sandbox is exercised in the mode a seat here would really be given, so
    a pass means the run's own configuration works rather than some other one.
    """
    binary = shutil.which(spec.binary)
    if binary is None:
        return {"applicable": True, "ok": False, "detail": f"{spec.binary} is not installed"}
    selftest = spec.sandbox_selftest(probe_file)
    if not selftest:
        return {"applicable": False,
                "detail": "this vendor has no inner sandbox to test; its restriction is a "
                          "tool denial, which needs no privilege to take effect"}
    argv = [binary, *selftest]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"applicable": True, "ok": False, "argv": argv,
                "detail": f"{type(exc).__name__}: {exc}"}
    # Vendor output can carry paths and session ids; keep one diagnostic line.
    # Prefer a line that is not a warning: codex opens with an unrelated
    # PATH-alias warning, and reporting that instead of the sandbox error
    # would describe the wrong failure.
    lines = [line for line in (done.stderr or done.stdout or "").strip().splitlines() if line]
    speaking = [line for line in lines if not line.lstrip().upper().startswith("WARNING")]
    return {"applicable": True, "ok": done.returncode == 0, "argv": argv,
            "exit_code": done.returncode,
            "detail": (speaking or lines or [""])[0][:300]}


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

    work = Path(args.work)
    tree = _read_check(work)
    report = {"contained": contained(), "work_tree": tree, "vendors": {}}
    blockers = []
    if not tree["ok"]:
        # Without a file the harness can read, a vendor check has nothing
        # honest to ask for, so it is not asked and not reported as passing.
        blockers.append("the work tree cannot be read")
    else:
        probe = str(work / tree["read"])
        report["vendors"] = {vendor: _sandbox_check(vendor, spec, probe)
                             for vendor, spec in CLI_SPECS.items()}
        report["probe_file"] = probe
    for vendor, result in report["vendors"].items():
        if result.get("applicable") and not result["ok"]:
            blockers.append(f"a seat on {vendor} cannot read the project: its sandbox is "
                            f"configured exactly as this run would configure it, and in "
                            f"that mode it can run no command at all")
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
