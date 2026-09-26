"""Interactive design evidence: steps reach the changed state before the screenshot.

Codex review of #25 (Run 13): the import dialog and its result rows appear
only after a click and a file choice, so a render at load could not show them
and a grader timeout could not say which step failed. These tests pin the
step rules offline, then drive a real headless browser on tiny generic pages:
a working dialog, an inert launcher, a missing selector, a blocked navigation.
"""

import json
import os

import pytest

from quadratus import design_evidence as de
from quadratus.design_evidence import capture, check, evidence_dir, parse_steps, validate_steps

PAGE = """<!doctype html><title>preview</title>
<button id=open>Import</button>
<a id=away href="https://example.com/">elsewhere</a>
<dialog id=d><input type=file id=f><table id=t></table></dialog>
<script>
  document.getElementById('open').onclick = () => document.getElementById('d').showModal();
  document.getElementById('f').onchange = (e) => {
    document.getElementById('t').innerHTML =
      '<tr data-status="ok"><td>' + e.target.files[0].name + '</td></tr>';
  };
</script>
"""
INERT = PAGE.replace("document.getElementById('open').onclick", "window.unused")


def _project(tmp_path):
    root = tmp_path / "project"
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "rows.csv").write_text("name\nalpha\n")
    (root / "index.html").write_text(PAGE)
    (root / "inert.html").write_text(INERT)
    return root


# -- the rules, offline --------------------------------------------------------------

def test_steps_are_parsed_in_order_beside_the_positional_arguments():
    positional, steps = parse_steps(["page.html", "t1", ".", "--click", "#open", "--wait", "dialog[open]",
                                     "--file", "#f=fixtures/rows.csv"])
    assert positional == ["page.html", "t1", "."]
    assert [s["action"] for s in steps] == ["click", "wait", "file"]
    assert steps[2] == {"action": "file", "selector": "#f", "path": "fixtures/rows.csv"}
    with pytest.raises(ValueError):
        parse_steps(["page.html", "t1", "--file", "#f"])
    with pytest.raises(ValueError):
        parse_steps(["page.html", "t1", "--click"])


@pytest.mark.parametrize("path", ["../outside.csv", "/etc/passwd", ".git/config", ".quadratus/runs/x.json",
                                  ".env", "config/.env.local", "keys/server.pem", "api_token.txt",
                                  "fixtures/missing.csv", "fixtures", "link.csv", "via/rows.csv"])
def test_a_file_step_takes_only_a_project_owned_non_secret_regular_file(tmp_path, path):
    root = _project(tmp_path)
    (root / "link.csv").symlink_to(root / "fixtures" / "rows.csv")
    (root / "via").symlink_to(root / "fixtures", target_is_directory=True)
    for name in (".env", "api_token.txt"):
        (root / name).write_text("secret")
    with pytest.raises(ValueError):
        validate_steps([dict(action="file", selector="#f", path=path)], str(root / "index.html"), root)


def test_steps_are_bounded_and_limited_to_three_actions(tmp_path):
    root = _project(tmp_path)
    page = str(root / "index.html")
    with pytest.raises(ValueError, match="at most"):
        validate_steps([dict(action="wait", selector="#t")] * (de.MAX_STEPS + 1), page, root)
    for bad in (dict(action="eval", selector="1"), dict(action="type", selector="#f"),
                dict(action="click", selector=""), dict(action="click", selector="x" * 301)):
        with pytest.raises(ValueError):
            validate_steps([bad], page, root)


@pytest.mark.parametrize("target", ["https://example.com/", "http://10.0.0.5:5000/", "ftp://localhost/"])
def test_an_interactive_capture_must_target_a_local_preview(tmp_path, target):
    root = _project(tmp_path)
    with pytest.raises(ValueError, match="local preview"):
        validate_steps([dict(action="click", selector="#open")], target, root)
    checked, _ = validate_steps([], target, root)       # a plain render keeps its old reach
    assert checked == []


def test_a_page_file_outside_the_project_is_refused_for_interaction(tmp_path):
    root = _project(tmp_path)
    other = tmp_path / "elsewhere.html"
    other.write_text(PAGE)
    with pytest.raises(ValueError, match="inside the project"):
        validate_steps([dict(action="click", selector="#open")], str(other), root)
    _, allowed = validate_steps([dict(action="click", selector="#open")], "http://127.0.0.1:5000/", root)
    assert allowed("http://127.0.0.1:5000/import") and not allowed("http://127.0.0.1:5001/")
    assert not allowed("https://example.com/")


def _summary(root, steps, views):
    folder = evidence_dir(root, "t1")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.json").write_text(json.dumps(dict(target="http://127.0.0.1:5000/", steps=steps,
                                                         views=views)))


def test_the_check_names_a_failed_step_and_refuses_partial_runs(tmp_path):
    from tests.lifecycle.harness import evidence
    evidence(tmp_path, "t1", age=0)
    base = json.loads((evidence_dir(tmp_path, "t1") / "summary.json").read_text())["views"]
    requested = [dict(action="click", selector="#open"), dict(action="wait", selector="dialog[open]")]
    ok = [dict(n=1, action="click", selector="#open", ok=True), dict(n=2, action="wait", selector="dialog[open]", ok=True)]
    failed = [ok[0], dict(n=2, action="wait", selector="dialog[open]", ok=False, error="Timeout 5000ms exceeded")]
    _summary(tmp_path, requested, {k: dict(v, steps=ok) for k, v in base.items()})
    passed, problem, shots = check(tmp_path, "t1", 0)
    assert passed, problem
    assert "steps: click #open; wait dialog[open]" in shots
    _summary(tmp_path, requested, {k: dict(v, steps=failed) for k, v in base.items()})
    passed, problem, _ = check(tmp_path, "t1", 0)
    assert not passed and "step 2 (wait dialog[open]) failed: Timeout" in problem
    _summary(tmp_path, requested, {k: dict(v, steps=ok[:1]) for k, v in base.items()})
    assert "ran 1 of 2 interaction steps" in check(tmp_path, "t1", 0)[1]


def test_a_refused_capture_is_recorded_so_an_older_render_cannot_stand_in(tmp_path):
    root = _project(tmp_path)
    with pytest.raises(ValueError):
        capture(str(root / "index.html"), "t1", root, [dict(action="file", selector="#f", path=".env")])
    passed, problem, _ = check(root, "t1", 0)
    assert not passed and "interaction steps were refused" in problem


# -- a real headless browser -------------------------------------------------------------

@pytest.fixture
def browser(monkeypatch):
    pytest.importorskip("playwright")
    if not os.environ.get("QUADRATUS_CHROMIUM") and os.path.exists("/opt/pw-browsers/chromium"):
        monkeypatch.setenv("QUADRATUS_CHROMIUM", "/opt/pw-browsers/chromium")
    monkeypatch.setattr(de, "STEP_TIMEOUT_MS", 1500)


def _capture(root, page, steps):
    try:
        return capture(str(root / page), "t1", root, steps)
    except Exception as exc:  # noqa: BLE001
        if "Executable doesn't exist" in str(exc) or "BrowserType.launch" in str(exc):
            pytest.skip(f"headless browser unavailable: {str(exc)[:120]}")
        raise


FLOW = [dict(action="click", selector="#open"), dict(action="wait", selector="dialog[open]"),
        dict(action="file", selector="#f", path="fixtures/rows.csv"),
        dict(action="wait", selector="#t tr[data-status]")]


def test_a_working_dialog_reaches_its_result_state(tmp_path, browser):
    root = _project(tmp_path)
    out = _capture(root, "index.html", FLOW)
    assert all(s["ok"] for view in out.values() for s in view["steps"])
    assert out["desktop"]["steps"][2]["file"] == "fixtures/rows.csv"
    passed, problem, shots = check(root, "t1", 0)
    assert passed, problem
    assert any(s.startswith("steps: click #open; wait dialog[open]; file #f = fixtures/rows.csv") for s in shots)


def test_an_inert_launcher_fails_at_the_named_step(tmp_path, browser):
    root = _project(tmp_path)
    out = _capture(root, "inert.html", FLOW)
    steps = out["desktop"]["steps"]
    assert [s["ok"] for s in steps] == [True, False], "the click happens; the dialog never opens"
    passed, problem, _ = check(root, "t1", 0)
    assert not passed and "step 2 (wait dialog[open]) failed" in problem


def test_a_missing_selector_fails_at_the_named_step(tmp_path, browser):
    root = _project(tmp_path)
    _capture(root, "index.html", [dict(action="click", selector="#nope")])
    passed, problem, _ = check(root, "t1", 0)
    assert not passed and "step 1 (click #nope) failed" in problem


def test_navigation_off_the_preview_is_blocked_and_fails_the_step(tmp_path, browser):
    root = _project(tmp_path)
    out = _capture(root, "index.html", [dict(action="click", selector="#away")])
    step = out["desktop"]["steps"][0]
    assert not step["ok"] and "navigation outside the preview was blocked" in step["error"]
    assert not check(root, "t1", 0)[0]


def test_a_capture_without_steps_is_unchanged(tmp_path, browser):
    root = _project(tmp_path)
    out = _capture(root, "index.html", None)
    assert "steps" not in out["desktop"]
    summary = json.loads((evidence_dir(root, "t1") / "summary.json").read_text())
    assert "steps" not in summary
    assert check(root, "t1", 0)[0]


def test_the_command_line_exits_nonzero_when_a_step_fails(tmp_path, browser, capsys):
    root = _project(tmp_path)
    try:
        code = de.main([str(root / "inert.html"), "t1", str(root), "--click", "#open", "--wait", "dialog[open]"])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"headless browser unavailable: {str(exc)[:120]}")
    assert code == 1
    assert de.main([str(root / "index.html"), "t1", str(root), "--file", "#f=.env"]) == 2


# -- Codex review of c222d62: integrity of the evidence itself --------------------------

def test_a_failed_recapture_invalidates_earlier_fresh_evidence(tmp_path, monkeypatch):
    """Finding 1: an exception mid-capture left the old summary and PNGs passing."""
    from quadratus import browser
    from tests.lifecycle.harness import evidence
    root = _project(tmp_path)
    evidence(root, "t1", age=3600)
    assert check(root, "t1", 0)[0], "fresh earlier evidence passes before the recapture"

    def broken(*a, **k):
        raise RuntimeError("browser crashed while loading")
    monkeypatch.setattr(browser, "render_page", broken)
    with pytest.raises(RuntimeError):
        capture(str(root / "index.html"), "t1", root, None)
    passed, problem, _ = check(root, "t1", 0)
    assert not passed and "the last capture failed: RuntimeError: browser crashed" in problem
    assert not (evidence_dir(root, "t1") / "desktop" / "page.png").exists()


def test_a_capture_that_never_finished_does_not_count(tmp_path):
    root = _project(tmp_path)
    folder = evidence_dir(root, "t1")
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text(json.dumps(dict(target="x", views={}, capture_in_progress=True)))
    assert check(root, "t1", 0) == (False, "the last capture did not finish", [])


@pytest.mark.parametrize("records,expected", [
    ([dict(n=1, action="click", selector="#open", ok=True),
      dict(n=2, action="wait", selector="#unrelated", ok=True)], "does not match the requested step"),
    ([dict(n=1, action="click", selector="#open", ok=True),
      dict(n=3, action="wait", selector="#results", ok=True)], "does not match the requested step"),
    ([dict(n=1, action="click", selector="#open", ok="yes"),
      dict(n=2, action="wait", selector="#results", ok=True)], "failed"),
    (["not a record", dict(n=2, action="wait", selector="#results", ok=True)], "record is malformed"),
    ([dict(n=1, action="click", selector="#open", ok=True)], "ran 1 of 2"),
    ("not a list", "no interaction step record"),
])
def test_step_records_must_match_the_request_exactly(tmp_path, records, expected):
    """Finding 3: a wrong selector with the right count, a non-bool ok, or a
    non-dict record passed or raised."""
    from tests.lifecycle.harness import evidence
    evidence(tmp_path, "t1", age=0)
    folder = evidence_dir(tmp_path, "t1")
    summary = json.loads((folder / "summary.json").read_text())
    summary["steps"] = [dict(action="click", selector="#open"), dict(action="wait", selector="#results")]
    for view in summary["views"].values():
        view["steps"] = records
    (folder / "summary.json").write_text(json.dumps(summary))
    passed, problem, _ = check(tmp_path, "t1", 0)
    assert not passed and expected in problem


def test_malformed_summaries_never_raise(tmp_path):
    from tests.lifecycle.harness import evidence
    evidence(tmp_path, "t1", age=0)
    folder = evidence_dir(tmp_path, "t1")
    summary = json.loads((folder / "summary.json").read_text())
    for requested in ([1, 2], [dict(action="eval", selector="x")], "steps"):
        summary["steps"] = requested
        (folder / "summary.json").write_text(json.dumps(summary))
        passed, problem, _ = check(tmp_path, "t1", 0)
        assert not passed and problem
    summary.pop("steps")
    summary["views"]["desktop"]["steps"] = [dict(n=1, action="click", selector="#x", ok=True)]
    (folder / "summary.json").write_text(json.dumps(summary))
    assert "never requested" in check(tmp_path, "t1", 0)[1]
