"""A reviewer's blocking design judgement gets one bounded fix, then the
same checks and the same reviewer again.

Overnight r01 (20261011T052348Z-1a2969ca): the cross-vendor design reviewer
rightly blocked a shortcut hint that squeezed the library search field to
46 px; the run ended FindingsOpen after 7 of its 20 calls with no repair,
although a failed check gets a gate-fix and a broken render a design-fix.
Whole-controller replays with a real harness capture (as in
test_harness_capture); scripted verdicts are routing proof, not judgement.
"""

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_audit_findings import REPAIR_SCOPE, REQS, _design_files
from tests.lifecycle.test_harness_capture import (
    CAPTURE,
    FITTING_PAGE,
    ICON,
    _decl,
    _no_edit,
    _observed,
    _profile,
    _run,
    browser,
)

pytestmark = pytest.mark.requirements_ledger

#: This task's own scenarios: the hint beside the search field, as its
#: fixture page shows it; the shared REPAIR_SCOPE proof names an Import
#: button this page never has.
HINT_PROOFS = [
    dict(id="P1", requirements=["R1"], operation="the import page with the toolbar hint is rendered at 390 px and desktop",
         state="the hint squeezed the search field", expect="the search field keeps its room inside the viewport",
         location="templates/index.html", evidence="capture"),
    dict(id="P2", requirements=["R2"], operation="the toolbar text is read in both renders", state="the hint is shown",
         expect="the toolbar text is visible beside the hint", location="templates/index.html", evidence="capture"),
]
BUILD = _decl("KIND: frontend standard", dict(REPAIR_SCOPE, proofs=HINT_PROOFS, capture=CAPTURE), "Add the toolbar hint.")
WIDER = f"<html>{ICON}<body><div class=toolbar style='max-width:100%'>tools, wider search</div></body></html>\n"
#: The scripted reviewer's observations of the fixed page (WIDER), one per
#: covered requirement; a BLOCKING verdict judges no row.
HINT_OBSERVED = {
    "R1": "SUFFICIENT both renders show the search field with room beside the hint inside the viewport",
    "R2": "SUFFICIENT both renders show the toolbar text 'tools, wider search' visible",
}


def _verdicts(*replies):
    queue = list(replies)
    return _observed(HINT_OBSERVED, base=lambda call, replay: queue.pop(0) if queue else "APPROVED")


def _run_review(tmp_path, monkeypatch, review, fixes):
    def fix(call, replay):
        fixes.append(call.prompt)
        H.write(call, {"templates/index.html": WIDER})
        return 'Gave the search field room.\nCHANGED: ["templates/index.html"]'
    profile, _ = _profile(tmp_path)
    return _run(tmp_path, monkeypatch, [REQS + BUILD], {"t1": _no_edit}, profile=profile,
                files={**_design_files(), "templates/index.html": FITTING_PAGE},
                roles={"design-review": review, "design-fix": fix})


@browser
def test_a_blocking_design_judgement_gets_one_fix_and_a_second_review(tmp_path, monkeypatch):
    fixes = []
    replay = _run_review(tmp_path, monkeypatch,
                         _verdicts("BLOCKING: the hint squeezes the search field to 46 px", "APPROVED"), fixes)
    assert len(fixes) == 1 and "BLOCKING: the hint squeezes the search field to 46 px" in fixes[0]
    assert "Do not start servers or run capture commands" in fixes[0]
    assert len(replay.of("design-review")) == 2
    assert replay.result.completed, replay.result.error


@browser
def test_a_second_blocking_verdict_stays_open_with_no_further_fix(tmp_path, monkeypatch):
    fixes = []
    replay = _run_review(tmp_path, monkeypatch,
                         _verdicts("BLOCKING: the hint squeezes the search field", "BLOCKING: still squeezed"), fixes)
    assert len(fixes) == 1 and len(replay.of("design-review")) == 2, "one fix, one re-review, never a loop"
    assert not replay.result.completed
    record = H.result_json(replay)["design_checks"][-1]
    assert record["final_review"]["blocking"] == ["BLOCKING: still squeezed"]
    assert record["review_fix"]["after"] == "still blocking"


@browser
@pytest.mark.parametrize("verdict", ["BLOCKING: the reviewer altered its copy", "Looks fine I think"])
def test_harness_and_verdict_failures_get_no_design_fix(tmp_path, monkeypatch, verdict):
    # A delivery failure and a reply that is no verdict are not design
    # judgements a source fix could answer.
    fixes = []
    replay = _run_review(tmp_path, monkeypatch, lambda c, r: verdict, fixes)
    assert fixes == [] and len(replay.of("design-review")) == 1
    assert not replay.result.completed
