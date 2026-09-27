"""Evidence tampering and identity mismatch as integrity (map J9b).

Whole-controller replays. Evidence positively observed not to hold (a
symlinked screenshot, a fixture whose bytes changed after capture, a harness
capture recorded against a source the harness did not capture) stops the run
as ``integrity``: no design-fix, no recapture, no product repair, and the
evidence stays as found. Missing or stale evidence, including a lead's own
capture of a tree it then edited, keeps the one bounded recapture (J8, J9a).
Synthetic writes stand in for the tampering: controller invariants, not
observed vendor behaviour.
"""

import hashlib
import json
from pathlib import Path

from quadratus.design_evidence import evidence_dir, fixture_dir
from tests.lifecycle import harness as H
from tests.lifecycle.test_harness_capture import (
    CAPTURE,
    FITTING_PAGE,
    REPAIR_SCOPE,
    REQS,
    _decl,
    _fix,
    _profile,
    _run,
    browser,
)
from tests.lifecycle.test_lifecycle_matrix import _design_run

FIXTURE = ".quadratus/capture-fixtures/t1/rows.csv"


def _edit_and_capture(call):
    H.write(call, {"templates/index.html": "<button id=import>Import</button>\n"})
    H.evidence(Path(call.cwd), "t1", age=0)


def _with_fixture_step(root: Path):
    """Record one file step against the fixture's current digest, as a capture does."""
    folder = fixture_dir(root, "t1")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "rows.csv").write_text("name\nalpha\n")
    digest = hashlib.sha256((folder / "rows.csv").read_bytes()).hexdigest()
    path = evidence_dir(root, "t1") / "summary.json"
    summary = json.loads(path.read_text())
    summary["steps"] = [dict(action="file", selector="#upload", label=FIXTURE, sha256=digest)]
    for view in summary["views"].values():
        view["steps"] = [dict(n=1, action="file", selector="#upload", file=FIXTURE, ok=True)]
    path.write_text(json.dumps(summary))


def _stopped_as_integrity(replay, needle):
    result = replay.result
    assert not result.completed
    assert result.error.startswith("EvidenceTampered: task t1"), result.error
    assert needle in result.error, (needle, result.error)
    assert "preserved as found" in result.error
    stop = replay.workflow["run"]["facts"][-1]
    assert stop["kind"] == "integrity" and stop["legacy"] == "EvidenceTampered", stop
    assert not replay.of("design-fix"), "never recaptured or repaired"
    record = json.loads(replay.artifact_texts("design-evidence")[0])
    assert record["verified"] is False and needle in record["problem"] and record["tampered"]
    assert (replay.project / "templates/index.html").read_text().startswith("<button"), "work preserved"


# -- positive: observed tampering stops -------------------------------------

def test_a_symlinked_screenshot_stops_as_integrity(tmp_path, monkeypatch):
    def lead(call, replay):
        _edit_and_capture(call)
        mobile = evidence_dir(Path(call.cwd), "t1") / "mobile" / "page.png"
        mobile.unlink()
        mobile.symlink_to(mobile.parent.parent / "desktop" / "page.png")
        return 'Added the button and captured it.\nCHANGED: ["templates/index.html"]'
    replay = _design_run(tmp_path, monkeypatch, lead=lead,
                         revision=lambda call, replay: "Nothing to change.\nCHANGED: []")
    _stopped_as_integrity(replay, "symlink, not a capture")
    assert (evidence_dir(replay.project, "t1") / "mobile" / "page.png").is_symlink(), "left as found"


def test_a_fixture_changed_after_capture_stops_as_integrity(tmp_path, monkeypatch):
    def lead(call, replay):
        _edit_and_capture(call)
        _with_fixture_step(Path(call.cwd))
        (Path(call.cwd) / FIXTURE).write_text("name\nswapped\n")
        return 'Added the button and captured it.\nCHANGED: ["templates/index.html"]'
    replay = _design_run(tmp_path, monkeypatch, lead=lead,
                         revision=lambda call, replay: "Nothing to change.\nCHANGED: []")
    _stopped_as_integrity(replay, "changed after the capture")
    assert (replay.project / FIXTURE).read_text() == "name\nswapped\n", "left as found"


@browser
def test_a_harness_capture_recorded_on_another_source_stops_as_integrity(tmp_path, monkeypatch):
    from quadratus.session import Session
    real = Session._harness_capture

    def moved(self, spec):
        failure = real(self, spec)
        path = evidence_dir(Path(self.project), spec.task_id) / "summary.json"
        summary = json.loads(path.read_text())
        summary["source_fingerprint"] = "0" * 64      # the record moved under the harness
        path.write_text(json.dumps(summary))
        return failure
    monkeypatch.setattr(Session, "_harness_capture", moved)
    profile, _ = _profile(tmp_path)
    build = _decl("KIND: frontend standard", dict(REPAIR_SCOPE, capture=CAPTURE), "Add the toolbar.")
    replay = _run(tmp_path, monkeypatch, [REQS + build], {"t1": _fix}, profile=profile)
    result = replay.result
    assert not result.completed
    assert result.error.startswith("EvidenceTampered: task t1") and "different source tree" in result.error
    stop = replay.workflow["run"]["facts"][-1]
    assert stop["kind"] == "integrity" and stop["legacy"] == "EvidenceTampered", stop
    assert not replay.of("design-fix"), "never recaptured or repaired"
    record = json.loads(replay.artifact_texts("design-evidence")[0])
    assert record["harness_capture"] is True and record["tampered"]
    assert (replay.project / "templates/index.html").read_text() == FITTING_PAGE, "work preserved"


# -- negative: missing or stale evidence keeps the bounded recapture --------

def test_a_self_capture_of_a_tree_edited_afterwards_is_stale_not_tampered(tmp_path, monkeypatch):
    def lead(call, replay):
        _edit_and_capture(call)
        H.write(call, {"static/style.css": "#import { padding: 8px; }\n"})
        return 'Added the button, captured, then styled it.\nCHANGED: ["templates/index.html", "static/style.css"]'
    replay = _design_run(tmp_path, monkeypatch, lead=lead,
                         revision=lambda call, replay: "Nothing to change.\nCHANGED: []")
    assert len(replay.of("design-fix")) == 1, "one bounded recapture, as J8/J9a"
    assert "EvidenceTampered" not in replay.result.error
    record = json.loads(replay.artifact_texts("design-evidence")[0])
    assert "different source tree" in record["first_problem"] and "tampered" not in record


def test_a_fixture_gone_after_capture_is_invalid_proof_not_tampering(tmp_path, monkeypatch):
    def lead(call, replay):
        _edit_and_capture(call)
        _with_fixture_step(Path(call.cwd))
        (Path(call.cwd) / FIXTURE).unlink()
        return 'Added the button and captured it.\nCHANGED: ["templates/index.html"]'
    replay = _design_run(tmp_path, monkeypatch, lead=lead,
                         revision=lambda call, replay: "Nothing to change.\nCHANGED: []")
    assert len(replay.of("design-fix")) == 1
    assert "EvidenceTampered" not in replay.result.error
    assert "cannot be reproduced" in json.loads(replay.artifact_texts("design-evidence")[0])["first_problem"]


def test_missing_renders_keep_the_bounded_recapture(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"templates/index.html": "<button id=import>Import</button>\n"})
        return 'Added the button.\nCHANGED: ["templates/index.html"]'
    replay = _design_run(tmp_path, monkeypatch, lead=lead,
                         revision=lambda call, replay: "Nothing to change.\nCHANGED: []")
    assert len(replay.of("design-fix")) == 1
    assert "EvidenceTampered" not in replay.result.error
