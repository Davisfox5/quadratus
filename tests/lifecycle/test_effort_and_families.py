"""Effort and family resolution through the real controller and the fake
vendor CLIs (2026-10-24): the argv every seat is launched with, the ledger
row each call leaves, the run's model record, and what a run does when the
installed client cannot resolve a family."""
import json
from pathlib import Path

import pytest

from tests.conftest import write_codex_catalog
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, Script
from tests.lifecycle.test_proof_binding import CASE, NEGATIVE, _at, _lead, _plan, _proofs, _scope
from tests.lifecycle.test_workflow_repair import CODE_FINDING, MET, NEG_TESTS


def _effort(call):
    """The effort a fake CLI was launched with, read from its argv in the
    vendor's own spelling (claude/grok --effort, codex -c model_reasoning_effort=)."""
    argv = call.argv
    for i, arg in enumerate(argv):
        if arg == "--effort" and i + 1 < len(argv):
            return argv[i + 1]
        if arg == "-c" and i + 1 < len(argv) and argv[i + 1].startswith("model_reasoning_effort="):
            return argv[i + 1].partition("=")[2]
    return None


def _model(call):
    return call.argv[call.argv.index("--model") + 1] if "--model" in call.argv else ""


def _ledger(replay):
    return [json.loads(line) for line in (Path(replay.result.run_dir) / "invocations.jsonl").read_text().splitlines()
            if json.loads(line).get("invoked")]


def _script(collaborator=None, revision=None, recheck=None):
    script = Script(orchestrator=lambda c, r: _plan(_scope(proofs=_proofs("R1"))) if len(r.of("orchestrator")) == 1 else "DONE",
                    lead=_lead(), collaborator=collaborator or (lambda c, r: "NO FINDINGS" + MET),
                    auditor=lambda c, r: f"R1: MET - {CASE}", **{"requirements-review": lambda c, r: "COMPLETE"})
    if revision is not None:
        script.overrides["revision"] = revision
    if recheck is not None:
        script.overrides["recheck"] = recheck
    return script


@pytest.mark.requirements_ledger
def test_every_seat_is_launched_with_an_explicit_effort_and_the_family_wire_model(tmp_path, monkeypatch):
    replay = H.run(_at(tmp_path, "explicit"), monkeypatch, _script(), files=FILES, max_tasks=1, record_complete=False)
    by_role = {}
    for call in replay.calls:
        by_role.setdefault(call.role, []).append(call)
        assert _effort(call), (call.role, call.argv)
    assert set(by_role) >= {"orchestrator", "requirements-review", "lead", "collaborator", "auditor"}
    assert _effort(by_role["orchestrator"][0]) == "high" and _model(by_role["orchestrator"][0]) == "fable"
    assert _effort(by_role["lead"][0]) == "medium", "ordinary implementation (simple difficulty)"
    assert _effort(by_role["collaborator"][0]) == "medium", "the review floor"
    assert _effort(by_role["auditor"][0]) == "high"
    assert _effort(by_role["requirements-review"][0]) == "medium"
    codex = [c for c in replay.calls if c.vendor == "codex"]
    assert codex and all(_model(c) == "gpt-6.1-sol" for c in codex), "the catalogue's latest Sol, never 5.6"
    assert all("--effort" not in c.argv for c in codex), "codex spells it as a config override"
    rows = _ledger(replay)
    assert all(r["effort"] and r["effort_policy"] == "effort/1" and r["effort_reasons"] for r in rows)
    sol = next(r for r in rows if r["requested_model"] == "openai:sol")
    assert sol["wire_model"] == "gpt-6.1-sol" and sol["resolution_source"] == "latest sol in the installed client's catalogue"
    record = H.result_json(replay)["models"]
    assert record["resolutions"]["openai:sol"]["wire"] == "gpt-6.1-sol"
    assert record["resolutions"]["openai:sol"]["provenance"]["catalog"]["status"] == "ok"
    assert record["resolutions"]["claude:fable"]["source"] == "family alias; the CLI resolves it"
    assert record["codex_catalog"]["client_version"] == "0.163.0"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_a_repeated_fix_cycle_raises_the_editor_and_the_recheck_explicitly(tmp_path, monkeypatch):
    rechecks = []

    def revision(call, replay):
        H.write(call, {"tests/test_app.py": NEG_TESTS})
        return f"Added.\nPROVES: P1 - {CASE}\nPROVES: P1 - {NEGATIVE}\nCHANGED: {json.dumps(['tests/test_app.py'])}"

    def recheck(call, replay):
        rechecks.append(call)
        return "RFT1-1: UNRESOLVED still" if len(rechecks) == 1 else "RFT1-1: RESOLVED now"
    replay = H.run(_at(tmp_path, "cycles"), monkeypatch,
                   _script(collaborator=lambda c, r: CODE_FINDING + MET, revision=revision, recheck=recheck),
                   files=FILES, max_tasks=1, record_complete=False)
    revisions = [c for c in replay.calls if c.role == "revision"]
    assert [_effort(c) for c in revisions] == ["medium", "high"]
    assert [_effort(c) for c in rechecks] == ["medium", "high"]
    rows = _ledger(replay)
    second = [r for r in rows if r["role"] == "revision"][1]
    assert any("fix cycle 2" in reason for reason in second["effort_reasons"])
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_difficulty_raises_the_lead_and_its_reviewers_and_security_work_runs_high_throughout(tmp_path, monkeypatch):
    plan = _plan(_scope(proofs=_proofs("R1"))).replace("KIND: backend simple", "KIND: backend complex")
    script = _script()
    script.overrides["orchestrator"] = lambda c, r: plan if len(r.of("orchestrator")) == 1 else "DONE"
    replay = H.run(_at(tmp_path, "complex"), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    leads = [c for c in replay.calls if c.role == "lead"]
    reviews = [c for c in replay.calls if c.role == "collaborator"]
    assert leads and all(_effort(c) == "high" for c in leads), [c.argv for c in leads]
    assert reviews and all(_effort(c) == "high" for c in reviews), "difficult review: the whole brain trust, at high"
    # Security work takes the excursion: the Sol lead and the cross-vendor verifier, both high.
    plan = _plan(_scope(proofs=_proofs("R1"))).replace("KIND: backend simple", "KIND: security simple")
    script = _script()
    script.overrides["orchestrator"] = lambda c, r: plan if len(r.of("orchestrator")) == 1 else "DONE"
    replay = H.run(_at(tmp_path, "security"), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    roles = {c.role: _effort(c) for c in replay.calls}
    assert roles["lead"] == "high" and roles["verifier"] == "high", roles
    assert all(_effort(c) for c in replay.calls)


@pytest.mark.requirements_ledger
def test_a_stale_catalogue_leaves_openai_unavailable_with_the_reason_and_sends_nothing_older(tmp_path, monkeypatch):
    stale = write_codex_catalog(tmp_path / "stale.json", fetched_at="2026-09-01T00:00:00Z")
    monkeypatch.setenv("QUADRATUS_CODEX_CATALOG", str(stale))
    replay = H.run(_at(tmp_path, "stale"), monkeypatch, _script(), files=FILES, max_tasks=1, record_complete=False)
    assert [c for c in replay.calls if c.vendor == "codex"] == [], "no codex call on a stale catalogue"
    record = H.result_json(replay)["models"]
    sol = record["resolutions"]["openai:sol"]
    assert sol["status"] == "unavailable" and sol["wire"] is None and sol["reason"].startswith("catalogue stale")
    assert record["codex_catalog"]["status"] == "stale"
    # The other vendors still carry the run, each with an explicit effort.
    assert all(_effort(c) for c in replay.calls) and {c.vendor for c in replay.calls} <= {"claude", "grok"}


@pytest.mark.requirements_ledger
def test_a_future_release_in_the_catalogue_is_what_the_run_sends(tmp_path, monkeypatch):
    later = write_codex_catalog(tmp_path / "later.json", models=[
        dict(slug="gpt-7-sol", supported_reasoning_levels=["low", "medium", "high", "xhigh"]),
        dict(slug="gpt-6.1-sol", supported_reasoning_levels=["low", "medium", "high"]),
        dict(slug="gpt-6-astra", supported_reasoning_levels=["low", "medium", "high"]),
        dict(slug="gpt-6-luna", supported_reasoning_levels=["low", "medium", "high"]),
        dict(slug="gpt-5.6-terra", supported_reasoning_levels=["low", "medium", "high"]),
    ])
    monkeypatch.setenv("QUADRATUS_CODEX_CATALOG", str(later))
    replay = H.run(_at(tmp_path, "future"), monkeypatch, _script(), files=FILES, max_tasks=1, record_complete=False)
    codex = [c for c in replay.calls if c.vendor == "codex"]
    assert codex and all(_model(c) == "gpt-7-sol" for c in codex)
    assert H.result_json(replay)["models"]["resolutions"]["openai:sol"]["wire"] == "gpt-7-sol"


@pytest.mark.requirements_ledger
def test_an_operator_pin_is_a_visible_exception_on_the_record(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_ALIAS_OPENAI_SOL", "gpt-6-sol")
    replay = H.run(_at(tmp_path, "pinned"), monkeypatch, _script(), files=FILES, max_tasks=1, record_complete=False)
    codex = [c for c in replay.calls if c.vendor == "codex"]
    assert codex and all(_model(c) == "gpt-6-sol" for c in codex)
    sol = H.result_json(replay)["models"]["resolutions"]["openai:sol"]
    assert sol["source"] == "operator override" and sol["provenance"]["variable"] == "QUADRATUS_ALIAS_OPENAI_SOL"
    assert sol["supported"] == ["low", "medium", "high", "xhigh"], "the pinned slug's own ladder"


@pytest.mark.requirements_ledger
def test_the_budget_record_does_not_change_with_effort(tmp_path, monkeypatch):
    """Effort is a call parameter, never a budget input: the same run shape
    admits the same stages and reserves the same attempts whatever the
    difficulty label selected."""
    low = H.run(_at(tmp_path, "simple"), monkeypatch, _script(), files=FILES, max_tasks=1, record_complete=False)
    plan = _plan(_scope(proofs=_proofs("R1"))).replace("KIND: backend simple", "KIND: backend complex")
    script = _script()
    script.overrides["orchestrator"] = lambda c, r: plan if len(r.of("orchestrator")) == 1 else "DONE"
    high = H.run(_at(tmp_path, "complex"), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    assert {_effort(c) for c in low.calls if c.role == "lead"} == {"medium"}
    assert {_effort(c) for c in high.calls if c.role == "lead"} == {"high"}
    budgets = [json.loads((Path(r.result.run_dir) / "budget.json").read_text()) for r in (low, high)]
    assert budgets[0]["limits"] == budgets[1]["limits"]
    stages = [[a["stages"] for a in H.result_json(r)["plan_admissions"] if a["scope"] == "mandatory"] for r in (low, high)]
    assert "effort" not in json.dumps(stages) and len(stages[0]) == len(stages[1]) == 1
