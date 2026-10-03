"""Stage B pair runner: the same feature task, once with Jev and once without,
on identical checkouts, under identical limits (2026-10-01, Davis's call on
#35: five features, two arms each, as parallel as the subscriptions allow).

Three commands, one manifest between them:

    python3 tools/jev_stage_b/pairs.py prepare --packet packet.json --out /abs/runs
    python3 tools/jev_stage_b/pairs.py run     --out /abs/runs [--pairs 1] [--only TASK] [--arm jev|rule]
    python3 tools/jev_stage_b/pairs.py collect --out /abs/runs

``prepare`` makes one detached git worktree per cell (task x arm x repeat)
at the packet's baseline, writes each cell's task file, and records the
order (AB then BA, counterbalanced by task) in ``manifest.json``. ``run``
launches cells through ``quadratus.project_run.run_project`` with the
packet's ``RunLimits`` spelled out, one pair at a time by default; both arms
of a pair start together so vendor availability is the same for both.
``collect`` reads every cell's ``result.json``, ``budget.json`` and
``usage.jsonl`` and renders the comparison. No provider is contacted by
``prepare`` or ``collect``; ``run`` is the only command that spends.

A cell is an ordinary run of the engine, so every check, capture, gate and
stop applies as it would to any run. A task with a ``text`` is a listed run
(``run_project(tasks=[text])``, one task, no planner). A task with only a
``goal`` is a planned run: the orchestrator decomposes the goal under the
packet's ``max_tasks``, and with ``survey_recovery`` set it continues
through failures, each re-plan carrying a HYPOTHESIS line the harness
records (Davis, 2026-10-02: deep features, failures logged, not stopped
on). On a planned jev cell the orchestrator is asked not to label and the
decider routes every task (``decider_labels="all"``); the rule cell's
orchestrator labels as usual. The comparison is between arms on the same
frozen goal, never between a run and an edited version of itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional

ARMS = ("jev", "rule")

#: Why a series stops before its next launch (plan, "Stop rules"). Each is
#: read off the cells that have run; none is a model's opinion.
STOP_SEAT = "seat-fallback"          # an orchestrator call answered by a model other than the chain's primary
STOP_UNKNOWN_USAGE = "unknown-usage"  # a cell reported attempts whose usage the CLI did not report
STOP_JEV_DRIFT = "jev-model-drift"    # Jev answered as a model other than the packet's jev_model
STOP_CREDENTIAL = "credential-failure"  # a decider refusal naming the key or an auth status, or a sign-in failure
STOP_CELL_ERROR = "cell-error"        # a cell raised before the engine produced a result
STOP_INTERRUPTED = "interrupted-cell"  # a cell was still "running" when a later run began: its launch never finished
STOP_INTEGRITY = "integrity-failure"   # a frozen input or the engine checkout changed at a launch or grading boundary
STOP_CAPABILITY = "capability-failure"  # a lead granted writes was denied them, or the engine saved CapabilityUnavailable

#: The file tools a lead needs; a denial of any of them on a lead call is
#: read from the run's trace.jsonl (Codex 5969458776: the terminal error
#: and the invocation identity said nothing about ten denied Opus leads).
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "write_file", "edit_file", "apply_patch",
               "search_replace", "create_file")

#: How the engine's own saved terminal error is classified (Codex review of
#: e3deaed, P1 #1): run_project catches the session's exception, writes it
#: to result.json and returns normally, so a seat or access failure never
#: reaches the launcher as an exception. Feature failures, stalls and budget
#: stops stay what they are; only these shapes are access failures.
SEAT_ERROR_MARKS = ("OrchestratorUnavailable", "Unavailable")
CREDENTIAL_ERROR_MARKS = ("not signed in", "sign in", "signed out", "unauthenticated", "unauthorized",
                          "401", "403", "api_key", "credential", "login")


def classify_access_failure(text: Optional[str]) -> Optional[str]:
    """STOP_SEAT, STOP_CREDENTIAL or None for an error message."""
    if not text:
        return None
    if any(mark in text for mark in SEAT_ERROR_MARKS):
        return STOP_SEAT
    low = text.lower()
    if any(mark in low for mark in CREDENTIAL_ERROR_MARKS):
        return STOP_CREDENTIAL
    return None

#: One run per series directory. The lock holds the pid and start time; a
#: stale lock (a crashed run) is reported, never silently taken over.
LOCK_NAME = "run.lock"


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def validate_packet(packet: dict) -> None:
    """Refuse a packet that would make the two arms differ in anything but the decider."""
    for key in ("project", "goal", "limits", "tasks"):
        if key not in packet:
            raise ValueError(f"packet needs {key!r}")
    project = packet["project"]
    if not Path(project["repo"]).is_absolute() or len(project.get("base_sha", "")) != 40:
        raise ValueError("project needs an absolute repo path and a full base_sha")
    limits = packet["limits"]
    for key in ("max_calls", "max_reported_tokens", "wall_seconds"):
        if not isinstance(limits.get(key), (int, float)) or limits[key] <= 0:
            raise ValueError(f"limits.{key} must be a positive number")
    try:
        # The engine's own validation, at prepare rather than at the first
        # launch: reserve_tokens_per_call, max_tokens_per_call and the rest.
        from quadratus.run_budget import RunLimits
        RunLimits(**limits)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"limits: {exc}") from exc
    turns = packet.get("lead_max_turns")
    if turns is not None and (type(turns) is not int or turns < 1):
        raise ValueError("lead_max_turns must be a positive integer or null")
    seen = set()
    for task in packet["tasks"]:
        if not task.get("id") or task["id"] in seen:
            raise ValueError("every task needs a unique id")
        seen.add(task["id"])
        text, goal = str(task.get("text") or ""), str(task.get("goal") or packet["goal"])
        if not text.strip() and "REQUIREMENTS:" not in goal:
            raise ValueError(f"{task['id']}: a planned task needs a goal with a REQUIREMENTS: block")
        if "KIND:" in text or "KIND:" in goal:
            raise ValueError(f"{task['id']}: a KIND line would pre-empt the decider; both arms must omit it")
        paths = task.get("declared_paths") or []
        if not isinstance(paths, list) or any(not isinstance(p, str) or not p for p in paths):
            raise ValueError(f"{task['id']}: declared_paths must be a list of path strings")
    per_arm = packet.get("capture_profiles")
    if per_arm is not None and (not isinstance(per_arm, dict) or set(per_arm) != set(ARMS)
                                or any(not isinstance(v, str) or not Path(v).is_absolute() for v in per_arm.values())):
        raise ValueError(f"capture_profiles must name one absolute profile per arm {ARMS}")
    graders = packet.get("graders")
    if graders is not None:
        gdir = Path(str(graders.get("dir") or ""))
        if not gdir.is_absolute():
            raise ValueError("graders.dir must be an absolute path outside the project")
        repo = Path(project["repo"]).resolve()
        if gdir.resolve() == repo or gdir.resolve().is_relative_to(repo):
            raise ValueError("graders.dir must lie outside the project repository: builders must not be able to edit it")
        for tid, commands in (graders.get("tasks") or {}).items():
            if tid not in seen:
                raise ValueError(f"graders.tasks names an unknown task {tid!r}")
            if not isinstance(commands, list) or not commands or any(
                    not isinstance(c, list) or not c or any(not isinstance(a, str) for a in c) for c in commands):
                raise ValueError(f"graders.tasks[{tid!r}] must be a non-empty list of argv lists")
        missing = [t["id"] for t in packet["tasks"] if t["id"] not in (graders.get("tasks") or {})]
        if missing:
            raise ValueError(f"every task needs a grader when graders are configured; missing: {', '.join(missing)}")
    if int(packet.get("repeats", 1)) < 1:
        raise ValueError("repeats must be at least 1")
    if int(packet.get("max_tasks", 1)) < 1:
        raise ValueError("max_tasks must be at least 1")
    recovery = packet.get("survey_recovery")
    if recovery is not None and (not isinstance(recovery, int) or recovery < 1):
        raise ValueError("survey_recovery must be a positive integer or null")


def cells_for(packet: dict) -> List[dict]:
    """Every cell with its counterbalanced order: even tasks jev first, odd tasks rule first."""
    cells = []
    for index, task in enumerate(packet["tasks"]):
        order = ARMS if index % 2 == 0 else tuple(reversed(ARMS))
        for repeat in range(int(packet.get("repeats", 1))):
            for position, arm in enumerate(order):
                cells.append(dict(task=task["id"], arm=arm, repeat=repeat, position=position,
                                  name=f"{task['id']}/{arm}-r{repeat}", text=task.get("text") or "",
                                  goal=task.get("goal") or packet["goal"],
                                  declared_paths=list(task.get("declared_paths") or []),
                                  entry="listed" if task.get("text") else "planned"))
    return cells


def prepare(packet: dict, out: Path, *, worktree: Callable[[Path, Path, str], None] = None) -> dict:
    validate_packet(packet)
    out.mkdir(parents=True, exist_ok=True)
    repo, base = Path(packet["project"]["repo"]), packet["project"]["base_sha"]
    worktree = worktree or _git_worktree
    cells = cells_for(packet)
    for cell in cells:
        path = out / cell["name"]
        if path.exists():
            raise FileExistsError(f"{path} exists; a prepared cell is never reused")
        path.parent.mkdir(parents=True, exist_ok=True)
        worktree(repo, path, base)
        cell["project"] = str(path)
        stem = path.parent / f"{cell['arm']}-r{cell['repeat']}"
        if cell["entry"] == "listed":
            stem.with_suffix(".tasks.json").write_text(json.dumps([cell["text"]], indent=2) + "\n")
            cell["tasks_file"] = str(stem.with_suffix(".tasks.json"))
        else:
            stem.with_suffix(".goal.txt").write_text(cell["goal"])
            cell["goal_file"] = str(stem.with_suffix(".goal.txt"))
        cell["state"] = "prepared"
    manifest = dict(format=1, packet=packet, packet_digest=_sha(packet), out=str(out),
                    prepared_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), cells=cells,
                    frozen=frozen_inputs(packet))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_inputs(packet: dict) -> dict:
    """Content hashes of everything a cell is judged by that lives outside
    the packet text: every file under ``graders.dir`` and the capture
    profile. Written at prepare, re-read before every launch (Davis via
    Codex, 2026-10-03: packet, grader and profile integrity)."""
    frozen: Dict[str, object] = {}
    graders = packet.get("graders")
    if graders:
        gdir = Path(graders["dir"])
        if not gdir.is_dir():
            raise FileNotFoundError(f"graders.dir {gdir} is not a directory")
        files = sorted(p for p in gdir.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        if not files:
            raise ValueError(f"graders.dir {gdir} holds no files")
        frozen["graders"] = {str(p.relative_to(gdir)): _file_sha(p) for p in files}
    profile = packet.get("capture_profile")
    if profile:
        frozen["capture_profile"] = _file_sha(Path(profile))
    per_arm = packet.get("capture_profiles") or {}
    for arm, path in sorted(per_arm.items()):
        frozen[f"capture_profile:{arm}"] = _file_sha(Path(path))
    return frozen


def profile_for(cell: dict, packet: dict) -> Optional[str]:
    """The capture profile a cell runs with. Two arms of a pair run at the
    same time, and a profile pins one loopback port, so each arm may have
    its own (``capture_profiles``); otherwise the one ``capture_profile``."""
    per_arm = packet.get("capture_profiles") or {}
    return per_arm.get(cell["arm"]) or packet.get("capture_profile")


def check_frozen(packet: dict, frozen: dict) -> None:
    """Refuse a launch when a grader or the profile changed since prepare."""
    now = frozen_inputs(packet)
    if now != (frozen or {}):
        changed = sorted(set((now.get("graders") or {}).items()) ^ set(((frozen or {}).get("graders") or {}).items()))
        what = ", ".join(sorted({k for k, _ in changed})) or "capture_profile"
        raise RuntimeError(f"frozen inputs changed since prepare: {what}; a changed grader or profile is not run")


def grade_cell(cell: dict, packet: dict, *, timeout: Optional[int] = None) -> Optional[dict]:
    """Run the feature's frozen graders against the cell's finished project.
    Each runs from ``graders.dir`` (never from the project, which the
    builders could edit) with the cell's project in ``STAGE_B_PROJECT`` and
    the arm in ``STAGE_B_ARM``; exit 0 is a pass. The grade is the
    independent judgement; the engine's own completion claim is reported
    beside it, never in place of it."""
    graders = packet.get("graders")
    if not graders or cell.get("state") != "ran":
        return None
    gdir = Path(graders["dir"])
    env = dict(os.environ, STAGE_B_PROJECT=str(cell["project"]), STAGE_B_ARM=cell["arm"],
               STAGE_B_RUN_DIR=str(cell.get("run_dir") or ""))
    limit = timeout or int(graders.get("timeout_seconds", 600))
    results = []
    for argv in graders["tasks"][cell["task"]]:
        started = time.perf_counter()
        try:
            proc = subprocess.run(argv, cwd=gdir, env=env, capture_output=True, text=True, timeout=limit)
            results.append(dict(argv=argv, exit=proc.returncode, passed=proc.returncode == 0,
                                output_tail=(proc.stdout + proc.stderr)[-2000:],
                                seconds=round(time.perf_counter() - started, 1)))
        except (subprocess.TimeoutExpired, OSError) as exc:
            results.append(dict(argv=argv, exit=None, passed=False, output_tail=f"{type(exc).__name__}: {exc}"[:2000],
                                seconds=round(time.perf_counter() - started, 1)))
    return dict(passed=sum(1 for r in results if r["passed"]), total=len(results), results=results)


def cli_versions(names=("claude", "codex", "grok")) -> dict:
    """What each vendor CLI reports as its version at launch time, recorded
    beside the engine SHA; None when the binary is missing or will not say."""
    out = {}
    for name in names:
        try:
            proc = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=20)
            out[name] = (proc.stdout or proc.stderr).strip().splitlines()[0][:120] if (proc.stdout or proc.stderr).strip() else None
        except (OSError, subprocess.TimeoutExpired, IndexError):
            out[name] = None
    return out


def _git_worktree(repo: Path, path: Path, base: str) -> None:
    git(repo, "worktree", "add", "--detach", str(path), base)


def engine_identity() -> dict:
    """The engine a launch would import: the checkout's HEAD and whether it
    is clean. Binding by operator discipline alone left this unrecorded
    (Codex and the Mac session on #35, 2026-10-03)."""
    import quadratus
    root = Path(quadratus.__file__).resolve().parent.parent
    try:
        sha = git(root, "rev-parse", "HEAD")
        dirty = bool(git(root, "status", "--porcelain", "--untracked-files=no"))
    except (subprocess.CalledProcessError, OSError) as exc:
        return dict(root=str(root), sha=None, dirty=None, error=f"{type(exc).__name__}: {exc}"[:200])
    return dict(root=str(root), sha=sha, dirty=dirty)


def check_engine(packet: dict, engine: dict) -> None:
    """Refuse a launch on an engine other than the packet's, or on a dirty
    checkout, which is no SHA at all."""
    if engine.get("sha") is None:
        raise RuntimeError(f"the engine checkout has no readable HEAD: {engine.get('error')}")
    if engine.get("dirty"):
        raise RuntimeError(f"the engine checkout {engine['root']} has uncommitted changes; a cell must run on one SHA")
    wanted = str(packet.get("engine_sha") or "")
    if len(wanted) == 40 and wanted != engine["sha"]:
        raise RuntimeError(f"packet engine_sha {wanted[:12]} but the importable engine is {engine['sha'][:12]}")


def default_launcher(cell: dict, packet: dict) -> dict:
    """One real run: the engine's own entry point with the packet's limits spelled out."""
    from quadratus.config import Settings
    from quadratus.project import Project
    from quadratus.project_run import run_project
    from quadratus.run_budget import RunLimits
    limits = RunLimits(**packet["limits"])
    extra = packet.get("extra_checks") or ()
    settings = Settings.from_env()
    if packet.get("lead_max_turns"):
        settings.lead_max_turns = int(packet["lead_max_turns"])
    result = run_project(
        cell["goal"], Project.open(cell["project"]), settings,
        allow_writes=True, check=packet.get("check", ""), extra_checks=extra,
        capture_profile=profile_for(cell, packet), readiness=packet.get("readiness"),
        run_limits=limits, declared_paths=tuple(cell.get("declared_paths") or ()),
        decider=cell["arm"] if cell["arm"] != "rule" else None,
        direct_tier=bool(packet.get("direct_tier", False)),
        progress=lambda message: print(f"[{cell['name']}] {message}", flush=True),
        **launch_shape(cell, packet))
    return dict(run_dir=str(result.run_dir), completed=bool(result.completed))


def launch_shape(cell: dict, packet: dict) -> dict:
    """The run_project arguments that differ between a listed and a planned
    cell. Listed: the one frozen text, no planner. Planned: the goal, the
    packet's task cap, the survey allowance, and on the jev arm a decider
    that labels every task because the orchestrator is asked not to."""
    if cell.get("entry", "listed" if cell.get("text") else "planned") == "listed":
        return dict(tasks=[cell["text"]], max_tasks=1)
    from quadratus.session import SurveyConfig
    recovery = packet.get("survey_recovery")
    shape = dict(max_tasks=int(packet.get("max_tasks", 1)),
                 survey=SurveyConfig(recovery_tasks=int(recovery)) if recovery else None)
    if cell["arm"] != "rule":
        shape["decider_labels"] = "all"
    return shape


def series_stop(cell: dict, packet: dict) -> Optional[str]:
    """The stop rule a finished cell trips, or None. Read from the cell's
    own record; the next batch is not launched past a stop."""
    if cell.get("state") == "integrity-failed" or cell.get("integrity"):
        return f"{STOP_INTEGRITY}: {cell.get('integrity') or cell.get('error')}"[:300]
    if cell.get("state") == "failed":
        text = str(cell.get("error") or "")
        kind = classify_access_failure(text)
        return f"{kind or STOP_CELL_ERROR}: {text[:160]}"
    if cell.get("state") != "ran":
        return None
    row = _read_cell(cell)
    # The engine's saved terminal error (result.json), which the launcher
    # never sees as an exception. An access-shaped one stops the series; an
    # error saved before any model call was answered is a cell error, not a
    # feature result; everything else (a stall, a budget stop, unmet
    # requirements) is the cell's own outcome.
    saved = row.get("result_error")
    if saved and str(saved).startswith("CapabilityUnavailable"):
        return f"{STOP_CAPABILITY}: engine saved {str(saved)[:160]} in {cell['name']}"
    if row.get("leads_denied_writes"):
        first = row["leads_denied_writes"][0]
        return (f"{STOP_CAPABILITY}: lead {first.get('model')} on {first.get('task')} was denied "
                f"{first.get('denied')} project write(s) and wrote nothing in the project in {cell['name']}")
    if saved:
        kind = classify_access_failure(saved)
        if kind:
            return f"{kind}: engine saved {saved[:160]} in {cell['name']}"
        if not row.get("orchestrators") and not row.get("leads") and not (row.get("calls") or 0):
            return f"{STOP_CELL_ERROR}: engine saved {saved[:160]} before any call was answered in {cell['name']}"
    if (row.get("unknown_usage") or 0) > 0:
        return f"{STOP_UNKNOWN_USAGE}: {row['unknown_usage']} attempt(s) in {cell['name']}"
    primary = _chain_primary()
    others = [m for m in row.get("orchestrators") or [] if primary and m != primary]
    if others:
        return f"{STOP_SEAT}: orchestrator answered by {', '.join(others)} in {cell['name']}"
    wanted = packet.get("jev_model")
    drift = [m for m in row.get("jev_models") or [] if wanted and m != wanted]
    if drift:
        return f"{STOP_JEV_DRIFT}: {', '.join(drift)} (packet names {wanted}) in {cell['name']}"
    for err in row.get("decision_errors") or []:
        low = err.lower()
        if "api_key" in low or "typesafe_api_key" in low or " 401" in low or " 403" in low or "unauthori" in low:
            return f"{STOP_CREDENTIAL}: {err[:160]}"
    return None


def _chain_primary() -> Optional[str]:
    try:
        from quadratus.registry import ORCHESTRATOR_CHAIN, resolve
    except ImportError:  # pragma: no cover - the engine is a sibling package
        return None
    first = ORCHESTRATOR_CHAIN[0]
    try:
        return getattr(resolve(first), "key", None) or str(first)
    except Exception:  # noqa: BLE001 -- a chain head that will not resolve is reported as its name
        return str(first)


def run(out: Path, *, launcher: Callable[[dict, dict], dict] = default_launcher,
        pairs: int = 1, only: Optional[str] = None, arm: Optional[str] = None,
        engine: Optional[dict] = None, override_stop: Optional[str] = None,
        versions: Optional[dict] = None) -> dict:
    """Launch unrun cells, both arms of a pair together, ``pairs`` pairs at a
    time. ``arm`` runs one arm only (rule first across the series, jev
    later, when the OpenAI window is better spent that way); the other arm's
    cells stay prepared for a later ``run``.

    Before any launch: the packet in the manifest must still hash to the
    digest ``prepare`` wrote, and the importable engine must be the packet's
    SHA on a clean checkout. After each batch the stop rules are read off
    the finished cells; a tripped rule is recorded on the manifest and no
    further batch launches until an operator passes ``override_stop`` with
    their reason, which is recorded too."""
    if arm is not None and arm not in ARMS:
        raise ValueError(f"arm must be one of {ARMS}")
    lock = out / LOCK_NAME
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        held = ""
        try:
            held = lock.read_text().strip()
        except OSError:
            pass
        raise RuntimeError(f"another run holds {lock} ({held or 'unreadable'}); a series runs from one process. "
                           "If that process is gone, remove the lock by hand after checking the manifest "
                           "for cells left 'running'") from None
    with os.fdopen(fd, "w") as handle:
        handle.write(f"pid={os.getpid()} at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
    try:
        return _run_locked(out, launcher=launcher, pairs=pairs, only=only, arm=arm, engine=engine,
                           override_stop=override_stop, versions=versions)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _run_locked(out: Path, *, launcher, pairs, only, arm, engine, override_stop, versions) -> dict:
    manifest = json.loads((out / "manifest.json").read_text())
    packet = manifest["packet"]
    writing = threading.Lock()

    def save():
        # Atomic, so a reader (a grader, the operator, a sibling thread) never sees a half-written manifest.
        with writing:
            tmp = out / "manifest.json.tmp"
            tmp.write_text(json.dumps(manifest, indent=2) + "\n")
            os.replace(tmp, out / "manifest.json")

    # A cell still "running" from an earlier process never finished its
    # launch (Davis via Codex, 2026-10-03: an interrupted launch cannot
    # silently retry). It is marked, kept, and never launched again.
    for cell in manifest["cells"]:
        if cell.get("state") == "running":
            cell.update(state="interrupted", error=f"{STOP_INTERRUPTED}: launch started {cell.get('started_at')} "
                                                   "and never recorded an outcome; not relaunched")
    if _sha(packet) != manifest.get("packet_digest"):
        raise RuntimeError("the manifest's packet no longer matches the digest written at prepare; "
                           "a packet changed after prepare is not run")
    # ``engine`` is a dict (a fixed identity, tests) or a callable that reads
    # the checkout afresh; the default re-reads at every boundary (Codex review
    # of e3deaed, P1 #2: a once-per-run check left every later cell unguarded).
    identify = engine if callable(engine) else (lambda: engine) if engine is not None else engine_identity

    def integrity() -> tuple:
        """(problem or None, engine identity now). Never raises: a failed
        check is recorded on the cell, never a crash mid-series."""
        try:
            check_frozen(packet, manifest.get("frozen"))
            ident = identify()
            check_engine(packet, ident)
        except Exception as exc:  # noqa: BLE001 -- the problem is the record
            return f"{type(exc).__name__}: {exc}"[:400], None
        return None, ident

    problem, engine = integrity()
    if problem:
        raise RuntimeError(problem)
    manifest["engine"] = engine
    manifest.setdefault("launches", []).append(dict(
        at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), engine_sha=engine.get("sha"),
        cli_versions=versions if versions is not None else cli_versions(), arm=arm, only=only))
    if manifest.get("stopped") and not override_stop:
        raise RuntimeError(f"the series stopped: {manifest['stopped']['reason']} (after {manifest['stopped']['after']}); "
                           "pass --override-stop with a reason to continue")
    if manifest.get("stopped") and override_stop:
        manifest.setdefault("overrides", []).append(dict(stopped=manifest.pop("stopped"), reason=override_stop,
                                                         at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    todo = [c for c in manifest["cells"] if c["state"] == "prepared" and (only is None or c["task"] == only)
            and (arm is None or c["arm"] == arm)]
    groups: Dict[str, List[dict]] = {}
    for cell in todo:
        groups.setdefault(f"{cell['task']}-r{cell['repeat']}", []).append(cell)

    def launch(cell):
        cell["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        started = time.perf_counter()
        # Boundary 1: this cell's own launch. The identity recorded on the
        # row is the one established now, not the first launch's copied in.
        problem, ident = integrity()
        if problem:
            cell.update(state="integrity-failed", error=f"{STOP_INTEGRITY}: {problem}")
            save()
            return
        cell["engine_sha"] = ident.get("sha")
        cell["state"] = "running"
        save()  # durable before the launch: a crash here leaves "running", which the next run marks interrupted
        try:
            outcome = launcher(cell, packet)
            cell.update(outcome, state="ran")
        except Exception as exc:  # noqa: BLE001 -- a failed cell is a result, never a torn-down sibling
            cell.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:500])
        cell["seconds"] = round(time.perf_counter() - started, 1)
        cell["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save()
        # Boundary 2: before grading, the grader bytes and the engine must be
        # what prepare froze and what this cell launched on. The cell's own
        # outcome is kept; the grade is withheld, recorded as withheld.
        problem, ident = integrity()
        if not problem and ident.get("sha") != cell["engine_sha"]:
            problem = f"engine checkout moved from {cell['engine_sha'][:12]} to {str(ident.get('sha'))[:12]} during the cell"
        if problem:
            cell.update(integrity=f"before grading: {problem}", grade=None, grade_withheld=problem[:300])
            save()
            return
        cell["grade"] = grade_cell(cell, packet)
        # Boundary 3: after grading, so a grader changed while it ran is caught.
        problem, _ = integrity()
        if problem:
            cell.update(integrity=f"after grading: {problem}", grade_unverified=True)
        save()

    with ThreadPoolExecutor(max_workers=max(1, pairs) * len(ARMS)) as pool:
        batches = list(groups.values())
        for start in range(0, len(batches), max(1, pairs)):
            flat = [cell for group in batches[start:start + max(1, pairs)] for cell in group]
            list(pool.map(launch, flat))
            tripped = [(c["name"], series_stop(c, packet)) for c in flat]
            tripped = [(name, why) for name, why in tripped if why]
            if tripped:
                name, why = tripped[0]
                manifest["stopped"] = dict(reason=why, after=name,
                                           also=[f"{n}: {w}" for n, w in tripped[1:]],
                                           at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                break
    save()
    return manifest


def _read_cell(cell: dict) -> dict:
    grade = cell.get("grade") or {}
    row = dict(task=cell["task"], arm=cell["arm"], repeat=cell["repeat"], state=cell["state"],
               error=cell.get("error"), seconds=cell.get("seconds"), integrity=cell.get("integrity"),
               grade_passed=grade.get("passed"), grade_total=grade.get("total"),
               grade_withheld=cell.get("grade_withheld"), grade_unverified=bool(cell.get("grade_unverified")))
    run_dir = Path(cell["run_dir"]) if cell.get("run_dir") else None
    if not run_dir or not (run_dir / "result.json").exists():
        return row
    result = json.loads((run_dir / "result.json").read_text())
    explicit = result.get("explicit_tasks") or {}
    budget = result.get("budget") or {}
    decisions = result.get("decisions") or []
    if explicit:
        closed, unfinished = len(explicit.get("tasks_closed_clean", [])), len(explicit.get("tasks_unfinished", []))
    else:
        # A planned run: the task count and the harness's own unfinished lists.
        unfinished = len(set(result.get("turn_limited_tasks") or []) | set(result.get("failed_tasks") or []))
        closed = max(0, int(result.get("tasks") or 0) - unfinished)
    survey = result.get("survey") or {}
    def distinct(key):
        seen = []
        for d in decisions:
            if d.get("decision") == key and d.get("answer") and d["answer"] not in seen:
                seen.append(d["answer"])
        return ",".join(seen) or None
    row.update(jev_models=sorted({str((d.get("usage") or {}).get("model")) for d in decisions
                                  if (d.get("usage") or {}).get("model")}),
               decision_errors=[str(d["error"]) for d in decisions if d.get("error")],
               engine_sha=cell.get("engine_sha"), started_at=cell.get("started_at"))
    row.update(completed=result.get("completed"), result_error=result.get("error"),
               entry="listed" if explicit else "planned", tasks=int(result.get("tasks") or 0),
               closed_clean=closed, unfinished=unfinished,
               recovery_used=survey.get("recovery_used"), hypotheses=len(survey.get("hypotheses") or []),
               repeats_stopped=len(survey.get("repeats") or []), causes=survey.get("unique_causes") or [],
               checks_failed=sum(1 for c in (result.get("checks") or []) if not c.get("passed", c.get("ok", True))),
               calls=budget.get("reserved_attempts"), tokens=budget.get("reported_tokens"),
               stop=budget.get("stop_reason") or "", unknown_usage=budget.get("unknown_usage_attempts"),
               kind=distinct("task.kind"), difficulty=distinct("task.difficulty"), decisions=len(decisions),
               decision_tokens=sum((d.get("usage") or {}).get("input_tokens") or 0 for d in decisions)
               + sum((d.get("usage") or {}).get("output_tokens") or 0 for d in decisions),
               source_changed=result.get("source_changed"))
    # Per task, because a planned run's two arms need not decompose alike:
    # the lead that was actually invoked (the ledger's canonical model, never
    # a selected-but-unreached one) and the decider's labels for that task.
    per_task: Dict[str, dict] = {}
    orchestrators: List[str] = []
    inv = run_dir / "invocations.jsonl"
    if inv.exists():
        for line in inv.read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            model = entry.get("canonical_model") or entry.get("resolved_model") or entry.get("requested_model")
            role = str(entry.get("role", ""))
            if not model or not entry.get("invoked", True):
                continue
            if role.startswith("orchestrator"):
                if model not in orchestrators:
                    orchestrators.append(model)
                continue
            if not role.startswith("lead"):
                continue
            per_task.setdefault(str(entry.get("task") or "?"), dict(lead=model))
    row["orchestrators"] = orchestrators
    for d in decisions:
        slot = per_task.setdefault(str(d.get("task") or "?"), dict(lead=None))
        field = {"task.kind": "kind", "task.difficulty": "difficulty"}.get(d.get("decision"))
        if field and d.get("answer"):
            slot[field] = d["answer"]
    leads: List[str] = []
    for slot in per_task.values():
        if slot.get("lead") and slot["lead"] not in leads:
            leads.append(slot["lead"])
    row["lead"] = leads[0] if leads else None
    row["leads"] = leads
    row["per_task"] = [dict(task=t, **slot) for t, slot in sorted(per_task.items())]
    row.update(_write_denials(run_dir, cell.get("project")))
    return row


def _write_denials(run_dir: Path, project_root: Optional[Path] = None) -> dict:
    """What the run's trace.jsonl says about each invoked lead call's writes,
    judged against the cell's own project root (Codex review of f09c832):
    a denied project write beside a successful scratch write is still a
    lead that could not do its job, and an invoked lead with no usable
    trace is missing evidence, never zero denials."""
    out = dict(write_denials=0, outside_write_denials=0, leads_denied_writes=[], lead_files_written=0,
               outside_files_written=0, uncertain_files_written=0, lead_traces_missing=[], leads_invoked=0,
               trace_complete=False)
    root = Path(project_root).resolve() if project_root else None

    def place(path, cwd) -> Optional[bool]:
        """True inside the project, False outside, None unknown. A relative
        path is resolved against the call's recorded cwd (the project root
        when none was recorded), traversal and symlinks followed, before
        containment is tested (Codex re-review of 10bb9b4: a relative
        ../scratch write, or one through a project symlink, is not a
        project write). A withheld path proves nothing either way."""
        if not path or root is None:
            return None
        text = str(path)
        base = Path(cwd) if cwd else root
        try:
            candidate = Path(text) if text.startswith("/") else base / text
            return candidate.resolve().is_relative_to(root)
        except (OSError, ValueError, RuntimeError):
            return None

    invoked = []
    inv = run_dir / "invocations.jsonl"
    if inv.exists():
        for line in inv.read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if str(entry.get("role", "")).startswith("lead") and entry.get("invoked"):
                invoked.append(entry)
    out["leads_invoked"] = len(invoked)
    traces: Dict[str, dict] = {}
    trace = run_dir / "trace.jsonl"
    if trace.exists():
        for line in trace.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("invocation_id"):
                traces[str(rec["invocation_id"])] = rec
    for entry in invoked:
        iid = str(entry.get("invocation_id") or "")
        rec = traces.get(iid)
        label = dict(task=entry.get("task"), invocation_id=iid or None,
                     model=entry.get("canonical_model") or entry.get("resolved_model") or entry.get("requested_model"))
        if rec is None:
            out["lead_traces_missing"].append(dict(label, reason="no trace record"))
            continue
        if rec.get("tool_calls") is None or rec.get("error"):
            # No tool calls, or the parser failed on the transcript: an empty
            # list beside an error is not an observation of zero denials.
            out["lead_traces_missing"].append(dict(label, reason=str(
                rec.get("error") or rec.get("transcript") or "no tool calls")[:120]))
            continue
        cwd = rec.get("cwd")
        denied_in = denied_out = 0
        for c in rec.get("tool_calls") or []:
            if c.get("name") in WRITE_TOOLS and str(c.get("outcome") or "").startswith("denied"):
                # A denied write with no usable path counts against the
                # project: it is the grant that failed, wherever it aimed.
                if place(c.get("path"), cwd) is False:
                    denied_out += 1
                else:
                    denied_in += 1
        written_in = written_out = written_unknown = 0
        for f in rec.get("files_written") or []:
            where = place(f, cwd)
            if where is True:
                written_in += 1
            elif where is False:
                written_out += 1
            else:
                written_unknown += 1
        out["write_denials"] += denied_in
        out["outside_write_denials"] += denied_out
        out["lead_files_written"] += written_in
        out["outside_files_written"] += written_out
        out["uncertain_files_written"] += written_unknown
        if denied_in and not written_in:
            out["leads_denied_writes"].append(dict(label, denied=denied_in, outside_written=written_out,
                                                   uncertain_written=written_unknown))
    out["trace_complete"] = bool(invoked) and not out["lead_traces_missing"]
    return out


def _grade_text(row: dict) -> Optional[str]:
    return f"{row['grade_passed']}/{row['grade_total']}" if row.get("grade_total") is not None else None


def _comparability(j: dict, r: dict) -> dict:
    """Whether a pair's two cells ran under the same conditions: both ran,
    same engine, same orchestrator seat, no unknown usage, Jev not refused.
    A pair that is not comparable is still reported, marked."""
    reasons = []
    for row in (j, r):
        if row["state"] != "ran":
            reasons.append(f"{row['arm']} {row['state']}")
        if row.get("integrity"):
            reasons.append(f"{row['arm']} integrity: {row['integrity'][:80]}")
        access = classify_access_failure(row.get("result_error"))
        if access:
            reasons.append(f"{row['arm']} {access}: {str(row['result_error'])[:80]}")
        if str(row.get("result_error") or "").startswith("CapabilityUnavailable"):
            reasons.append(f"{row['arm']} capability: {str(row['result_error'])[:80]}")
        for lead in row.get("leads_denied_writes") or []:
            reasons.append(f"{row['arm']} lead {lead.get('model')} denied {lead.get('denied')} project write(s) on {lead.get('task')}")
        for lead in row.get("lead_traces_missing") or []:
            reasons.append(f"{row['arm']} lead {lead.get('model')} on {lead.get('task')} has no usable trace: {lead.get('reason')}")
        if row.get("state") == "ran" and not row.get("leads_invoked"):
            reasons.append(f"{row['arm']} no invoked lead to check")
        if (row.get("unknown_usage") or 0) > 0:
            reasons.append(f"{row['arm']} unknown usage {row['unknown_usage']}")
    if j.get("engine_sha") and r.get("engine_sha") and j["engine_sha"] != r["engine_sha"]:
        reasons.append("different engine SHAs")
    if j.get("orchestrators") and r.get("orchestrators") and set(j["orchestrators"]) != set(r["orchestrators"]):
        reasons.append(f"orchestrator seats differ: jev {'+'.join(j['orchestrators'])}, rule {'+'.join(r['orchestrators'])}")
    if j.get("decision_errors"):
        reasons.append(f"jev refused {len(j['decision_errors'])} decision(s)")
    gap = None
    try:
        from datetime import datetime
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        gap = abs((datetime.strptime(j["started_at"], fmt) - datetime.strptime(r["started_at"], fmt)).total_seconds())
    except (KeyError, TypeError, ValueError):
        pass
    return dict(comparable=not reasons, not_comparable_because=reasons, start_gap_seconds=gap)


def collect(out: Path) -> dict:
    manifest = json.loads((out / "manifest.json").read_text())
    rows = [_read_cell(c) for c in manifest["cells"]]
    pairs = []
    for task in {r["task"] for r in rows}:
        for repeat in sorted({r["repeat"] for r in rows if r["task"] == task}):
            by_arm = {r["arm"]: r for r in rows if r["task"] == task and r["repeat"] == repeat}
            if set(by_arm) == set(ARMS):
                j, r = by_arm["jev"], by_arm["rule"]
                pairs.append(dict(task=task, repeat=repeat, **_comparability(j, r),
                                  lead_changed=(j.get("leads") != r.get("leads")) if j.get("leads") and r.get("leads") else None,
                                  both_completed=bool(j.get("completed")) and bool(r.get("completed")),
                                  grade=dict(jev=_grade_text(j), rule=_grade_text(r)),
                                  grade_delta=((j.get("grade_passed") or 0) - (r.get("grade_passed") or 0))
                                  if j.get("grade_total") is not None and r.get("grade_total") is not None else None,
                                  completed=dict(jev=j.get("completed"), rule=r.get("completed")),
                                  tokens_delta=(j.get("tokens") or 0) - (r.get("tokens") or 0)
                                  if j.get("tokens") is not None and r.get("tokens") is not None else None,
                                  seconds_delta=(j.get("seconds") or 0) - (r.get("seconds") or 0)
                                  if j.get("seconds") is not None and r.get("seconds") is not None else None))
    summary = dict(packet_digest=manifest["packet_digest"], engine=manifest.get("engine"),
                   stopped=manifest.get("stopped"), overrides=manifest.get("overrides", []), cells=len(rows),
                   ran=sum(r["state"] == "ran" for r in rows), failed=sum(r["state"] == "failed" for r in rows),
                   interrupted=sum(r["state"] == "interrupted" for r in rows),
                   integrity_failed=sum(1 for r in rows if r["state"] == "integrity-failed" or r.get("integrity")),
                   pending=sum(r["state"] == "prepared" for r in rows), rows=rows, pairs=sorted(pairs, key=lambda p: (p["task"], p["repeat"])),
                   boundary="Paired comparison on frozen goals: the grade column is the frozen independent graders' "
                            "verdict, 'engine done' is the engine's own completion claim and never the grade; no "
                            "accuracy or savings claim beyond these cells, and a faster failed cell is not a win")
    (out / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "comparison.md").write_text(render(summary))
    return summary


def render(summary: dict) -> str:
    lines = ["| task | arm | r | grade | engine done | tasks | closed | checks failed | recovery/hyp | leads | kind/difficulty | calls | tokens | s | stop |",
             "|---|---|---:|---|---|---:|---:|---:|---|---|---|---:|---:|---:|---|"]
    for r in sorted(summary["rows"], key=lambda r: (r["task"], r["repeat"], r["arm"])):
        if r["arm"] == "jev":
            label = f"{r.get('kind') or '-'}/{r.get('difficulty') or '-'}"
        else:
            label = "orchestrator" if r.get("entry") == "planned" else "default"
        recovery = (f"{r['recovery_used']}/{r.get('hypotheses', 0)}" if r.get("recovery_used") is not None else "")
        lines.append(f"| {r['task']} | {r['arm']} | {r['repeat']} | {_grade_text(r) or ''} | {r.get('completed', r['state'])} | {r.get('tasks', '')} | "
                     f"{r.get('closed_clean', '')} | {r.get('checks_failed', '')} | {recovery} | "
                     f"{'+'.join(r.get('leads') or [])} | {label} | "
                     f"{r.get('calls', '')} | {r.get('tokens', '')} | {r.get('seconds', '')} | {r.get('stop') or r.get('error') or ''} |")
    planned = [r for r in summary["rows"] if r.get("per_task")]
    if planned:
        lines += ["", "| task | arm | engine task | lead | kind | difficulty |", "|---|---|---|---|---|---|"]
        for r in sorted(planned, key=lambda r: (r["task"], r["repeat"], r["arm"])):
            for t in r["per_task"]:
                lines.append(f"| {r['task']} | {r['arm']} | {t['task']} | {t.get('lead') or ''} | "
                             f"{t.get('kind') or ''} | {t.get('difficulty') or ''} |")
    lines += ["", "| pair | comparable | grade jev | grade rule | lead changed | both engine done | jev done | rule done | tokens (jev - rule) | seconds (jev - rule) | start gap s |",
              "|---|---|---|---|---|---|---|---|---:|---:|---:|"]
    for p in summary["pairs"]:
        comparable = "yes" if p["comparable"] else "no: " + "; ".join(p["not_comparable_because"])
        lines.append(f"| {p['task']} r{p['repeat']} | {comparable} | {p['grade']['jev'] or ''} | {p['grade']['rule'] or ''} | "
                     f"{p['lead_changed']} | {p['both_completed']} | "
                     f"{p['completed']['jev']} | {p['completed']['rule']} | {p['tokens_delta']} | {p['seconds_delta']} | "
                     f"{p['start_gap_seconds'] if p['start_gap_seconds'] is not None else ''} |")
    if summary.get("stopped"):
        lines += ["", f"Series stopped: {summary['stopped']['reason']} (after {summary['stopped']['after']})."]
    lines += ["", f"Cells {summary['cells']}: ran {summary['ran']}, failed {summary['failed']}, "
              f"interrupted {summary.get('interrupted', 0)}, pending {summary['pending']}. "
              f"Packet {summary['packet_digest'][:12]}. {summary['boundary']}", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--packet", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--pairs", type=int, default=1, help="pairs launched together (both arms of each pair always start together)")
    r.add_argument("--only", help="run only this task id's cells")
    r.add_argument("--arm", choices=ARMS, default=None, help="run only this arm's cells (the other arm stays prepared)")
    r.add_argument("--override-stop", metavar="REASON", default=None,
                   help="continue past a recorded series stop; the reason is recorded on the manifest")
    c = sub.add_parser("collect")
    c.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.out.is_absolute():
        parser.error("--out must be absolute")
    if args.command == "prepare":
        manifest = prepare(json.loads(args.packet.read_text()), args.out)
        print(f"prepared {len(manifest['cells'])} cells under {args.out}")
    elif args.command == "run":
        manifest = run(args.out, pairs=args.pairs, only=args.only, arm=args.arm, override_stop=args.override_stop)
        print(json.dumps({c["name"]: c["state"] for c in manifest["cells"]}, indent=2))
        if manifest.get("stopped"):
            print(f"series stopped: {manifest['stopped']['reason']} (after {manifest['stopped']['after']})")
    else:
        print(render(collect(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
