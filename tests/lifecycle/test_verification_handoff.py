"""A verification-only task over a source the harness has already verified
is not dispatched; its requirements wait for the final audit.

Overnight r02 and r03 (20261011T134747Z-b14aa88b, 20261011T152432Z-4ceef6b5):
after the feature task closed with a same-source harness pass (121 Python,
64/70 Node, none skipped) and approved renders, the orchestrator opened a
review-only t2 to verify it. t2's lead re-ran the tests where it could not
run a browser, reported failures and NOT RUN, and in r03 spent 574,721 of the
run's 1,843,534 reported tokens with no edits; the run hit its token reserve
before the requirement audit.

Whole-controller replays: real gate runs (pytest in the project), a real
harness capture where marked, scripted model replies. The handoff only marks
requirements covered; the final audit still judges every one.
"""

import dataclasses
import json
import sys
from pathlib import Path

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_audit_findings import AUDIT_SCOPE, REPAIR_SCOPE
from tests.lifecycle.test_harness_capture import (
    CAPTURE,
    FITTING_PAGE,
    _decl,
    _no_edit,
    _profile,
    _run,
    browser,
)
from tests.lifecycle.test_lifecycle_matrix import T2

pytestmark = pytest.mark.requirements_ledger

REQS = "REQUIREMENTS:\nR1: the toolbar fits a phone\nR2: the import page keeps its tests passing\n"
BUILD = _decl("KIND: frontend standard", dict(REPAIR_SCOPE, capture=CAPTURE), "Make the toolbar fit.", covers="R1")
VERIFY = _decl("KIND: frontend standard", dict(AUDIT_SCOPE, capture=CAPTURE),
               "Verify the import page and run its tests.", covers="R2")
DOCS = _decl("KIND: docs simple", T2, "Document add in README.md.", covers="R1")


def _build(call, replay):
    H.write(call, {"templates/index.html": FITTING_PAGE})
    return 'Let the toolbar shrink.\nCHANGED: ["templates/index.html"]'


def _docs(call, replay):
    H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
    return 'Documented add.\nCHANGED: ["README.md"]'


def _leads(replay, task):
    return [c for c in replay.of("lead") if c.task == task]


def _handoffs(replay):
    return H.result_json(replay)["requirements"]["verification_handoffs"]


def _report(replay):
    return (Path(replay.result.run_dir) / "report.md").read_text()


def _truth(replay):
    """The independent record check (tools/truth_check.py), run as a separate
    process that imports nothing from quadratus."""
    import subprocess
    tool = Path(__file__).resolve().parents[2] / "tools" / "truth_check.py"
    out = subprocess.run([sys.executable, "-I", str(tool), replay.result.run_dir, "--project",
                          str(replay.project), "--json"], capture_output=True, text=True, timeout=60)
    return json.loads(out.stdout)


def _not_handed_off(replay, why=None):
    assert _handoffs(replay) == []
    dispatched = H.result_json(replay)["requirements"]["verification_dispatched"]
    if why is not None:
        assert [d["why"] for d in dispatched][:1] == [why], dispatched
    assert not any("TASK NOT DISPATCHED" in c.prompt for c in replay.of("orchestrator"))
    assert "was not dispatched" not in _report(replay)


# -- positive ---------------------------------------------------------------------------

@browser
def test_a_verified_source_closes_through_the_audit_without_a_verification_task(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)
    replay = _run(tmp_path, monkeypatch, [REQS + BUILD, VERIFY], {"t1": _build, "t2": _no_edit}, profile=profile)
    assert replay.result.completed, replay.result.error
    assert _leads(replay, "t2") == [], "no lead call for the verification task"
    [handoff] = _handoffs(replay)
    assert handoff["covers"] == ["R2"] and handoff["renders"]["task"] == "t1"
    assert "passed, 0 failed, 0 skipped; on the delivered source" in handoff["check"]
    sent_back = replay.of("orchestrator")[2].prompt
    assert "TASK NOT DISPATCHED" in sent_back and "not met until the final requirement audit" in sent_back
    [audit] = replay.of("auditor")
    assert "R2: the import page keeps its tests passing" in audit.prompt
    assert "0 skipped; on the delivered source" in audit.prompt and "APPROVED" in audit.prompt
    status = H.result_json(replay)["requirements"]["status"]
    assert status == {"R1": "met (audited)", "R2": "met (audited)"}
    assert "Verification task t2 was not dispatched" in _report(replay)
    truth = _truth(replay)
    assert truth["verdict"] == "verified", truth


@browser
def test_seats_judging_the_checked_source_get_the_records_not_the_commands(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)
    replay = _run(tmp_path, monkeypatch, [REQS + BUILD, VERIFY], {"t1": _build, "t2": _no_edit}, profile=profile)
    [design] = replay.of("design-review")
    [audit] = replay.of("auditor")
    [review] = replay.of("collaborator")
    for call in (design, audit):
        # The role text reaches the transport in argv or the prompt.
        text = call.prompt + " ".join(call.argv)
        assert "THE HARNESS HAS ALREADY RUN THE PROJECT'S CHECK COMMANDS ON EXACTLY THIS SOURCE" in text
        assert "0 skipped; on the delivered source" in text and "APPROVED CHECK COMMANDS" not in text
    # The draft review comes before any check of the draft: the commands stay.
    assert "APPROVED CHECK COMMANDS" in review.prompt + " ".join(review.argv)
    second = replay.of("orchestrator")[1].prompt
    assert "## The harness's latest check and renders" in second
    assert "renders from t1: verified; design review by" in second and "APPROVED; on the delivered source" in second
    assert "is not dispatched while these show the current source passing" in second
    for call in replay.calls:
        assert "quadratus-report" not in call.prompt, "no check command line reaches a model"


@browser
def test_the_audit_still_decides_a_handed_off_requirement(tmp_path, monkeypatch):
    # Covered is not met: a NOT MET from the audit sends DONE back with the
    # requirement named, and a second verification task is then dispatched.
    profile, _ = _profile(tmp_path)
    verdicts = iter(["R1: MET - templates/index.html\nR2: NOT MET - no test exercises the import page"])
    replay = _run(tmp_path, monkeypatch, [REQS + BUILD, VERIFY, "DONE", VERIFY],
                  {"t1": _build, "t2": _no_edit}, profile=profile,
                  roles={"auditor": lambda c, r: next(verdicts, "R1: MET - templates/index.html\n"
                                                                "R2: NOT MET - still no test")})
    assert not replay.result.completed
    assert len(_handoffs(replay)) == 1, "the NOT MET requirement is not handed off again"
    assert len(_leads(replay, "t2")) == 1
    assert H.result_json(replay)["requirements"]["status"]["R2"].startswith("NOT MET")
    assert _truth(replay)["verdict"] == "not-met"


# -- negative controls: each dispatches the task as named --------------------------------

@browser
def test_a_source_changed_after_the_check_is_not_verified(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)

    def verify_after_edit(call, replay):
        (replay.project / "README.md").write_text("changed after the check\n")
        return VERIFY
    plan = [REQS + BUILD, None]
    replay = _run(tmp_path, monkeypatch, [], {"t1": _build, "t2": _no_edit}, profile=profile,
                  roles={"orchestrator": lambda c, r: (plan.pop(0) or verify_after_edit(c, r)) if plan else "DONE"})
    _not_handed_off(replay, "the latest project check ran on an earlier source")
    assert len(_leads(replay, "t2")) == 1


@browser
def test_skipped_cases_are_not_verification(tmp_path, monkeypatch):
    from tests.lifecycle.test_lifecycle_matrix import _design_files
    profile, _ = _profile(tmp_path)
    files = {**_design_files(), "templates/index.html": FITTING_PAGE.replace("tools", "old tools"),
             "tests/test_skipped.py": "import pytest\n\n\n@pytest.mark.skip(reason='needs a browser')\n"
                                      "def test_page():\n    pass\n"}
    replay = _run(tmp_path, monkeypatch, [REQS + BUILD, VERIFY], {"t1": _build, "t2": _no_edit},
                  profile=profile, files=files)
    _not_handed_off(replay, "the latest project check: it skipped cases")


@browser
def test_a_not_run_report_is_not_verification(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)

    def build_not_run(call, replay):
        reply = _build(call, replay)
        return reply.replace("Let the toolbar shrink.", "Let the toolbar shrink.\n"
                             "NOT RUN: tests/test_app.py - no Python in my workspace")
    replay = _run(tmp_path, monkeypatch, [REQS + BUILD, VERIFY], {"t1": build_not_run, "t2": _no_edit},
                  profile=profile)
    _not_handed_off(replay, "a task reported acceptance it did not run")
    assert len(_leads(replay, "t2")) == 1


@browser
def test_renders_from_an_earlier_source_are_not_verification(tmp_path, monkeypatch):
    # t2 changes the source and its check passes, but t1's approved renders
    # show the source before t2.
    profile, _ = _profile(tmp_path)
    replay = _run(tmp_path, monkeypatch, [REQS + BUILD, DOCS, VERIFY],
                  {"t1": _build, "t2": _docs, "t3": _no_edit}, profile=profile)
    _not_handed_off(replay, "the latest renders were captured from an earlier source")
    assert len(_leads(replay, "t3")) == 1


def _delete_desktop(folder):
    (folder / "desktop" / "page.png").unlink()


def _replace_desktop(folder):
    shot = folder / "desktop" / "page.png"
    shot.write_bytes(shot.read_bytes() + b"\0")


def _rewrite_summary(folder):
    # Same source fingerprint, so only the approved bytes can tell.
    summary = folder / "summary.json"
    data = json.loads(summary.read_text())
    data["target"] = str(data.get("target", "")) + "?changed"
    summary.write_text(json.dumps(data))


@browser
@pytest.mark.parametrize("mutate, why", [
    (_delete_desktop, "the latest renders (t1): the evidence set is incomplete"),
    (_replace_desktop, "the latest renders (t1) changed after their review: "),
    (_rewrite_summary, "the latest renders (t1) changed after their review: "),
])
def test_renders_changed_after_approval_are_not_verification(tmp_path, monkeypatch, mutate, why):
    # Root review of 95b81c2: the files, not the saved flags. Mutated after
    # the approval and before the orchestrator names the verification task.
    from quadratus.design_evidence import evidence_dir
    profile, _ = _profile(tmp_path)

    def verify_after_mutation(call, replay):
        mutate(evidence_dir(replay.project, "t1"))
        return VERIFY
    plan = [REQS + BUILD, None]
    replay = _run(tmp_path, monkeypatch, [], {"t1": _build, "t2": _no_edit}, profile=profile,
                  roles={"orchestrator": lambda c, r: (plan.pop(0) or verify_after_mutation(c, r)) if plan
                         else "DONE"})
    assert _handoffs(replay) == []
    [dispatched] = H.result_json(replay)["requirements"]["verification_dispatched"]
    assert dispatched["why"].startswith(why), dispatched
    assert len(_leads(replay, "t2")) == 1
    assert not replay.result.completed, "the completion guard still holds"


@browser
def test_with_the_requirements_audit_off_the_review_task_runs(tmp_path, monkeypatch):
    # Root review of 95b81c2: no final audit would judge what a handoff covers.
    monkeypatch.setenv("QUADRATUS_REQUIREMENTS_LEDGER", "0")
    profile, _ = _profile(tmp_path)
    replay = _run(tmp_path, monkeypatch, [BUILD, VERIFY], {"t1": _build, "t2": _no_edit}, profile=profile)
    _not_handed_off(replay, "the requirements audit is disabled")
    assert len(_leads(replay, "t2")) == 1 and replay.of("auditor") == []
    second = replay.of("orchestrator")[1].prompt
    assert "## The harness's latest check and renders" in second
    assert "is not dispatched while" not in second and "requirement audit judges" not in second


@browser
def test_unapproved_renders_are_not_verification(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)
    replay = _run(tmp_path, monkeypatch, [REQS + BUILD, VERIFY], {"t1": _build, "t2": _no_edit}, profile=profile,
                  roles={"design-review": lambda c, r: "BLOCKING: the toolbar overlaps the search field"})
    assert not replay.result.completed
    _not_handed_off(replay)


def test_design_verification_without_any_renders_is_not_handed_off(tmp_path, monkeypatch):
    # No capture profile and no design work before it: nothing shows the page.
    replay = _run(tmp_path, monkeypatch, [REQS + DOCS, VERIFY], {"t1": _docs, "t2": _no_edit})
    _not_handed_off(replay, "no renders of this source were captured and reviewed")


# -- the check predicate on real gate receipts ------------------------------------------

def _gate(tmp_path, *commands):
    from quadratus.integration import GateCommand, GateSuite
    from quadratus.project import Project
    (tmp_path / "app.py").write_text("x = 1\n")
    result = GateSuite([GateCommand(id=f"c{i}", **kw) for i, kw in enumerate(commands, 1)], cwd=tmp_path).run()
    current = Project(tmp_path).fingerprint()
    return dict(passed=result.passed, command=result.command, report=result.report, source=current,
                receipts=[dataclasses.asdict(r) for r in result.receipts]), current


@pytest.mark.parametrize("commands, why", [
    ([dict(argv=(sys.executable, "-c", "raise SystemExit(1)"))], "the latest project check failed"),
    ([dict(argv=(sys.executable, "-c", "pass"))], "counts are unknown"),
    ([dict(argv=(sys.executable, "-c", "pass"), skip_reason="no browser")], "check failed"),
    ([dict(argv=(sys.executable, "/no/such/runner.py"))], "check failed"),
])
def test_failed_skipped_missing_and_unexecuted_commands_are_not_verified(tmp_path, commands, why):
    from quadratus.session import _verified_by_check
    check, current = _gate(tmp_path, *commands)
    assert why in _verified_by_check(check, current)


def test_a_check_on_an_earlier_source_or_no_check_is_not_verified(tmp_path):
    from quadratus.session import _verified_by_check
    check, current = _gate(tmp_path, dict(argv=(sys.executable, "-c", "pass")))
    check["receipts"][0]["report"] = dict(state="parsed", counts=dict(passed=3, failed=0, errors=0, skipped=0))
    assert _verified_by_check(check, current) == "", "the control: a clean executed pass is verified"
    assert "earlier source" in _verified_by_check(check, "0" * 64)
    assert _verified_by_check(None, current) == "no project check has run"
    assert "earlier source" in _verified_by_check(dict(check, source=None), current)
    json.dumps(check)


def test_an_operator_listed_review_task_runs_as_written(tmp_path, monkeypatch):
    # An explicit list has no orchestrator to send a task back to.
    from tests.lifecycle.test_explicit_tasks import README_TASK, _readme_lead, _script
    review = README_TASK.replace('"max_lines": 20', '"max_lines": 1, "edits": "none"').replace(
        "Document add in README.md.", "Review the README against app.py.")
    replay = H.run(tmp_path, monkeypatch, _script(lead=lambda c, r: _readme_lead(c, r) if c.task == "t1" else
                                                  _no_edit(c, r)),
                   files=_files(), tasks=["REQUIREMENTS:\nR1: README documents add\nR2: README matches app.py\n"
                                          + README_TASK + "\nCOVERS: R1", review + "\nCOVERS: R2"], max_tasks=4)
    assert _handoffs(replay) == [] and len(_leads(replay, "t2")) == 1, replay.result.error
    assert H.result_json(replay)["requirements"]["verification_dispatched"] == []


def _files():
    from tests.lifecycle.test_direct_tier import _files as files
    return files()
