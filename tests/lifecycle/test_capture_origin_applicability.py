"""The harness capture follows the capture profile fixed at dispatch (O-NEXT-13).

7403511 fixed the lead prompt's page at dispatch (``TaskContract.capture_page``),
but ``Session._harness_capture`` still hands the live
``self.config.capture_profile`` to ``preview.capture_task``. The lead can be
told one page while the harness previews and captures another, and nothing
records the difference (O-NEXT-08, PR #25 comment 5865273760, finding 2).

The xfails pin what a correction must do: capture with the profile the task
was dispatched under, whatever the live config says by then, and record the
live reading beside it as a ``capture_profile:`` mismatch. Some drifts leave
``capture_page`` unchanged (preview argv, env, timeouts), which is the
evidence that the whole profile, not the URL, is what dispatch must fix.

Only the boundary is stubbed: ``quadratus.preview.capture_task``, imported
inside ``_harness_capture`` at call time, is replaced by a recorder that
keeps the profile it was handed and returns "" (or a given failure). No
preview is started and no browser runs. The Session, its contract, the
eligibility check, the dependency check and the source fingerprint are real.
Drift is a synthetic write to ``session.config`` between dispatch and
capture: a controller invariant, not observed behaviour.
See docs/review/capture-origin-seam.md.
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
SEAM = "O-NEXT-13: _harness_capture reads the live capture profile"


class _Recorder:
    """Stands in for ``preview.capture_task``: records the profile it was
    handed and returns ``result``."""

    def __init__(self, result=""):
        self.profiles, self.result = [], result

    def __call__(self, profile, root, task_id, capture):
        self.profiles.append(profile)
        return self.result


def _dispatched(tmp_path, monkeypatch, result=""):
    """A real Session with t1 dispatched under PROFILE as a harness task,
    its contract bound, and ``capture_task`` stubbed."""
    root = tmp_path / "project"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "index.html").write_text("<html><body>page</body></html>\n")
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE",
                      config=SessionConfig(project=root, allow_writes=True, capture_profile=PROFILE))
    spec = TaskSpec("t1", "Add an Import button to the page.", kind="frontend",
                    scope=TaskScope(permitted_paths=["templates/index.html"], capture=CAPTURE))
    outcome = TaskOutcome("t1", "implementation")
    session._outcome = outcome
    session._contract = session._build_contract(spec, outcome)
    session._bind_contract("claude:opus")
    assert session._contract.required.design_evidence == "harness"
    assert session._contract.capture_page == PAGE
    assert session._capture_ineligible() == "", "no gate, so the capture is eligible"
    recorder = _Recorder(result)
    monkeypatch.setattr(preview, "capture_task", recorder)
    return session, spec, recorder


def _drift(session, **changes):
    session.config = dataclasses.replace(session.config, **changes)


def _profile_mismatches(session):
    return [m for m in session._outcome.mismatches if m.startswith("capture_profile:")]


# -- drift: the capture must use the dispatch profile and say what moved ---------------

@pytest.mark.xfail(strict=True, raises=AssertionError, reason=SEAM)
def test_the_capture_uses_the_dispatch_origin_when_the_origin_drifts(tmp_path, monkeypatch):
    """The O-NEXT-08 repro: the lead is told 5000, the harness captures 6000."""
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    _drift(session, capture_profile=dataclasses.replace(PROFILE, origin="http://127.0.0.1:6000"))
    assert session._harness_capture(spec) == ""
    assert [p.origin for p in recorder.profiles] == ["http://127.0.0.1:5000"], \
        "captured the live origin, not the page the lead was told"
    assert _profile_mismatches(session), "the drift left no mismatch record"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=SEAM)
def test_the_capture_keeps_the_dispatch_profile_when_the_profile_is_removed(tmp_path, monkeypatch):
    """A "harness" contract with no live profile. Today capture_task is
    handed None (the real one then raises AttributeError in capture_argv);
    it must neither crash nor fall back to self-capture."""
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    _drift(session, capture_profile=None)
    failure = session._harness_capture(spec)
    assert recorder.profiles == [PROFILE], "capture_task was handed the live (absent) profile"
    assert failure == ""
    assert _profile_mismatches(session), "the removal left no mismatch record"
    assert session._contract.required.design_evidence == "harness", "never falls back to self"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=SEAM)
@pytest.mark.parametrize("changes", [
    dict(preview=("python3", "-m", "http.server", "5000", "--directory", "other")),
    dict(env=(("APP_MODE", "demo"),)),
    dict(ready_path="/health"),
    dict(total_timeout=30.0, capture_timeout=20.0),
], ids=["preview-argv", "env", "ready-path", "timeouts"])
def test_a_same_page_drift_is_still_the_dispatch_profile(tmp_path, monkeypatch, changes):
    """These drifts keep ``capture_page`` identical, so a URL-only pin
    cannot see them; what is served and captured still changes."""
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    moved = dataclasses.replace(PROFILE, **changes)
    _drift(session, capture_profile=moved)
    assert session._live_capture_page(spec) == session._contract.capture_page == PAGE, \
        "the drift is invisible to capture_page"
    assert session._harness_capture(spec) == ""
    assert recorder.profiles == [PROFILE], "captured under the live profile"
    assert _profile_mismatches(session), "the drift left no mismatch record"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=SEAM)
def test_the_design_fix_recapture_uses_the_same_profile_as_the_first(tmp_path, monkeypatch):
    """_check_design captures, spends a design-fix call, then recaptures
    (session.py:3352 and :3391): both go through _harness_capture."""
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    assert session._harness_capture(spec) == ""
    _drift(session, capture_profile=dataclasses.replace(PROFILE, origin="http://127.0.0.1:6000"))
    assert session._harness_capture(spec) == ""
    assert [p.origin for p in recorder.profiles] == ["http://127.0.0.1:5000"] * 2, \
        "the recapture measured a different page than the first capture"
    assert _profile_mismatches(session)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=SEAM)
def test_a_capture_without_this_tasks_contract_fails_closed(tmp_path, monkeypatch):
    """The outcome carries a bound contract (so eligibility passes), but the
    session holds no contract for this task. There is no dispatch profile
    to capture with, so the capture must refuse, not read the live one."""
    session, spec, recorder = _dispatched(tmp_path, monkeypatch)
    session._contract = None
    failure = session._harness_capture(spec)
    assert recorder.profiles == [], "captured with the live profile and no dispatch record"
    assert failure and failure.startswith("the harness did not capture because")


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
