"""The Stage B pair runner, offline: a throwaway git project, a fake launcher
that writes a result.json the way run_project does, no provider."""
import json
import subprocess
from pathlib import Path

import pytest

from tools.jev_stage_b import pairs as P

#: A clean engine checkout at a known SHA, so the offline tests never depend
#: on the state of the checkout they run from.
ENGINE = dict(root="/engine", sha="e" * 40, dirty=False)


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
            budget=dict(reserved_attempts=o.get("calls", 6), reported_tokens=o.get("tokens", 300_000), stop_reason="",
                        unknown_usage_attempts=o.get("unknown_usage", 0)),
            decisions=decisions)))
        (run_dir / "invocations.jsonl").write_text("\n".join([
            json.dumps(dict(task="run", role="orchestrator", canonical_model=o.get("orchestrator", "claude:fable"), invoked=True)),
            json.dumps(dict(task="t1", role="lead", canonical_model=o.get("lead", "grok:default"), invoked=True))]) + "\n")
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
    manifest = P.run(out, engine=ENGINE, launcher=_fake_launcher({("feat-1", "jev"): {"raise": True}}))
    states = {c["name"]: c["state"] for c in manifest["cells"]}
    assert states == {"feat-0/jev-r0": "ran", "feat-0/rule-r0": "ran", "feat-1/rule-r0": "ran", "feat-1/jev-r0": "failed"}
    assert "provider down" in next(c for c in manifest["cells"] if c["state"] == "failed")["error"]
    # A cell error is a series stop (plan, stop rules): recorded, and the next
    # run refuses until an operator overrides it. Nothing is re-run either way.
    assert manifest["stopped"]["reason"].startswith("cell-error: RuntimeError: provider down")
    with pytest.raises(RuntimeError, match="series stopped"):
        P.run(out, engine=ENGINE, launcher=_fake_launcher({}))
    again = P.run(out, engine=ENGINE, launcher=_fake_launcher({}), override_stop="test: nothing left")
    assert {c["name"]: c["state"] for c in again["cells"]} == states


def test_collect_pairs_arms_and_reports_lead_changes_and_deltas(tmp_path):
    repo = _project(tmp_path)
    out = tmp_path / "runs"
    P.prepare(_packet(repo), out)
    P.run(out, engine=ENGINE, launcher=_fake_launcher({
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
    assert "| feat-0 | jev | 0 |  | True |" in text and "security/simple" in text and "faster failed cell is not a win" in text


# ---- Davis, 2026-10-02: deep planned features, failures logged, one arm at a time

def test_a_planned_task_launches_the_goal_with_the_survey_and_the_jev_arm_labels_all(tmp_path):
    repo = _project(tmp_path)
    packet = _packet(repo, tasks=0)
    packet.update(max_tasks=8, survey_recovery=4,
                  tasks=[dict(id="deep-0", goal="REQUIREMENTS:\nR1 a\nR2 b\n", declared_paths=["app.py", "tests/"])])
    out = tmp_path / "runs"
    manifest = P.prepare(packet, out)
    cell = manifest["cells"][0]
    assert cell["entry"] == "planned" and "tasks_file" not in cell
    assert Path(cell["goal_file"]).read_text() == "REQUIREMENTS:\nR1 a\nR2 b\n"
    from quadratus.session import SurveyConfig
    jev, rule = (c for c in manifest["cells"] if c["arm"] == "jev"), (c for c in manifest["cells"] if c["arm"] == "rule")
    assert P.launch_shape(next(jev), packet) == dict(max_tasks=8, survey=SurveyConfig(recovery_tasks=4), decider_labels="all")
    assert P.launch_shape(next(rule), packet) == dict(max_tasks=8, survey=SurveyConfig(recovery_tasks=4))
    listed = dict(entry="listed", text="SCOPE: {}\nBuild it.\nCOVERS: R1", arm="jev")
    assert P.launch_shape(listed, packet) == dict(tasks=[listed["text"]], max_tasks=1)
    bad = dict(packet, tasks=[dict(id="x", goal="R1 no block\n")])
    with pytest.raises(ValueError, match="REQUIREMENTS"):
        P.prepare(bad, tmp_path / "b")
    bad = dict(packet, survey_recovery=0)
    with pytest.raises(ValueError, match="survey_recovery"):
        P.prepare(bad, tmp_path / "c")


def test_run_can_take_one_arm_first_and_collect_reads_a_planned_result(tmp_path):
    repo = _project(tmp_path)
    packet = _packet(repo, tasks=0)
    packet.update(max_tasks=6, survey_recovery=3, tasks=[dict(id="deep-0", goal="REQUIREMENTS:\nR1 a\n")])
    out = tmp_path / "runs"
    P.prepare(packet, out)

    def planned_launcher(cell, _packet):
        run_dir = Path(cell["project"]) / ".quadratus" / "runs" / "r"
        run_dir.mkdir(parents=True)
        decisions = [dict(task="t1", decision="task.kind", answer="backend", usage={}),
                     dict(task="t2", decision="task.kind", answer="test", usage={}),
                     dict(task="t1", decision="task.difficulty", answer="standard", usage={})] if cell["arm"] == "jev" else []
        (run_dir / "result.json").write_text(json.dumps(dict(
            completed=False, error="x", checks=[dict(passed=False)], source_changed=True, tasks=4,
            turn_limited_tasks=["t2"], failed_tasks=["t3"], explicit_tasks=None,
            survey=dict(recovery_used=2, hypotheses=[dict(task="t3"), dict(task="t4")], repeats=[], unique_causes=["cap", "checks"]),
            budget=dict(reserved_attempts=30, reported_tokens=900_000, stop_reason="", unknown_usage_attempts=0),
            decisions=decisions)))
        (run_dir / "invocations.jsonl").write_text("\n".join([
            json.dumps(dict(task="t1", role="lead", canonical_model="openai:gpt-5.6-sol", invoked=True)),
            json.dumps(dict(task="t1", role="reviewer:a", canonical_model="anthropic:opus", invoked=True)),
            json.dumps(dict(task="t2", role="lead", requested_model="grok:default", invoked=True)),
            json.dumps(dict(task="t3", role="lead", canonical_model="anthropic:opus", invoked=False, selected=True))]) + "\n")
        return dict(run_dir=str(run_dir), completed=False)

    with pytest.raises(ValueError, match="arm"):
        P.run(out, engine=ENGINE, launcher=planned_launcher, arm="codex")
    manifest = P.run(out, engine=ENGINE, launcher=planned_launcher, arm="rule")
    assert {c["name"]: c["state"] for c in manifest["cells"]} == {"deep-0/jev-r0": "prepared", "deep-0/rule-r0": "ran"}
    P.run(out, engine=ENGINE, launcher=planned_launcher, arm="jev")
    summary = P.collect(out)
    jev = next(r for r in summary["rows"] if r["arm"] == "jev")
    assert (jev["entry"], jev["tasks"], jev["closed_clean"], jev["unfinished"]) == ("planned", 4, 2, 2)
    assert jev["recovery_used"] == 2 and jev["hypotheses"] == 2 and jev["causes"] == ["cap", "checks"]
    assert jev["kind"] == "backend,test" and jev["difficulty"] == "standard" and jev["decisions"] == 3
    assert jev["leads"] == ["openai:gpt-5.6-sol", "grok:default"]  # t3's lead was selected, never invoked
    assert jev["per_task"] == [dict(task="t1", lead="openai:gpt-5.6-sol", kind="backend", difficulty="standard"),
                               dict(task="t2", lead="grok:default", kind="test")]
    assert summary["pairs"][0]["lead_changed"] is False
    text = (out / "comparison.md").read_text()
    assert "| 2/2 | openai:gpt-5.6-sol+grok:default | backend,test/standard |" in text and "| orchestrator |" in text
    assert "| deep-0 | jev | t2 | grok:default | test |  |" in text


# ---- Codex and the Mac session on #35 (2026-10-03): the runner's own controls

def test_run_refuses_a_changed_packet_a_dirty_or_wrong_engine_and_records_the_engine(tmp_path):
    repo = _project(tmp_path)
    out = tmp_path / "runs"
    P.prepare(_packet(repo), out)
    with pytest.raises(RuntimeError, match="uncommitted"):
        P.run(out, engine=dict(ENGINE, dirty=True), launcher=_fake_launcher({}))
    packet_sha = dict(_packet(repo), engine_sha="f" * 40)
    out2 = tmp_path / "runs2"
    P.prepare(packet_sha, out2)
    with pytest.raises(RuntimeError, match="engine_sha"):
        P.run(out2, engine=ENGINE, launcher=_fake_launcher({}))
    manifest = json.loads((out / "manifest.json").read_text())
    manifest["packet"]["limits"]["max_calls"] = 999
    (out / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="digest"):
        P.run(out, engine=ENGINE, launcher=_fake_launcher({}))
    manifest["packet"]["limits"]["max_calls"] = 20
    (out / "manifest.json").write_text(json.dumps(manifest))
    manifest = P.run(out, engine=ENGINE, launcher=_fake_launcher({}))
    assert manifest["engine"] == ENGINE and all(c["engine_sha"] == "e" * 40 for c in manifest["cells"])
    assert "stopped" not in manifest


def test_a_tripped_stop_rule_ends_the_series_before_the_next_pair_and_an_override_is_recorded(tmp_path):
    repo = _project(tmp_path)
    out = tmp_path / "runs"
    P.prepare(_packet(repo, tasks=3), out)
    launcher = _fake_launcher({("feat-0", "rule"): dict(orchestrator="openai:gpt-6-astra")})
    manifest = P.run(out, engine=ENGINE, launcher=launcher)
    states = {c["name"]: c["state"] for c in manifest["cells"]}
    assert states["feat-0/jev-r0"] == "ran" and states["feat-0/rule-r0"] == "ran"
    assert states["feat-1/rule-r0"] == "prepared" and states["feat-2/jev-r0"] == "prepared"
    assert manifest["stopped"]["reason"].startswith("seat-fallback: orchestrator answered by openai:gpt-6-astra")
    assert manifest["stopped"]["after"] == "feat-0/rule-r0"
    with pytest.raises(RuntimeError, match="series stopped"):
        P.run(out, engine=ENGINE, launcher=_fake_launcher({}))
    manifest = P.run(out, engine=ENGINE, launcher=_fake_launcher({("feat-1", "jev"): dict(unknown_usage=2)}),
                     override_stop="Davis: Astra seat accepted for this series")
    assert manifest["overrides"][0]["reason"].startswith("Davis") and "stopped" in manifest["overrides"][0]
    assert manifest["stopped"]["reason"].startswith("unknown-usage: 2 attempt(s) in feat-1/jev-r0")
    assert {c["name"]: c["state"] for c in manifest["cells"]}["feat-2/jev-r0"] == "prepared"
    summary = P.collect(out)
    pair0 = next(p for p in summary["pairs"] if p["task"] == "feat-0")
    pair1 = next(p for p in summary["pairs"] if p["task"] == "feat-1")
    assert not pair0["comparable"] and pair0["not_comparable_because"] == [
        "orchestrator seats differ: jev claude:fable, rule openai:gpt-6-astra"]
    assert not pair1["comparable"] and "jev unknown usage 2" in pair1["not_comparable_because"]
    assert pair0["start_gap_seconds"] is not None
    text = (out / "comparison.md").read_text()
    assert "no: orchestrator seats differ" in text and "Series stopped: unknown-usage" in text


def test_series_stop_reads_jev_drift_credential_failures_and_cell_errors(tmp_path):
    repo = _project(tmp_path)
    packet = dict(_packet(repo), jev_model="jev-1.13.0")
    out = tmp_path / "runs"
    P.prepare(packet, out)

    def launcher(cell, _packet):
        run_dir = Path(cell["project"]) / ".quadratus" / "runs" / "r"
        run_dir.mkdir(parents=True)
        decisions = [dict(decision="task.kind", answer="docs", usage=dict(model="jev-1.14.0", input_tokens=1, output_tokens=1))]
        (run_dir / "result.json").write_text(json.dumps(dict(completed=True, checks=[], tasks=1, budget=dict(unknown_usage_attempts=0),
                                                             decisions=decisions if cell["arm"] == "jev" else [])))
        return dict(run_dir=str(run_dir), completed=True)

    manifest = P.run(out, engine=ENGINE, launcher=launcher)
    assert manifest["stopped"]["reason"].startswith("jev-model-drift: jev-1.14.0 (packet names jev-1.13.0)")
    cell = dict(state="ran", name="x/jev-r0", arm="jev", task="x", repeat=0,
                run_dir=str(Path(manifest["cells"][0]["project"]) / ".quadratus" / "runs" / "r"))
    credential = Path(cell["run_dir"]) / "result.json"
    credential.write_text(json.dumps(dict(completed=True, checks=[], tasks=1, budget={},
                                          decisions=[dict(decision="task.kind", answer=None, error="no TYPESAFE_API_KEY: Jev is a billed API")])))
    assert P.series_stop(cell, packet).startswith("credential-failure: no TYPESAFE_API_KEY")
    assert P.series_stop(dict(state="failed", name="y", error="OrchestratorUnavailable: both seats down"), packet).startswith("seat-fallback")
    assert P.series_stop(dict(state="failed", name="y", error="RuntimeError: disk full"), packet).startswith("cell-error")
    assert P.series_stop(dict(state="prepared", name="z"), packet) is None


# ---- Davis via Codex (2026-10-03): frozen independent graders outside the builders' scope

def _graders(tmp_path: Path) -> Path:
    gdir = tmp_path / "graders"
    gdir.mkdir()
    (gdir / "grade_feat.py").write_text(
        "import os, sys\n"
        "project = os.environ['STAGE_B_PROJECT']\n"
        "assert os.environ['STAGE_B_ARM'] in ('jev', 'rule')\n"
        "sys.exit(0 if 'built' in open(os.path.join(project, 'app.py')).read() else 1)\n")
    return gdir


def test_prepare_freezes_graders_and_profile_and_run_refuses_a_change(tmp_path):
    repo = _project(tmp_path)
    gdir = _graders(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    packet = _packet(repo)
    packet.update(capture_profile=str(profile),
                  graders=dict(dir=str(gdir), tasks={"feat-0": [["python3", "grade_feat.py"]], "feat-1": [["python3", "grade_feat.py"]]}))
    out = tmp_path / "runs"
    manifest = P.prepare(packet, out)
    assert set(manifest["frozen"]["graders"]) == {"grade_feat.py"} and manifest["frozen"]["capture_profile"]
    (gdir / "grade_feat.py").write_text("import sys; sys.exit(0)\n")
    with pytest.raises(RuntimeError, match="frozen inputs changed since prepare: grade_feat.py"):
        P.run(out, engine=ENGINE, launcher=_fake_launcher({}), versions={})
    inside = dict(packet, graders=dict(dir=str(repo / "graders"), tasks=packet["graders"]["tasks"]))
    with pytest.raises(ValueError, match="outside the project"):
        P.prepare(inside, tmp_path / "b")
    partial = dict(packet, graders=dict(dir=str(gdir), tasks={"feat-0": [["python3", "grade_feat.py"]]}))
    with pytest.raises(ValueError, match="missing: feat-1"):
        P.prepare(partial, tmp_path / "c")


def test_graders_run_after_each_cell_from_their_own_directory_and_the_grade_is_reported_apart_from_completion(tmp_path):
    repo = _project(tmp_path)
    gdir = _graders(tmp_path)
    packet = _packet(repo)
    packet["graders"] = dict(dir=str(gdir), tasks={"feat-0": [["python3", "grade_feat.py"]],
                                                  "feat-1": [["python3", "grade_feat.py"], ["python3", "-c", "import sys; sys.exit(3)"]]})
    out = tmp_path / "runs"
    P.prepare(packet, out)
    base = _fake_launcher({("feat-1", "rule"): dict(completed=True)})

    def launcher(cell, pk):
        outcome = base(cell, pk)
        if cell["arm"] == "jev":  # only the jev cells actually build the feature
            (Path(cell["project"]) / "app.py").write_text("built\n")
        return outcome

    manifest = P.run(out, engine=ENGINE, launcher=launcher, versions=dict(claude="1.0", codex=None, grok="0.9"))
    assert manifest["launches"][0]["cli_versions"] == dict(claude="1.0", codex=None, grok="0.9")
    grades = {c["name"]: (c["grade"]["passed"], c["grade"]["total"]) for c in manifest["cells"]}
    assert grades == {"feat-0/jev-r0": (1, 1), "feat-0/rule-r0": (0, 1), "feat-1/jev-r0": (1, 2), "feat-1/rule-r0": (0, 2)}
    failed = next(c for c in manifest["cells"] if c["name"] == "feat-1/rule-r0")["grade"]["results"][1]
    assert failed["exit"] == 3 and failed["passed"] is False
    summary = P.collect(out)
    pair1 = next(p for p in summary["pairs"] if p["task"] == "feat-1")
    # The engine claimed completion on the rule cell; the grader did not agree. Both are shown.
    assert pair1["completed"] == dict(jev=True, rule=True) and pair1["grade"] == dict(jev="1/2", rule="0/2") and pair1["grade_delta"] == 1
    text = (out / "comparison.md").read_text()
    assert "| feat-1 | rule | 0 | 0/2 | True |" in text and "never the grade" in text


def test_one_run_per_series_and_a_cell_is_running_on_disk_before_its_launch_finishes(tmp_path):
    repo = _project(tmp_path)
    out = tmp_path / "runs"
    P.prepare(_packet(repo, tasks=1), out)
    (out / P.LOCK_NAME).write_text("pid=1 at=earlier\n")
    with pytest.raises(RuntimeError, match="another run holds"):
        P.run(out, engine=ENGINE, launcher=_fake_launcher({}))
    (out / P.LOCK_NAME).unlink()
    seen = {}

    def launcher(cell, packet):
        on_disk = json.loads((out / "manifest.json").read_text())
        seen[cell["name"]] = next(c["state"] for c in on_disk["cells"] if c["name"] == cell["name"])
        assert (out / P.LOCK_NAME).exists()
        return _fake_launcher({})(cell, packet)

    manifest = P.run(out, engine=ENGINE, launcher=launcher)
    assert seen == {"feat-0/jev-r0": "running", "feat-0/rule-r0": "running"}
    assert all(c["state"] == "ran" and c["finished_at"] for c in manifest["cells"])
    assert not (out / P.LOCK_NAME).exists()


def test_a_cell_left_running_by_a_dead_process_is_marked_interrupted_and_never_relaunched(tmp_path):
    repo = _project(tmp_path)
    out = tmp_path / "runs"
    P.prepare(_packet(repo, tasks=2), out)
    manifest = json.loads((out / "manifest.json").read_text())
    victim = next(c for c in manifest["cells"] if c["name"] == "feat-0/jev-r0")
    victim.update(state="running", started_at="2026-10-03T00:00:00Z")
    (out / "manifest.json").write_text(json.dumps(manifest))
    launched = []

    def launcher(cell, packet):
        launched.append(cell["name"])
        return _fake_launcher({})(cell, packet)

    manifest = P.run(out, engine=ENGINE, launcher=launcher)
    states = {c["name"]: c["state"] for c in manifest["cells"]}
    assert states["feat-0/jev-r0"] == "interrupted" and "feat-0/jev-r0" not in launched
    assert "not relaunched" in next(c for c in manifest["cells"] if c["name"] == "feat-0/jev-r0")["error"]
    assert states["feat-0/rule-r0"] == "ran" and states["feat-1/jev-r0"] == "ran" and states["feat-1/rule-r0"] == "ran"
    # A second run finds nothing to do and still does not touch the interrupted cell.
    again = P.run(out, engine=ENGINE, launcher=launcher)
    assert {c["name"]: c["state"] for c in again["cells"]} == states and len(launched) == 3
    summary = P.collect(out)
    assert summary["interrupted"] == 1
    pair = next(p for p in summary["pairs"] if p["task"] == "feat-0")
    assert pair["comparable"] is False and "jev interrupted" in pair["not_comparable_because"]
    assert "interrupted 1" in P.render(summary)


def test_each_arm_may_have_its_own_capture_profile_and_both_are_frozen(tmp_path):
    repo = _project(tmp_path)
    jev, rule = tmp_path / "jev.json", tmp_path / "rule.json"
    jev.write_text('{"origin": "http://127.0.0.1:52055"}')
    rule.write_text('{"origin": "http://127.0.0.1:52056"}')
    packet = dict(_packet(repo, tasks=1), capture_profiles=dict(jev=str(jev), rule=str(rule)))
    with pytest.raises(ValueError, match="one absolute profile per arm"):
        P.validate_packet(dict(packet, capture_profiles=dict(jev=str(jev))))
    out = tmp_path / "runs"
    manifest = P.prepare(packet, out)
    assert set(manifest["frozen"]) == {"capture_profile:jev", "capture_profile:rule"}
    cells = {c["arm"]: c for c in manifest["cells"]}
    assert P.profile_for(cells["jev"], packet) == str(jev) and P.profile_for(cells["rule"], packet) == str(rule)
    rule.write_text('{"origin": "http://127.0.0.1:52099"}')
    with pytest.raises(RuntimeError, match="frozen inputs changed"):
        P.run(out, engine=ENGINE, launcher=_fake_launcher({}))
