"""The harness capture runs under the profile bound at dispatch (map P3.4;
O-NEXT-13 at a229f10, Codex ruling 5865903915).

``Session._harness_capture`` handed the live ``capture_profile`` to
``preview.capture_task``, so the lead could be told one page while another
was previewed and captured, and same-page drifts (preview argv, env,
readiness, timeouts) were invisible to ``capture_page``. The session now
holds an independent copy of the whole profile per task, the contract
records only its digest (no env values), and the capture fails closed: no
bound profile for this task is a refusal, and a live profile that was
removed or differs is recorded by field name and stops the capture before
the preview starts, through the existing capture-failure route. No-drift
captures use the bound copy.

Only ``preview.capture_task`` is stubbed, imported inside
``_harness_capture`` at call time; the Session, its contract, eligibility,
the dependency check and the source fingerprint are real. One
whole-controller journey drifts during the draft. The drift is a synthetic
write to ``session.config``, a controller invariant.
"""

import dataclasses

import pytest

from quadratus import preview
from quadratus.artifacts import ArtifactStore
from quadratus.outcome import TaskOutcome
from quadratus.preview import ENVIRONMENT, CaptureFailure, CaptureProfile
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec

PROFILE = CaptureProfile(preview=("python3", "-m", "http.server", "5000"), origin="http://127.0.0.1:5000")
CAPTURE = {"path": "/index.html", "steps": []}
PAGE = "http://127.0.0.1:5000/index.html"
CHANGED = "the harness did not capture because the capture profile changed after dispatch"


class _Recorder:
    """Stands in for ``preview.capture_task``: records the profile it was
    handed and returns ``result``."""

    def __init__(self, result=""):
        self.profiles, self.result = [], result

    def __call__(self, profile, root, task_id, capture, receipt=None):
        self.profiles.append(profile)
        return self.result


def _dispatched(tmp_path, monkeypatch, result="", profile=PROFILE):
    """A real Session with t1 dispatched under PROFILE as a harness task,
    its contract bound, and ``capture_task`` stubbed."""
    root = tmp_path / "project"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "index.html").write_text("<html><body>page</body></html>\n")
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE",
                      config=SessionConfig(project=root, allow_writes=True, capture_profile=profile))
    spec = TaskSpec("t1", "Add an Import button to the page.", kind="frontend",
                    scope=TaskScope(permitted_paths=["templates/index.html"], capture=CAPTURE))
    outcome = TaskOutcome("t1", "implementation")
    session._outcome = outcome
    session._contract = session._build_contract(spec, outcome)
    session._bind_contract("claude:opus")
    assert session._contract.required.design_evidence == "harness"
    assert session._contract.capture_page == PAGE
    assert session._contract.capture_profile == session._profile_digest(profile)
    assert session._capture_ineligible() == "", "no gate, so the capture is eligible"
    recorder = _Recorder(result)
    monkeypatch.setattr(preview, "capture_task", recorder)
    return session, spec, recorder


def _drift(session, **changes):
    session.config = dataclasses.replace(session.config, **changes)


def _profile_mismatches(session):
    return [m for m in session._outcome.mismatches if m.startswith("capture_profile:")]


# -- drift: the capture must use the dispatch profile and say what moved ---------------

def test_an_origin_drift_stops_the_capture_before_the_preview(tmp_path, monkeypatch):
    """The O-NEXT-08 repro: the lead is told 5000; the live profile says 6000."""
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    _drift(session, capture_profile=dataclasses.replace(PROFILE, origin="http://127.0.0.1:6000"))
    assert session._harness_capture(spec) == f"{CHANGED} (origin)"
    assert recorder.profiles == [], "no preview under either profile"
    assert _profile_mismatches(session) == ["capture_profile: live differs from contract in origin"]


def test_a_removed_profile_stops_the_capture_and_never_falls_back(tmp_path, monkeypatch):
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    _drift(session, capture_profile=None)
    assert session._harness_capture(spec) == f"{CHANGED} (profile (removed))"
    assert recorder.profiles == []
    assert _profile_mismatches(session) == ["capture_profile: live differs from contract in profile (removed)"]
    assert session._contract.required.design_evidence == "harness", "never falls back to self"


@pytest.mark.parametrize("changes, fields", [
    (dict(preview=("python3", "-m", "http.server", "5000", "--directory", "other")), "preview"),
    (dict(env=(("APP_MODE", "demo"),)), "env"),
    (dict(ready_path="/health"), "ready_path"),
    (dict(total_timeout=30.0, capture_timeout=20.0), "capture_timeout, total_timeout"),
], ids=["preview-argv", "env", "ready-path", "timeouts"])
def test_a_same_page_drift_stops_the_capture(tmp_path, monkeypatch, changes, fields):
    """These drifts keep ``capture_page`` identical; a URL-only pin misses them."""
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    _drift(session, capture_profile=dataclasses.replace(PROFILE, **changes))
    assert session._live_capture_page(spec) == session._contract.capture_page == PAGE
    assert session._harness_capture(spec) == f"{CHANGED} ({fields})"
    assert recorder.profiles == []
    assert _profile_mismatches(session) == [f"capture_profile: live differs from contract in {fields}"]


def test_a_design_fix_recapture_after_drift_stops(tmp_path, monkeypatch):
    """_check_design captures, spends a design-fix call, then recaptures."""
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    assert session._harness_capture(spec) == ""
    _drift(session, capture_profile=dataclasses.replace(PROFILE, origin="http://127.0.0.1:6000"))
    assert session._harness_capture(spec) == f"{CHANGED} (origin)"
    assert recorder.profiles == [PROFILE], "the first capture only"


def test_a_capture_without_this_tasks_contract_fails_closed(tmp_path, monkeypatch):
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    session._contract = None
    assert session._harness_capture(spec) == ("the harness did not capture because the task's contract "
                                              "holds no capture profile")
    assert recorder.profiles == []


def test_a_missing_or_foreign_snapshot_fails_closed(tmp_path, monkeypatch):
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    held = session._capture_snapshot
    session._capture_snapshot = None
    assert session._harness_capture(spec).endswith("the task's contract holds no capture profile")
    session._capture_snapshot = ("t2", held[1])
    assert session._harness_capture(spec).endswith("the task's contract holds no capture profile")
    session._capture_snapshot = ("t1", dataclasses.replace(held[1], origin="http://127.0.0.1:6000"))
    assert session._harness_capture(spec).endswith("the task's contract holds no capture profile"), \
        "a snapshot that is not the one the contract records"
    assert recorder.profiles == []


def test_a_profile_mutated_after_dispatch_does_not_move_the_snapshot(tmp_path, monkeypatch):
    """The snapshot is a copy: the live object is frozen, but its env pairs
    are replaced, not shared."""
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    assert session._capture_snapshot[1] == PROFILE and session._capture_snapshot[1] is not PROFILE


def test_env_values_never_reach_the_record(tmp_path, monkeypatch):
    import json
    secret = PROFILE.__class__(**{**dataclasses.asdict(PROFILE), "preview": PROFILE.preview,
                                  "env": (("APP_TOKEN_HINT", "s3cr3t-value"),)})
    session, spec, recorder = _dispatched(tmp_path, monkeypatch, profile=secret)
    record = json.dumps(session._contract.to_dict())
    assert "s3cr3t-value" not in record and session._contract.capture_profile.startswith("sha256:")
    _drift(session, capture_profile=dataclasses.replace(secret, env=(("APP_TOKEN_HINT", "other-value"),)))
    session._harness_capture(spec)
    assert not any("s3cr3t" in m or "other-value" in m for m in session._outcome.mismatches)
    assert _profile_mismatches(session) == ["capture_profile: live differs from contract in env"]


# -- controls: no drift; any correction must keep these -------------------------------

def test_no_drift_captures_with_the_dispatch_profile_and_records_nothing(tmp_path, monkeypatch):
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    assert session._harness_capture(spec) == ""
    assert recorder.profiles == [PROFILE]
    assert session._outcome.mismatches == []


def test_no_drift_recapture_uses_the_same_profile(tmp_path, monkeypatch):
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    assert session._harness_capture(spec) == "" and session._harness_capture(spec) == ""
    assert recorder.profiles == [PROFILE, PROFILE]
    assert session._outcome.mismatches == []


def test_a_capture_failure_passes_through_with_its_origin(tmp_path, monkeypatch):
    failure = CaptureFailure("something is already listening on http://127.0.0.1:5000", ENVIRONMENT)
    session, spec, recorder = _dispatched(tmp_path, monkeypatch, result=failure)
    got = session._harness_capture(spec)
    assert got == failure and got.origin == ENVIRONMENT
    assert recorder.profiles == [PROFILE]


def test_an_ineligible_capture_stops_before_the_boundary(tmp_path, monkeypatch):
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    session._outcome.contract = None
    assert session._harness_capture(spec) == ("the harness did not capture because "
                                              "the task has no dispatch record")
    assert recorder.profiles == []


# -- the record ---------------------------------------------------------------------

def _record(capture_profile="absent"):
    from quadratus.outcome import missing_facts
    from tests.test_outcome import REQUIRED, _closed
    task = _closed()
    task.contract["required"] = dict(REQUIRED, design_evidence="harness")
    if capture_profile != "absent":
        task.contract["capture_profile"] = capture_profile
    return missing_facts(task)


def test_an_older_harness_record_without_the_profile_says_so():
    assert _record() == ["t1.contract.capture_profile (absent: recorded before this field existed)"]


def test_a_malformed_profile_digest_is_missing():
    for bad in (None, "sha256:", "sha256:" + "A" * 64, {"origin": "x"}):
        assert _record(bad) == ["t1.contract.capture_profile"], bad
    assert _record("sha256:" + "0" * 64) == []


# -- whole controller -----------------------------------------------------------------

@pytest.mark.requirements_ledger
def test_a_drift_during_the_draft_ends_the_task_unverified_with_no_preview(tmp_path, monkeypatch):
    import sys

    from tests.lifecycle.test_harness_capture import CAPTURE as DECLARED
    from tests.lifecycle.test_harness_capture import (
        REPAIR_SCOPE,
        REQS,
        _decl,
        _fix,
        _free_port,
        _run,
    )
    from tests.lifecycle.test_preview_provenance import _profile, _task
    started = []
    monkeypatch.setattr(preview, "capture_task", lambda *a, **k: started.append(a) or "")
    draft = Session._draft_with_channels

    def drifted(self, *args, **kw):
        profile = self.config.capture_profile
        self.config = dataclasses.replace(self.config, capture_profile=dataclasses.replace(
            profile, origin=f"http://127.0.0.1:{_free_port()}"))
        return draft(self, *args, **kw)
    monkeypatch.setattr(Session, "_draft_with_channels", drifted)
    build = _decl("KIND: frontend standard", dict(REPAIR_SCOPE, capture=DECLARED), "Add the toolbar.")
    replay = _run(tmp_path, monkeypatch, [REQS + build], {"t1": _fix}, record_complete=False,
                  profile=_profile(tmp_path, [sys.executable, "-m", "http.server"], _free_port()))
    assert started == [], "the preview never started"
    assert replay.result.error.startswith("DesignUnverified: task t1"), replay.result.error
    assert "the capture profile changed after dispatch (origin)" in replay.result.error
    assert "capture_profile: live differs from contract in origin" in _task(replay)["mismatches"]
    assert not replay.result.completed
