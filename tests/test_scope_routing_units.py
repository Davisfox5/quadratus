"""Cycle04 (PR73 6713487831): the 'OUTSIDE SCOPE' declaration is an
untrusted routing claim, validated against what the harness holds. Real
Session; the lifecycle controls are in tests/lifecycle/test_scope_routing.py."""
import os
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.scope import TaskScope
from quadratus.session import _MAX_SCOPE_LINES, _MAX_SCOPE_PATHS, Session, SessionConfig, TaskSpec

CASE = "tests/test_app.py::test_add"


def _session(tmp_path, *, permitted=("README.md", "tests/"), forbidden=(), review_only=False, scope=True,
             failures=(CASE,), task="t1"):
    project = tmp_path / "project"
    for name in ("app.py", "lib/util.py", "README.md", "tests/test_app.py", "secrets/key.txt"):
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text("x\n")
    (project / "node_modules" / "pkg").mkdir(parents=True)
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=project))
    spec = TaskSpec(task, "Document add.", scope=(TaskScope(permitted_paths=list(permitted),
                                                            forbidden_paths=list(forbidden),
                                                            review_only=review_only) if scope else None))
    session._active_spec = spec
    report = dict(state="parsed", failures=[dict(nodeid=n, when="call", exc_type="builtins.AssertionError",
                                                 assertion=True, body=True) for n in failures],
                  counts=dict(failed=len(failures), errors=0))
    session._outcome = SimpleNamespace(task_id=task, checks=[dict(passed=False, attempt=1, source="s1",
                                                                  receipts=[dict(id="check", status="failed",
                                                                                 report=report)])])
    session.review_findings = [
        dict(fid="RFT1-1", task="t1", author="openai:sol", kind="defect", closed=False, claim="add returns 0"),
        dict(fid="RFT1-2", task="t1", author="openai:sol", kind="defect", closed=True, claim="done"),
        dict(fid="RFT9-1", task="t9", author="openai:sol", kind="defect", closed=False, claim="elsewhere"),
    ]
    return session, project


def _declare(session, text, role="revision"):
    session._record_scope_declarations("openai:sol", role, text)
    return session.scope_declarations


def test_an_open_finding_and_a_current_failed_case_route_with_every_path_deduplicated(tmp_path):
    session, _ = _session(tmp_path)
    decls = _declare(session, "Did the docs.\nOUTSIDE SCOPE: RFT1-1 - app.py, lib/util.py\n"
                              "OUTSIDE SCOPE: RFT1-1 - app.py\n"
                              f"OUTSIDE SCOPE: {CASE} - `app.py`\nCHANGED: []")
    by_id = {d["id"]: d for d in decls}
    assert by_id["RFT1-1"]["status"] == "routed" and by_id["RFT1-1"]["paths"] == ["app.py", "lib/util.py"]
    assert by_id[CASE]["status"] == "routed" and by_id[CASE]["kind"] == "case"
    record = session.review_findings[0]
    assert record["route"] == "scope" and record["scope_paths"] == ["app.py", "lib/util.py"]
    assert session._routed_ids(session._active_spec) == {"RFT1-1"}, "out of the editor's lines"
    _declare(session, "OUTSIDE SCOPE: RFT1-1 - app.py")
    assert session.scope_declarations[-1]["repeat"] is True, "a repeat changes nothing"
    assert record["scope_paths"] == ["app.py", "lib/util.py"]


@pytest.mark.parametrize("ident, why", [
    ("RFT9-1", "belongs to t9, not t1"),
    ("RFT1-2", "is not an open blocking finding"),
    ("tests/test_app.py::test_gone", "neither an open finding of t1 nor a failed case of its latest check"),
    ("RFT1-99", "neither an open finding"),
])
def test_an_identity_this_task_does_not_hold_open_is_rejected(tmp_path, ident, why):
    session, _ = _session(tmp_path)
    (decl,) = _declare(session, f"OUTSIDE SCOPE: {ident} - app.py")
    assert decl["status"] == "rejected" and why in decl["why"]
    assert all(not r.get("route") for r in session.review_findings)


@pytest.mark.parametrize("path, why, forbidden", [
    ("/etc/passwd", "not a project-relative file path", False),
    ("~/x.py", "not a project-relative file path", False),
    ("../other/app.py", "escapes the project", False),
    ("lib/*.py", "not a project-relative file path", False),
    ("node_modules/pkg/index.js", "inside a dependency tree", True),
    (".quadratus/runs/x", "a harness or version-control path", True),
    ("README.md", "inside this task's grant", False),
    ("tests/test_app.py", "inside this task's grant", False),
    ("secrets/key.txt", "forbidden by the operator", True),
])
def test_a_path_that_is_not_a_project_file_outside_the_grant_is_refused_with_its_reason(tmp_path, path, why,
                                                                                         forbidden):
    session, _ = _session(tmp_path, forbidden=("secrets/**",))
    (decl,) = _declare(session, f"OUTSIDE SCOPE: RFT1-1 - {path}")
    assert decl["status"] == ("forbidden" if forbidden else "rejected")
    listed = decl["forbidden"] if forbidden else decl["rejected"]
    assert listed and why in listed[0]["why"], decl
    assert not session.review_findings[0].get("route"), "no route on a refused path"


def test_a_link_out_of_the_project_is_refused(tmp_path):
    session, project = _session(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, project / "linked")
    (decl,) = _declare(session, "OUTSIDE SCOPE: RFT1-1 - linked/app.py")
    assert decl["status"] == "forbidden" and "passes through a link" in decl["forbidden"][0]["why"]


def test_an_operator_forbidden_path_is_not_a_route_and_an_ordinary_one_beside_it_is(tmp_path):
    session, _ = _session(tmp_path, forbidden=("secrets/**",))
    (decl,) = _declare(session, "OUTSIDE SCOPE: RFT1-1 - secrets/key.txt, app.py")
    assert decl["status"] == "routed" and decl["paths"] == ["app.py"] and decl["forbidden"][0]["path"] == "secrets/key.txt"


@pytest.mark.parametrize("kind", ["unrestricted", "review-only", "exact"])
def test_scope_shapes_keep_their_meaning(tmp_path, kind):
    if kind == "unrestricted":
        session, _ = _session(tmp_path, permitted=())
        (decl,) = _declare(session, "OUTSIDE SCOPE: RFT1-1 - app.py")
        assert decl["status"] == "rejected" and "inside this task's grant" in decl["rejected"][0]["why"]
    elif kind == "review-only":
        session, _ = _session(tmp_path, review_only=True)
        (decl,) = _declare(session, "OUTSIDE SCOPE: RFT1-1 - app.py")
        assert decl["status"] == "rejected" and "review-only" in decl["why"]
    else:
        session, _ = _session(tmp_path, permitted=("README.md",))
        session._active_spec.scope.exact = True
        (decl,) = _declare(session, "OUTSIDE SCOPE: RFT1-1 - README.md.bak, app.py")
        assert decl["status"] == "routed" and decl["paths"] == ["README.md.bak", "app.py"]


def test_declarations_are_bounded_per_reply_and_per_id(tmp_path):
    session, _ = _session(tmp_path)
    paths = ", ".join(f"lib/p{i}.py" for i in range(_MAX_SCOPE_PATHS + 2))
    _declare(session, f"OUTSIDE SCOPE: RFT1-1 - {paths}\n"
             + "\n".join(f"OUTSIDE SCOPE: RFT1-{i} - app.py" for i in range(50, 50 + _MAX_SCOPE_LINES + 3)))
    first = session.scope_declarations[0]
    assert len(first["paths"]) == _MAX_SCOPE_PATHS
    assert [r["why"] for r in first["rejected"]] == ["past the per-id path bound"] * 2
    assert len(session.scope_declarations) == _MAX_SCOPE_LINES, "lines past the bound are not read"
    assert first["lines_ignored"] == 4, "12 lines, 8 read"


def _result(failures, *, state="parsed", status="failed", errors=0, truncated=False, when="call"):
    report = dict(state=state, failures=[dict(nodeid=n, when=when, exc_type="E", assertion=False) for n in failures],
                  counts=dict(failed=len(failures), errors=errors), truncated=truncated, collect_errors=0)
    return SimpleNamespace(receipts=[SimpleNamespace(id="check", status=status, report=report)], report=None)


def test_a_gate_fix_is_skipped_only_when_every_failed_case_is_declared(tmp_path):
    session, _ = _session(tmp_path, failures=(CASE, "tests/test_app.py::test_other"))
    spec = session._active_spec
    _declare(session, f"OUTSIDE SCOPE: {CASE} - app.py")
    assert session._scope_covers(spec, _result([CASE, "tests/test_app.py::test_other"])) is None, "mixed"
    assert session._scope_covers(spec, _result([CASE]))["paths"] == ["app.py"]
    assert session._scope_covers(spec, _result([CASE], state="unparsable")) is None, "unparsed"
    assert session._scope_covers(spec, _result([CASE], status="timeout")) is None, "no test result"
    assert session._scope_covers(spec, _result([CASE], errors=1)) is None, "setup error"
    assert session._scope_covers(spec, _result([CASE], when="setup")) is None, "outside a body"
    assert session._scope_covers(spec, _result([CASE], truncated=True)) is None, "truncated record"
    # An open review finding naming the file does not cover a failed case.
    other, _ = _session(tmp_path / "f")
    _declare(other, "OUTSIDE SCOPE: RFT1-1 - app.py")
    assert other._scope_covers(other._active_spec, _result([CASE])) is None


def test_the_taught_line_is_the_parsed_line_with_the_parsers_bounds():
    from quadratus.session import _OUTSIDE_SCOPE, _OUTSIDE_SCOPE_RULE
    assert f"at most {_MAX_SCOPE_PATHS} paths per id and {_MAX_SCOPE_LINES} lines" in _OUTSIDE_SCOPE_RULE
    example = "OUTSIDE SCOPE: RFT1-1 - app.py, lib/util.py"
    assert "'OUTSIDE SCOPE: <finding id or failed case id> - <project-relative path>[, <path>...]'" in \
        _OUTSIDE_SCOPE_RULE
    assert _OUTSIDE_SCOPE.findall(example) == [("RFT1-1", "app.py, lib/util.py")]
    assert "grants nothing, edits nothing and proves nothing" in _OUTSIDE_SCOPE_RULE


# -- root review of b9d136e: robust paths and the current failure episode ---------------------------

def test_malformed_and_directory_paths_are_rejected_and_a_new_file_name_is_kept(tmp_path):
    session, _ = _session(tmp_path)
    for raw in (".", "./", "lib", "lib/", "a/./b.py"):
        _declare(session, f"OUTSIDE SCOPE: RFT1-1 - {raw}")
        assert session.scope_declarations[-1]["status"] == "rejected", raw
    _declare(session, "OUTSIDE SCOPE: RFT1-1 - lib/new_module.py")
    assert session.scope_declarations[-1]["status"] == "routed", "a file that does not exist yet is a valid name"


def test_a_crash_while_reading_a_declaration_is_recorded_and_the_reply_still_returns(tmp_path, monkeypatch):
    session, _ = _session(tmp_path)
    session._writes = lambda: False
    monkeypatch.setattr(session, "_scope_path_problem", lambda spec, raw: 1 / 0)
    monkeypatch.setattr(session, "_invoke_model", lambda key, prompt, **k: "Done.\nOUTSIDE SCOPE: RFT1-1 - app.py")
    for name in ("_record_proves", "_record_not_run", "_record_runs", "_stop_repeated_environment_failure"):
        monkeypatch.setattr(session, name, lambda *a, **k: None)
    session._last_changed_report = None
    assert session._edit("openai:sol", "p", role="revision").startswith("Done.")
    assert session.scope_declarations[-1]["status"] == "rejected"
    assert "could not be read: ZeroDivisionError" in session.scope_declarations[-1]["why"]


def _attempt(passed, failures=(), kind="builtins.AssertionError", attempt=2, seq=None):
    report = dict(state="parsed", failures=[dict(nodeid=n, when="call", exc_type=kind, assertion=True, body=True)
                                            for n in failures], counts=dict(failed=len(failures), errors=0))
    run = dict(passed=passed, attempt=attempt, source="s", receipts=[dict(id="check", status="passed" if passed else
                                                                      "failed", report=report)])
    if seq is not None:
        run["seq"] = seq
    return run


def test_a_case_claim_lasts_through_its_episode_and_ends_when_the_case_passes_or_fails_otherwise(tmp_path):
    session, _ = _session(tmp_path)
    spec = session._active_spec
    _declare(session, f"OUTSIDE SCOPE: {CASE} - app.py")
    # Benign in-scope work: the same case still fails the same way on a new source.
    session._outcome.checks.append(dict(_attempt(False, [CASE]), source="after-readme-edit"))
    assert session._scope_covers(spec, _result([CASE])) is not None, "kept across in-scope edits"
    # The same case now fails with another exception type: a new failure.
    session._outcome.checks.append(_attempt(False, [CASE], kind="builtins.NameError", attempt=3))
    assert session._scope_covers(spec, _result([CASE])) is None
    fresh, _ = _session(tmp_path / "fresh")
    _declare(fresh, f"OUTSIDE SCOPE: {CASE} - app.py")
    fresh._outcome.checks.append(_attempt(True))
    fresh._outcome.checks.append(_attempt(False, [CASE], attempt=3))
    assert fresh._scope_covers(fresh._active_spec, _result([CASE])) is None, "passed in between: superseded"
    # Declared again on the new episode, it routes again.
    _declare(fresh, f"OUTSIDE SCOPE: {CASE} - app.py")
    assert fresh._scope_covers(fresh._active_spec, _result([CASE])) is not None


def test_run_order_follows_run_numbers_when_records_carry_them(tmp_path):
    session, _ = _session(tmp_path)
    session._outcome.checks[0]["seq"] = 1
    session.review_checks.append(dict(_attempt(True), task="t1", stage="review", seq=2))
    cases, anchor = session._task_failed_cases("t1")
    assert cases == set() and anchor["kind"] == "observed", "the later observed pass is current"
    assert session._next_run_seq() == 3


def test_a_reviewer_restating_a_scope_routed_finding_lapses_the_route(tmp_path):
    session, _ = _session(tmp_path)
    _declare(session, "OUTSIDE SCOPE: RFT1-1 - app.py")
    record = session.review_findings[0]
    assert record["route"] == "scope"
    session._read_restatement(session._active_spec, record,
                              "SCENARIO P1 - the state should be 'nothing stored' | proposal: state: nothing stored")
    assert record["restated"]
    assert record["route"] != "scope" and record["scope_lapsed"]
    assert record["route_history"][-1]["lapsed"] == "restated by its reviewer"
    assert "RFT1-1" not in session._routed_ids(session._active_spec) or record.get("route")
