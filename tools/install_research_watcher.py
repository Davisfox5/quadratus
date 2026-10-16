"""Install the current checkout's research watcher as a macOS user LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.quadratus.research-inbox"


def main():
    if sys.platform != "darwin":
        raise SystemExit("Run this installer on your Mac with signed-in Claude and Codex CLIs.")
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")
    for binary in ("gh", "claude", "codex"):
        if not shutil.which(binary):
            raise SystemExit(f"Install {binary} and sign in before installing the watcher.")
    user = subprocess.run(["gh", "api", "user", "--jq", ".id"], capture_output=True,
                          text=True, timeout=30, check=True).stdout.strip()
    if user != "167821890":
        raise SystemExit("GitHub CLI must be signed in as Davisfox5 (gh auth switch/login).")
    venv = ROOT / ".quadratus/research-venv"
    if not venv.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin/python"
    subprocess.run([str(python), "-m", "pip", "install", "-e", str(ROOT)], check=True)
    state = Path.home() / ".quadratus/research-inbox"
    state.mkdir(parents=True, exist_ok=True)
    # Check permissions and input validation without spending subscription usage.
    subprocess.run([str(python), str(ROOT / "tools/research_review.py"), "--dry-run"], check=True)
    path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(ROOT / "tools/research_review.py")],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "StartInterval": 900,
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                 "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(state / "watcher.log"),
        "StandardErrorPath": str(state / "watcher-error.log"),
    }
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True, check=False)
    path.write_bytes(plistlib.dumps(config))
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    print("Research watcher installed. It checks digest PRs every 15 minutes while your Mac is awake.")
    print(f"Logs and engine evidence: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
