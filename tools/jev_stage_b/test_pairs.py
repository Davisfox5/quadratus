"""The Stage B pair runner, offline: a throwaway git project, a fake launcher
that writes a result.json the way run_project does, no provider."""
import json
import subprocess
from pathlib import Path

import pytest

from tools.jev_stage_b import pairs as P


def _project(tmp_path: Path) -> Path:
    repo = tmp_path / "gametape"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "app.py").write_text("print('baseline')\n")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=f", "-c", "user.email=f@example.invalid",
                    "commit", "-qm", "baseline"], check=True)
    return repo


def _packet(repo: Path, tasks=2, repeats=1) -> dict:
    return dict(project=dict(repo=str(repo), base_sha=P.git(repo, "rev-parse", "HEAD")),
                goal="REQUIREMENTS:\nR1 the feature\n", limits=dict(max_calls=20, max_reported_tokens=500_000, wall_seconds=600),
                repeats=repeats, tasks=[dict(id=f"feat-{i}", text=f"SCOPE: {{}}\nBuild feature {i}.\nCOVERS: R1") for i in range(tasks)])


def _fake_launcher(outcomes):
    """Writes result.json/invocations.jsonl like a run; outcomes keyed by (task, arm)."""
    def launch(cell, packet):
        o = outcomes.get((cell["task"], cell["arm"]), {})
        run_dir = Path(cell["project"]) / ".quadratus" / "runs" / "r"
        run_dir.mkdir(parents=True)
        decisions = []
        if cell["arm"] == "jev":
            decisions = [dict(decision="task.kind", answer=o.get("kind", "backend"), usage=dict(input_tokens=700, output_tokens=170)),
                         dict(decision="task.difficulty", answer=o.get("difficulty", "simple"), usage=dict(input_tokens=600, output_tokens=50))]
        (run_dir / "result.json").write_text(json.dumps(dict(
            completed=o.get("completed", True), error=None, checks=[dict(passed=True)], source_changed=True,
            explicit_tasks=dict(tasks_closed_clean=["t1"] if o.get("completed", True) else [], tasks_unfinished=[] if o.get("completed", True) else ["t1"]),
            budget=dict(reserved_attempts=o.get("calls", 6), reported_tokens=o.get("tokens", 300_000), stop_reason="", unknown_usage_attempts=0),
            decisions=decisions)))
        (run_dir / "invocations.jsonl").write_text(json.dumps(dict(role="lead", model=o.get("lead", "grok:default"))) + "\n")
        if o.get("raise"):
            raise RuntimeError("provider down")
        return dict(run_dir=str(run_dir), completed=o.get("completed", True))
    return launch


def test_prepare_makes_one_detached_checkout_per_cell_with_counterbalanced_order(tmp_path):
    repo = _project(tmp_path)
    manifest = P.prepare(_packet(repo), tmp_path / "runs")
    names = [c["name"] for c in manifest["cells"]]
    assert names == ["feat-0/jev-r0", "feat-0/rule-r0", "feat-1/rule-r0", "feat-1/jev-r0"]
    for cell in manifest["cells"]:
        assert P.git(Path(cell["project"]), "rev-parse", "HEAD") == manifest["packet"]["project"]["base_sha"]
        assert json.loads(Path(cell["tasks_file"]).read_text()) == [cell["text"]]
        assert "KIND:" not in cell["text"]
    with pytest.raises(FileExistsError):
        P.prepare(_packet(repo), tmp_path / "runs")


def test_a_kind_line_or_a_bad_limit_is_refused_before_any_checkout(tmp_path):
    repo = _project(tmp_path)
    bad = _packet(repo)
    bad["tasks"][0]["text"] = "KIND: backend simple\nBuild it."
    with pytest.raises(ValueError, match="KIND line"):
        P.prepare(bad, tmp_path / "a")
    bad = _packet(repo)
    bad["limits"]["max_calls"] = 0
    with pytest.raises(ValueError, match="max_calls"):
        P.prepare(bad, tmp_path / "b")
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_run_launches_both_arms_of_a_pair_and_a_failed_cell_never_stops_its_sibling(tmp_path):
    repo = _project(tmp_path)
    out = tmp_path / "runs"
    P.prepare(_packet(repo), out)
    manifest = P.run(out, launcher=_fake_launcher({("feat-1", "jev"): {"raise": True}}))
    states = {c["name"]: c["state"] for c in manifest["cells"]}
    assert states == {"feat-0/jev-r0": "ran", "feat-0/rule-r0": "ran", "feat-1/rule-r0": "ran", "feat-1/jev-r0": "failed"}
    assert "provider down" in next(c for c in manifest["cells"] if c["state"] == "failed")["error"]
    again = P.run(out, launcher=_fake_launcher({}))  # nothing left to run; nothing re-run
    assert {c["name"]: c["state"] for c in again["cells"]} == states


def test_collect_pairs_arms_and_reports_lead_changes_and_deltas(tmp_path):
    repo = _project(tmp_path)
    out = tmp_path / "runs"
    P.prepare(_packet(repo), out)
    P.run(out, launcher=_fake_launcher({
        ("feat-0", "jev"): dict(kind="security", difficulty="simple", lead="openai:gpt-5.6-sol", tokens=320_000),
        ("feat-0", "rule"): dict(lead="grok:default", tokens=300_000),
        ("feat-1", "jev"): dict(completed=False, tokens=100_000),
        ("feat-1", "rule"): dict(tokens=250_000)}))
    summary = P.collect(out)
    pair0 = next(p for p in summary["pairs"] if p["task"] == "feat-0")
    pair1 = next(p for p in summary["pairs"] if p["task"] == "feat-1")
    assert pair0["lead_changed"] is True and pair0["both_completed"] and pair0["tokens_delta"] == 20_000
    assert pair1["lead_changed"] is False and not pair1["both_completed"] and pair1["completed"] == dict(jev=False, rule=True)
    jev0 = next(r for r in summary["rows"] if r["task"] == "feat-0" and r["arm"] == "jev")
    assert jev0["kind"] == "security" and jev0["decision_tokens"] == 1520 and jev0["lead"] == "openai:gpt-5.6-sol"
    text = (out / "comparison.md").read_text()
    assert "| feat-0 | jev | 0 | True |" in text and "security/simple" in text and "faster failed cell is not a win" in text
