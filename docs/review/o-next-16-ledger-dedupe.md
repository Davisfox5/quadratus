# O-NEXT-16: review of 8fcc738 (ledger at dispatch) and 72fb872 (mismatch dedupe)

Independent, bounded review on PR #25. No engine edits, no model calls. The
probes below were run from a scratch copy of each tree (`git archive`,
`PYTHONPATH` set to that copy) and are not part of the suite.

## TL;DR

- 8fcc738: all six new tests are red on a761edd at their behaviour or field
  line and green at 8fcc738. The debt route now needs dispatch AND live.
- One post-dispatch gap, pre-existing and unchanged by 8fcc738: a ledger
  disabled after a RESOLVES repair task is dispatched lets settlement close
  the finding, skips the DONE audit, and the run completes clean with no
  mismatch recorded. The Codex policy (5865627034) asks for a recorded
  mismatch that fails closed.
- 72fb872: exactly the three append sites plus the test file. Five of six new
  tests red on 8fcc738 at their count lines. Controller outcome and
  classification unchanged. The capture_page site dedupes but no committed
  test repeats it.

## Reproduction: disable after a RESOLVES repair is dispatched

Drop into `tests/lifecycle/` of 72fb872 (or a761edd, same result):

```python
import dataclasses
import pytest
from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_audit_findings import AUDIT, REQS, REPAIR, WIDE, _capture, _repair, _run

pytestmark = pytest.mark.requirements_ledger


def test_repair_disabled_mid_task(tmp_path, monkeypatch):
    check = Session._check_design

    def drifted(self, spec, *a, **kw):
        if spec.task_id == "t2":
            self.config = dataclasses.replace(self.config, requirements_ledger=False)
        return check(self, spec, *a, **kw)
    monkeypatch.setattr(Session, "_check_design", drifted)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair()}, record_complete=False)
    t2 = next(t for t in replay.workflow["tasks"] if t["task_id"] == "t2")
    status = H.result_json(replay)["requirements"]["status"]
    # Observed on 72fb872:
    assert replay.result.completed and replay.result.error == ""
    assert t2["contract"]["required"]["requirements_ledger"] is True
    assert t2["mismatches"] == []
    assert [(f["id"], f["status"]) for f in replay.findings] == [("F1", "resolved")]
    assert status == {"R1": "covered by t2", "R2": "covered by t2"}
```

Control with no drift: same findings, `completed` true, status
`met (audited)` for both. The `RECAPTURE` variant (review-only, clean
renders) gives the same result as the repair.

Why: `_audit_debt_applies` (session.py:4623) is the only post-dispatch
reader that calls `_required("requirements_ledger", ...)`, and it returns
before that call when the check is ok or the task is not review-only.
`_settle_resolution` (session.py:4521) and `_resolve_findings`
(session.py:4639) never consult the ledger, and `_requirements_satisfied`
(session.py:3957) reads live only, so the DONE audit is skipped.

## Other readers in session.py

| Reader | Line | When |
|---|---|---|
| orchestrator prompt (`_COVERS_REQUEST`/`_REQUIREMENTS_REQUEST`) | 2876 | run level, before naming a task |
| contract snapshot | 2127 | dispatch |
| parallel child config (`requirements_ledger=False`) | 3753 | run level, child record-only |
| `_covers_problem` | 3858 | pre-dispatch (called 3207, 3725) |
| `_absorb_requirements` | 3883 | orchestrator reply, run level |
| `_requirements_satisfied` | 3957 | DONE, run level |
| `_resolves_problem` | 4191 | pre-dispatch (called 3219) |
| `_audit_debt_applies` | 4634 | post-dispatch, dispatch AND live |

Enable direction: RESOLVES is refused pre-dispatch when the ledger is off
(4191), so `_current_resolves` stays empty and settlement has nothing to
discharge. `_mark_covered` (4495) keeps `NOT MET` while any finding naming the
requirement is open. No discharge path found.

Disable direction: nothing deletes requirements, findings or status.
`requirements.listed` and the open finding survive (test 1 of
test_ledger_applicability).

## 72fb872 controller replay (R2 drift), before and after

| Field | 8fcc738 | 72fb872 |
|---|---|---|
| `completed` | false | false |
| `error` | GoalUnconfirmedAtCap | GoalUnconfirmedAtCap |
| parity `primary` | cap | cap |
| parity `typed_completed` | false | false |
| parity `agree` | true | true |
| t1 mismatches | collab x2, design_review x1 | collab x1, design_review x1 |

capture_page probe (three `_prompt_under` calls after an origin drift):
three identical notes on 8fcc738, one on 72fb872.
