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
import subprocess
import sys
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
                    prepared_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), cells=cells)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


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
    result = run_project(
        cell["goal"], Project.open(cell["project"]), Settings.from_env(),
        allow_writes=True, check=packet.get("check", ""), extra_checks=extra,
        capture_profile=packet.get("capture_profile"), readiness=packet.get("readiness"),
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
    if cell.get("state") == "failed":
        text = str(cell.get("error") or "")
        if "Unavailable" in text or "sign" in text.lower() and "in" in text.lower():
            return f"{STOP_SEAT}: {text[:160]}"
        return f"{STOP_CELL_ERROR}: {text[:160]}"
    if cell.get("state") != "ran":
        return None
    row = _read_cell(cell)
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
        engine: Optional[dict] = None, override_stop: Optional[str] = None) -> dict:
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
    manifest = json.loads((out / "manifest.json").read_text())
    packet = manifest["packet"]
    if _sha(packet) != manifest.get("packet_digest"):
        raise RuntimeError("the manifest's packet no longer matches the digest written at prepare; "
                           "a packet changed after prepare is not run")
    engine = engine if engine is not None else engine_identity()
    check_engine(packet, engine)
    manifest["engine"] = engine
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
        cell["engine_sha"] = engine.get("sha")
        try:
            outcome = launcher(cell, packet)
            cell.update(outcome, state="ran")
        except Exception as exc:  # noqa: BLE001 -- a failed cell is a result, never a torn-down sibling
            cell.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:500])
        cell["seconds"] = round(time.perf_counter() - started, 1)
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

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
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _read_cell(cell: dict) -> dict:
    row = dict(task=cell["task"], arm=cell["arm"], repeat=cell["repeat"], state=cell["state"],
               error=cell.get("error"), seconds=cell.get("seconds"))
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
    return row


def _comparability(j: dict, r: dict) -> dict:
    """Whether a pair's two cells ran under the same conditions: both ran,
    same engine, same orchestrator seat, no unknown usage, Jev not refused.
    A pair that is not comparable is still reported, marked."""
    reasons = []
    for row in (j, r):
        if row["state"] != "ran":
            reasons.append(f"{row['arm']} {row['state']}")
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
                                  completed=dict(jev=j.get("completed"), rule=r.get("completed")),
                                  tokens_delta=(j.get("tokens") or 0) - (r.get("tokens") or 0)
                                  if j.get("tokens") is not None and r.get("tokens") is not None else None,
                                  seconds_delta=(j.get("seconds") or 0) - (r.get("seconds") or 0)
                                  if j.get("seconds") is not None and r.get("seconds") is not None else None))
    summary = dict(packet_digest=manifest["packet_digest"], engine=manifest.get("engine"),
                   stopped=manifest.get("stopped"), overrides=manifest.get("overrides", []), cells=len(rows),
                   ran=sum(r["state"] == "ran" for r in rows), failed=sum(r["state"] == "failed" for r in rows),
                   pending=sum(r["state"] == "prepared" for r in rows), rows=rows, pairs=sorted(pairs, key=lambda p: (p["task"], p["repeat"])),
                   boundary="Paired completion, lead and resource comparison on frozen task text; no accuracy or savings "
                            "claim beyond these cells, and a faster failed cell is not a win")
    (out / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "comparison.md").write_text(render(summary))
    return summary


def render(summary: dict) -> str:
    lines = ["| task | arm | r | done | tasks | closed | checks failed | recovery/hyp | leads | kind/difficulty | calls | tokens | s | stop |",
             "|---|---|---:|---|---:|---:|---:|---|---|---|---:|---:|---:|---|"]
    for r in sorted(summary["rows"], key=lambda r: (r["task"], r["repeat"], r["arm"])):
        if r["arm"] == "jev":
            label = f"{r.get('kind') or '-'}/{r.get('difficulty') or '-'}"
        else:
            label = "orchestrator" if r.get("entry") == "planned" else "default"
        recovery = (f"{r['recovery_used']}/{r.get('hypotheses', 0)}" if r.get("recovery_used") is not None else "")
        lines.append(f"| {r['task']} | {r['arm']} | {r['repeat']} | {r.get('completed', r['state'])} | {r.get('tasks', '')} | "
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
    lines += ["", "| pair | comparable | lead changed | both done | jev done | rule done | tokens (jev - rule) | seconds (jev - rule) | start gap s |",
              "|---|---|---|---|---|---|---:|---:|---:|"]
    for p in summary["pairs"]:
        comparable = "yes" if p["comparable"] else "no: " + "; ".join(p["not_comparable_because"])
        lines.append(f"| {p['task']} r{p['repeat']} | {comparable} | {p['lead_changed']} | {p['both_completed']} | "
                     f"{p['completed']['jev']} | {p['completed']['rule']} | {p['tokens_delta']} | {p['seconds_delta']} | "
                     f"{p['start_gap_seconds'] if p['start_gap_seconds'] is not None else ''} |")
    if summary.get("stopped"):
        lines += ["", f"Series stopped: {summary['stopped']['reason']} (after {summary['stopped']['after']})."]
    lines += ["", f"Cells {summary['cells']}: ran {summary['ran']}, failed {summary['failed']}, pending {summary['pending']}. "
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
