"""A preview failure is an operator handoff only when its origin is proven
(map E1; Codex, 5862699144, option b).

``capture_task`` returned free text for every preview failure, and every one
became ``invalid_proof`` and ``DesignUnverified``. It now carries a
structured origin set where the failure is raised, never read from prose:

| failure (preview.py)                               | origin        | route                       |
|----------------------------------------------------|---------------|-----------------------------|
| port already listening, preview never launched     | environment   | PreviewUnavailable (operator) |
| a runner outside the project could not be launched | environment   | PreviewUnavailable (operator) |
| a project file (even ./python3) could not launch   | unattributed  | unchanged: DesignUnverified |
| the preview exited before it was ready             | unattributed  | unchanged: DesignUnverified |
| the preview was not ready within its timeout       | unattributed  | unchanged: DesignUnverified |
| the preview exited as it became ready              | unattributed  | unchanged: DesignUnverified |
| the budget was spent before the capture            | unattributed  | unchanged: DesignUnverified |
| the capture timed out / exited non-zero            | unattributed  | unchanged: DesignUnverified |

An application exit is not proof of an operator fault. Neither route makes
a repair call, and the failure text is byte-identical on both. Whole
controller replays with a real preview process and a real port.
"""

import json
import socket
import sys
from pathlib import Path

import pytest

from tests.lifecycle.test_audit_findings import _design_files
from tests.lifecycle.test_harness_capture import (
    CAPTURE,
    REPAIR_SCOPE,
    REQS,
    WIDE_PAGE,
    _decl,
    _fix,
    _free_port,
    _run,
)

pytestmark = pytest.mark.requirements_ledger


def _profile(tmp_path, preview, port):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(dict(preview=preview, origin=f"http://127.0.0.1:{port}",
                                    ready_timeout=2, capture_timeout=5)))
    return path


def _replay(tmp_path, monkeypatch, preview, port):
    build = _decl("KIND: frontend standard", dict(REPAIR_SCOPE, capture=CAPTURE), "Add the toolbar.")
    return _run(tmp_path, monkeypatch, [REQS + build], {"t1": _fix}, profile=_profile(tmp_path, preview, port))


def _task(replay):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")


def _no_repair(replay):
    assert not replay.of("design-fix") and not replay.of("gate-fix") and not replay.of("design-review")
    assert [c.task for c in replay.of("lead")] == ["t1"]
    assert not replay.result.completed


def _handed_off(replay, text):
    _no_repair(replay)
    assert replay.result.error == f"PreviewUnavailable: task t1: {text}. No repair call was made. Work preserved."
    stop = replay.workflow["run"]["facts"][-1]
    assert (stop["kind"], stop["legacy"]) == ("operator", "PreviewUnavailable")
    record = json.loads(replay.artifact_texts("design-evidence")[0])
    assert record["problem"] == text and record["verified"] is False, "the text is kept as it was"
    assert _task(replay)["edges"]["evidence"] is False


def _unverified(replay, needle):
    _no_repair(replay)
    assert replay.result.error.startswith("DesignUnverified: task t1"), replay.result.error
    assert needle in replay.result.error
    assert _task(replay)["primary"] == "invalid_proof"


# -- proven environment: operator handoff -------------------------------------------

def test_a_taken_port_is_an_operator_handoff(tmp_path, monkeypatch):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        replay = _replay(tmp_path, monkeypatch, [sys.executable, "-m", "http.server", str(port)], port)
    _handed_off(replay, f"something is already listening on http://127.0.0.1:{port}; the preview was not started")


def test_a_runner_that_cannot_launch_is_an_operator_handoff(tmp_path, monkeypatch):
    missing = str(tmp_path / "no-such-runtime" / "python3")
    replay = _replay(tmp_path, monkeypatch, [missing, "-m", "http.server"], _free_port())
    error = replay.result.error
    assert "the preview could not start: " in error and "No such file or directory" in error, error
    text = error[len("PreviewUnavailable: task t1: "):-len(". No repair call was made. Work preserved.")]
    _handed_off(replay, text)


# -- unattributed: the existing route, unchanged ------------------------------------

def test_a_preview_that_exits_before_it_is_ready_stays_unverified(tmp_path, monkeypatch):
    """Opus E1's scenario: an application exit, not proof of an operator fault."""
    replay = _replay(tmp_path, monkeypatch,
                     [sys.executable, "-c", "raise SystemExit('Xvfb: cannot open display')"], _free_port())
    _unverified(replay, "exited with 1 before it was ready")


def test_a_preview_that_is_never_ready_stays_unverified(tmp_path, monkeypatch):
    """It serves, but on another port than the profile's origin."""
    replay = _replay(tmp_path, monkeypatch, [sys.executable, "-m", "http.server", str(_free_port())], _free_port())
    _unverified(replay, "was not ready at")


def test_a_project_file_preview_that_cannot_launch_stays_unverified(tmp_path, monkeypatch):
    """A project file that cannot be executed may be the project's doing."""
    def lead(call, replay):
        (Path(call.cwd) / "serve.sh").chmod(0o644)
        return _fix(call, replay)
    build = _decl("KIND: frontend standard", dict(REPAIR_SCOPE, capture=CAPTURE), "Add the toolbar.")
    files = {**_design_files(), "templates/index.html": WIDE_PAGE, "serve.sh": "#!/bin/sh\nexit 0\n"}
    replay = _run(tmp_path, monkeypatch, [REQS + build], {"t1": lead}, files=files,
                  profile=_profile(tmp_path, ["serve.sh"], _free_port()))
    _unverified(replay, "the preview could not start: ")


# -- the returned value: same text, structured origin -------------------------------

@pytest.mark.parametrize("preview, origin, detail", [
    # A conventional runner outside the project that cannot launch: the environment.
    (["{outside}/python3", "-m", "http.server"], "environment",
     "the preview could not start: [Errno 2] No such file or directory: '{outside}/python3'"),
    # A project file named like a runner (Sol review, 5862984388): unattributed.
    (["./python3"], None, "the preview could not start: [Errno 13] Permission denied: './python3'"),
    (["{root}/python3"], None, "the preview could not start: [Errno 13] Permission denied: '{root}/python3'"),
    # A path that cannot be resolved is not proven outside (Sol review, 5863184501).
    (["{root}/loop/python3"], None,
     "the preview could not start: [Errno 40] Too many levels of symbolic links: '{root}/loop/python3'"),
    # A launched preview that exits: an application exit, unattributed.
    ([sys.executable, "-c", "raise SystemExit(3)"], None, "the preview exited with 3 before it was ready: "),
])
def test_capture_task_keeps_the_text_and_carries_the_origin(tmp_path, preview, origin, detail):
    from quadratus.preview import CaptureFailure, capture_task, profile_from_dict
    root = tmp_path / "project"
    root.mkdir()
    (root / "python3").write_text("#!/bin/sh\nexit 0\n")
    (root / "python3").chmod(0o644)
    (root / "loop").mkdir()
    (root / "loop" / "python3").symlink_to(root / "loop" / "python3")
    names = {"{outside}": str(tmp_path / "no-such-runtime"), "{root}": str(root)}

    def fill(text):
        for key, value in names.items():
            text = text.replace(key, value)
        return text
    profile = profile_from_dict(dict(preview=[fill(a) for a in preview], origin=f"http://127.0.0.1:{_free_port()}",
                                     ready_timeout=2, capture_timeout=5), root)
    failure = capture_task(profile, root, "t1", CAPTURE)
    assert isinstance(failure, CaptureFailure) and failure.origin == origin
    assert str(failure).startswith(fill(detail)) and (detail.endswith(": ") or str(failure) == fill(detail))
    assert type(str(failure)) is str and json.loads(json.dumps(failure)) == str(failure)


def test_a_path_that_cannot_be_resolved_is_not_proven_outside(tmp_path, monkeypatch):
    """Resolution failing for any reason leaves the failure unattributed."""
    from quadratus import preview
    root = tmp_path / "project"
    root.mkdir()

    def broken(self, strict=False):
        raise RuntimeError("Symlink loop from '/x'")
    monkeypatch.setattr(preview.Path, "resolve", broken)
    assert preview._outside_runner("/usr/bin/python3", root) is False
    assert preview._outside_runner("python3", root) is True, "a bare runner needs no resolution"
