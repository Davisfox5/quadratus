"""Independent controls for attribution, approval history and explicit retirement."""

import hashlib
import json
import re

import pytest

from quadratus import checkpoint, project_run
from quadratus.artifacts import ArtifactStore
from quadratus.config import Settings
from quadratus.project import Project
from quadratus.session import Session, SessionConfig
from tools import truth_check

GOAL = "Replace obsolete behavior and its obsolete test."
PY_PATH = "tests/test_default.py"
OLD = b"def test_default():\n    assert 5 == 5\n"
NEW = b"def test_new_default():\n    assert 10 == 10\n"
JS_PATH = "tests/default.test.js"
JS_OLD = b"test('old default', () => { assert.equal(defaultValue(), 5); });\n"
JS_NEW = b"test('new default', () => { assert.equal(defaultValue(), 10); });\n"


def _approval(tmp_path, *, path=PY_PATH, original=OLD, delivered=NEW,
              old_case="test_default", new_case="test_new_default", retire=False):
    store = ArtifactStore(tmp_path / "run" / "artifacts")
    proposal = dict(goal=GOAL, path=path, original_sha256=hashlib.sha256(original).hexdigest(),
                    current_sha256=hashlib.sha256(delivered).hexdigest(), reason="Requested behavior changed",
                    cases=[dict(case=old_case, action="retire" if retire else "replace",
                                replacements=[] if retire else [dict(path=path, case=new_case)])])
    digest = hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
    answer = f"APPROVE TEST CHANGE {digest}"
    return dict(proposal, id=digest, status="approved", approval="operator reply", answer=answer,
                original_artifact=store.put(original.decode(), kind="original", author="harness").id,
                delivered_artifact=store.put(delivered.decode(), kind="delivered", author="harness").id,
                checked_source="final-source")


def _record(*approvals, executed=()):
    return dict(goal=GOAL, source_fingerprint="final-source", test_change_decisions=list(approvals),
                requirements=dict(operator_rulings=[
                    f"Test change {a['id']} for {a['path']}: {a['answer']}" for a in approvals]),
                checks=[dict(passed=True, source="final-source", receipts=[
                    dict(status="passed", required=True, cases=dict(executed=dict.fromkeys(executed, 1)))])])


def _project(tmp_path, path, data):
    root = tmp_path / "project"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return root


def test_historical_human_approval_remains_provenance_after_a_new_exact_approval(tmp_path):
    previous = _approval(tmp_path, delivered=NEW + b"# first approved version\n")
    current = _approval(tmp_path)
    previous["checked_source"] = "historical-source"
    record = _record(previous, current, executed=[f"{PY_PATH}::test_new_default"])
    root = _project(tmp_path, PY_PATH, NEW)

    valid, problems = truth_check._test_change_approvals(record, tmp_path / "run", root)
    assert not problems and valid == {previous["id"]: PY_PATH, current["id"]: PY_PATH}


def test_historical_approval_cannot_replace_a_missing_current_approval(tmp_path):
    previous = _approval(tmp_path, delivered=NEW + b"# earlier bytes\n")
    current = _approval(tmp_path)
    record = _record(previous, current, executed=[f"{PY_PATH}::test_new_default"])
    record["test_change_decisions"][-1]["answer"] = "unapproved current bytes"
    root = _project(tmp_path, PY_PATH, NEW)

    valid, problems = truth_check._test_change_approvals(record, tmp_path / "run", root)
    assert not valid and problems


@pytest.mark.parametrize("approved", [False, True])
def test_only_a_current_valid_migration_supersedes_legacy_repair_review(tmp_path, approved):
    approval = _approval(tmp_path)
    record = _record(approval, executed=[f"{PY_PATH}::test_new_default"])
    record["test_repairs"] = [dict(path=PY_PATH, assertions_verified=False)]
    if not approved:
        record["test_change_decisions"][0]["answer"] = "not an approval"
    (tmp_path / "run/result.json").write_text(json.dumps(record))
    (tmp_path / "run/changes.diff").write_text("")
    root = _project(tmp_path, PY_PATH, NEW)

    verdict = truth_check.judge(tmp_path / "run", root)
    has_unreviewed_repair = any("authorized test repair" in item for item in verdict["unverified"])
    assert has_unreviewed_repair is not approved


def test_retiring_the_last_case_in_a_retained_file_is_an_explicit_operator_decision(tmp_path):
    root = _project(tmp_path, PY_PATH, OLD)
    (root / "tests/test_other.py").write_text("def test_other():\n    assert True\n")
    questions = []

    def ask(question):
        questions.append(question)
        return re.search(r"Reply exactly (APPROVE TEST CHANGE [0-9a-f]{64})", question).group(1)

    session = Session(GOAL, ArtifactStore(tmp_path / "run" / "artifacts"),
                      lambda *a, **k: pytest.fail("no model invocation is needed"),
                      config=SessionConfig(project=root, allow_writes=True, ask_operator=ask))
    session._snapshot_original_tests()
    (root / PY_PATH).write_bytes(b"# The obsolete case was deliberately retired.\n")
    current = Project(root).contents()
    receipt = dict(status="passed", required=True, cases=dict(executed={"tests/test_other.py::test_other": 1}))
    approved = session._test_migrations([PY_PATH], current, [receipt], request=True)

    assert len(questions) == 1 and "Full diff:" in questions[0]
    assert approved[PY_PATH]["cases"] == [dict(case="test_default", action="retire", replacements=[])]
    decision = approved[PY_PATH]
    record = _record(decision)
    record["source_fingerprint"] = decision["checked_source"]
    record["checks"][-1]["source"] = decision["checked_source"]
    valid, problems = truth_check._test_change_approvals(record, tmp_path / "run", root)
    assert valid == {decision["id"]: PY_PATH} and not problems


def test_concrete_migration_supersedes_legacy_repair_for_the_same_file_at_done(tmp_path, monkeypatch):
    root = _project(tmp_path, PY_PATH, OLD)
    (root / "tests/helper.py").write_text("VALUE = 5\n")

    def ask(question):
        return re.search(r"Reply exactly (APPROVE TEST CHANGE [0-9a-f]{64})", question).group(1)

    session = Session(GOAL, ArtifactStore(tmp_path / "run" / "artifacts"),
                      lambda *a, **k: pytest.fail("no model invocation is needed"),
                      config=SessionConfig(project=root, allow_writes=True, repair_tests=(PY_PATH,),
                                           ask_operator=ask))
    session._snapshot_original_tests()
    (root / PY_PATH).write_bytes(NEW)
    (root / "tests/helper.py").write_text("VALUE = 10\n")
    current = Project(root).contents()
    receipt = dict(status="passed", required=True, cases=dict(executed={f"{PY_PATH}::test_new_default": 1}))
    assert session._test_migrations([PY_PATH], current, [receipt], request=True)
    session.checks = [dict(passed=True, source=session._source_fingerprint(), receipts=[receipt])]
    remaining = []

    def run_originals(current, changed, keep=()):
        remaining.append((changed, keep))
        return ""

    monkeypatch.setattr(session, "_run_original_tests_at_done", run_originals)
    assert session._original_tests_at_done() == ""
    assert remaining == [(["tests/helper.py"], (PY_PATH,))]


@pytest.mark.parametrize("side", ["original", "delivered"])
def test_duplicate_python_case_names_cannot_be_collapsed_into_valid_evidence(tmp_path, side):
    original, delivered = (OLD + OLD, NEW) if side == "original" else (OLD, NEW + NEW)
    approval = _approval(tmp_path, original=original, delivered=delivered)
    root = _project(tmp_path, PY_PATH, delivered)
    valid, problems = truth_check._test_change_approvals(
        _record(approval, executed=[f"{PY_PATH}::test_new_default"]), tmp_path / "run", root)
    assert not valid and problems


def _js_approval(tmp_path, **kwargs):
    return _approval(tmp_path, path=JS_PATH, original=JS_OLD, delivered=JS_NEW,
                     old_case="old default", new_case="new default", **kwargs)


def test_js_literal_case_migration_is_attributed_to_its_unique_current_file(tmp_path):
    approval = _js_approval(tmp_path)
    root = _project(tmp_path, JS_PATH, JS_NEW)
    valid, problems = truth_check._test_change_approvals(
        _record(approval, executed=["new default"]), tmp_path / "run", root)
    assert valid == {approval["id"]: JS_PATH} and not problems


def test_js_bare_name_in_another_file_cannot_establish_this_replacement(tmp_path):
    approval = _js_approval(tmp_path)
    root = _project(tmp_path, JS_PATH, JS_NEW)
    (root / "tests/other.test.js").write_bytes(JS_NEW)
    valid, problems = truth_check._test_change_approvals(
        _record(approval, executed=["new default"]), tmp_path / "run", root)
    assert not valid and problems


def test_unrelated_duplicate_names_do_not_make_a_unique_js_replacement_ambiguous(tmp_path):
    approval = _js_approval(tmp_path)
    root = _project(tmp_path, JS_PATH, JS_NEW)
    (root / "tests/test_other.py").write_bytes(OLD + OLD)
    valid, problems = truth_check._test_change_approvals(
        _record(approval, executed=["new default"]), tmp_path / "run", root)
    assert valid and not problems


@pytest.mark.parametrize("executed,expected", [(["new default"], False),
                                             ([f"{JS_PATH}::new default"], True),
                                             (["tests/other.test.js::new default"], False)])
def test_without_project_js_requires_file_qualified_execution(tmp_path, executed, expected):
    approval = _js_approval(tmp_path)
    valid, problems = truth_check._test_change_approvals(
        _record(approval, executed=executed), tmp_path / "run", None)
    assert bool(valid) is expected and bool(problems) is not expected


@pytest.mark.parametrize("delivered", [JS_NEW + JS_NEW,
                                       b"test(`new default`, () => {});\n",
                                       b"test(caseName, () => {});\n"])
def test_js_duplicate_or_dynamic_case_declarations_are_not_verified(tmp_path, delivered):
    approval = _approval(tmp_path, path=JS_PATH, original=JS_OLD, delivered=delivered,
                         old_case="old default", new_case="new default")
    root = _project(tmp_path, JS_PATH, delivered)
    valid, problems = truth_check._test_change_approvals(
        _record(approval, executed=["new default"]), tmp_path / "run", root)
    assert not valid and problems


def test_js_comments_and_strings_do_not_create_ambiguous_case_declarations(tmp_path):
    data = JS_NEW + b"// test('new default', () => {});\nconst description = \"test('new default')\";\n"
    approval = _approval(tmp_path, path=JS_PATH, original=JS_OLD, delivered=data,
                         old_case="old default", new_case="new default")
    root = _project(tmp_path, JS_PATH, data)
    valid, problems = truth_check._test_change_approvals(
        _record(approval, executed=["new default"]), tmp_path / "run", root)
    assert valid and not problems


@pytest.mark.parametrize("authority", [None, [], ()])
def test_default_resume_workflow_digest_keeps_the_legacy_no_authority_identity(tmp_path, monkeypatch, authority):
    root = _project(tmp_path, PY_PATH, OLD)
    (root / ".quadratus/runs/previous").mkdir(parents=True)
    original_identity = checkpoint.workflow_identity
    observed = {}

    class ValidatedBeforeCalls(Exception):
        pass

    def identity(**config):
        assert "test_changes" not in config
        observed["legacy"] = original_identity(**config)
        return observed["legacy"]

    def validate(parent, body, identities):
        assert identities["workflow"] == observed["legacy"]
        raise ValidatedBeforeCalls

    monkeypatch.setattr(checkpoint, "workflow_identity", identity)
    monkeypatch.setattr(checkpoint, "load", lambda *a: {})
    monkeypatch.setattr(checkpoint, "validate", validate)
    with pytest.raises(ValidatedBeforeCalls):
        project_run.run_project(GOAL, root, Settings(), test_changes=authority, resume_from="previous")
    assert observed["legacy"]
