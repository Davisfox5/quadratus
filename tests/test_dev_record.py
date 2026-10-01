"""The development record tool (tools/dev_record.py): the acceptance cases
named in the coordination plan (#35, comment 5922888762). No vendor calls,
no network: commit existence on origin is injected."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import dev_record as R  # noqa: E402

CANDIDATE = "c" * 40


def _record():
    return dict(version=1, candidate=dict(pr=1, sha=CANDIDATE, gate="g", required_receipts=["ci", "acceptance", "review"]),
                tasks=[], receipts=[])


def _claim(rec, tid="T1", owns=("quadratus/a.py",), author="claude", base=CANDIDATE, **kw):
    return R.claim(rec, task_id=tid, purpose="p", base=base, owns=list(owns), author=author, **kw)


def test_competing_claims_cannot_both_succeed():
    rec = _record()
    _claim(rec, "T1", owns=["quadratus/a.py"])
    with pytest.raises(R.RecordError, match="overlaps active task T1"):
        _claim(rec, "T2", owns=["quadratus/a.py"], author="codex")
    with pytest.raises(R.RecordError, match="overlaps"):
        _claim(rec, "T3", owns=["quadratus"], author="codex")
    _claim(rec, "T4", owns=["docs/x.md"], author="codex")
    _claim(rec, "T5", owns=["quadratus/a.py"], author="codex", resolve=True)
    assert [t["id"] for t in rec["tasks"]] == ["T1", "T4", "T5"]


def test_a_stale_base_is_refused_unless_resolved():
    rec = _record()
    with pytest.raises(R.RecordError, match="stale base"):
        _claim(rec, base="d" * 40)
    _claim(rec, base="d" * 40, resolve=True)


def test_a_delivery_must_exist_on_origin():
    rec = _record()
    _claim(rec)
    with pytest.raises(R.RecordError, match="not on origin"):
        R.deliver(rec, task_id="T1", sha="e" * 40, exists=lambda s: False)
    R.deliver(rec, task_id="T1", sha="e" * 40, exists=lambda s: True)
    assert rec["tasks"][0]["state"] == "delivered"


def test_self_review_cannot_satisfy_independence():
    rec = _record()
    _claim(rec, author="claude")
    R.deliver(rec, task_id="T1", sha="e" * 40, exists=lambda s: True)
    with pytest.raises(R.RecordError, match="self-review"):
        R.review(rec, task_id="T1", reviewer="claude", sha="e" * 40, verdict="cleared", evidence="u")
    R.review(rec, task_id="T1", reviewer="codex", sha="e" * 40, verdict="cleared", evidence="u")
    assert rec["tasks"][0]["state"] == "reviewed"


def test_a_stale_review_cannot_qualify_changed_code_and_keeps_its_evidence():
    rec = _record()
    _claim(rec)
    R.deliver(rec, task_id="T1", sha="e" * 40, exists=lambda s: True)
    R.review(rec, task_id="T1", reviewer="codex", sha="e" * 40, verdict="cleared", evidence="first")
    R.deliver(rec, task_id="T1", sha="f" * 40, exists=lambda s: True)
    task = rec["tasks"][0]
    assert task["state"] == "delivered" and task["reviews"][0]["verdict"] == "stale"
    assert task["reviews"][0]["evidence"] == "first"
    with pytest.raises(R.RecordError, match="not reviewed"):
        R.integrate(rec, task_id="T1", sha="f" * 40, candidate="g" * 40)
    late = R.review(rec, task_id="T1", reviewer="codex", sha="e" * 40, verdict="cleared", evidence="old")
    assert late["verdict"] == "stale"


def test_unaffected_reviews_survive_unrelated_changes():
    rec = _record()
    _claim(rec, "T1", owns=["quadratus/a.py"])
    _claim(rec, "T2", owns=["docs/b.md"], author="codex")
    R.deliver(rec, task_id="T1", sha="e" * 40, exists=lambda s: True)
    R.review(rec, task_id="T1", reviewer="codex", sha="e" * 40, verdict="cleared", evidence="u")
    R.deliver(rec, task_id="T2", sha="f" * 40, exists=lambda s: True)
    assert rec["tasks"][0]["reviews"][0]["verdict"] == "cleared"


def test_a_blocker_needs_its_four_parts():
    rec = _record()
    _claim(rec)
    R.deliver(rec, task_id="T1", sha="e" * 40, exists=lambda s: True)
    with pytest.raises(R.RecordError, match="blocker names"):
        R.review(rec, task_id="T1", reviewer="codex", sha="e" * 40, verdict="blocked", evidence="u",
                 blocker=dict(requirement="r"))
    R.review(rec, task_id="T1", reviewer="codex", sha="e" * 40, verdict="blocked", evidence="u",
             blocker=dict(requirement="r", failure="f", evidence="e", classification="reachable"))
    assert rec["tasks"][0]["state"] == "blocked"


def test_failed_or_missing_required_receipts_block_integration_and_stay_distinct():
    rec = _record()
    assert R.readiness(rec)["receipts"] == dict(ci="missing", acceptance="missing", review="missing")
    R.receipt(rec, kind="ci", sha=CANDIDATE, state="failed")
    R.receipt(rec, kind="acceptance", sha=CANDIDATE, state="skipped")
    R.receipt(rec, kind="review", sha="o" * 40, state="passed")
    r = R.readiness(rec)
    assert r["receipts"] == dict(ci="failed", acceptance="skipped", review="missing") and not r["ready"]


def test_a_valid_independently_reviewed_candidate_qualifies():
    rec = _record()
    _claim(rec)
    R.deliver(rec, task_id="T1", sha="e" * 40, exists=lambda s: True)
    R.review(rec, task_id="T1", reviewer="codex", sha="e" * 40, verdict="cleared", evidence="u")
    R.integrate(rec, task_id="T1", sha="e" * 40, candidate="n" * 40)
    for kind in ("ci", "acceptance", "review"):
        R.receipt(rec, kind=kind, sha="n" * 40, state="passed", evidence="x")
    r = R.readiness(rec)
    assert r["ready"] and r["candidate"] == "n" * 40 and not r["open_tasks"] and not r["blockers"]
    assert "ready;" in R.render(rec)


def test_the_cli_round_trips_through_the_file(tmp_path, monkeypatch):
    path = tmp_path / "rec.json"
    path.write_text(json.dumps(_record()))
    monkeypatch.setattr(R, "commit_exists_on_origin", lambda sha, repo=None: True)
    assert R.main(["--record", str(path), "claim", "--id", "T1", "--purpose", "p", "--base", CANDIDATE,
                   "--owns", "quadratus/a.py", "--author", "claude"]) == 0
    assert R.main(["--record", str(path), "claim", "--id", "T2", "--purpose", "p", "--base", CANDIDATE,
                   "--owns", "quadratus/a.py", "--author", "codex"]) == 2
    assert R.main(["--record", str(path), "ready"]) == 0
    assert json.loads(path.read_text())["tasks"][0]["id"] == "T1"


def test_the_checked_in_record_loads_and_renders():
    rec = R.load()
    assert rec["candidate"]["sha"] and all(t["state"] in R.STATES for t in rec["tasks"])
    assert "Development record" in R.render(rec)
