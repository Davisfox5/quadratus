"""Proportionate boundary evidence and shared error-boundary regressions
(Davis, 2026-11-06, batch 6674444836 items D and E).

grok-worker-01 left R19 unmet on a 5000-digit ID that the delivered code in
fact refused cleanly (root's copy-only controls, 6674546520): the gap was an
untested risk, never an observed defect, and nothing named the classes to
test before the audit. Two other defects of that run shared one shape across
entry points: a storage error escaping the dispatcher, and a release failure
after a committed change turning a success into a failure.

The project below is a generic two-entry-point store, written for these
tests: no ticket application, signature or route. Its tests read the state
after each refusal. The approved pytest check runs them; that run is the
evidence. A deterministic engine proves here that the named cases executed
and passed on the delivered source, that a failing case reaches the lead as
check output and buys the ordinary repair, and that a MET without such a run
is held as an untested risk. Whether a case asserts the right behaviour
stays the reviewer's and the auditor's judgment.
"""
import json
import sys

import pytest

from quadratus import session as S
from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios
from tests.lifecycle.test_lifecycle_matrix import Script, judging

R1 = ("Removing an item by ID through the command line or the HTTP handler refuses an invalid ID with a JSON "
      "error object (exit status 2, HTTP 400), reports a storage error as a JSON error object (exit status 1, "
      "HTTP 500) and leaves the store unchanged on every refusal")

#: Shared by both candidates: the two entry points over one operation.
ENTRIES = '''

def cli(argv, path):
    code, body = remove(path, argv[0] if argv else "")
    print(json.dumps(body))
    return {200: 0, 400: 2, 404: 1, 500: 1}[code]


def http(method, url, path):
    if method != "DELETE" or not url.startswith("/items/"):
        return 404, json.dumps({"error": "no route"})
    try:
        code, body = remove(path, url[len("/items/"):])
    except Exception:  # the dispatcher's own catch: a JSON 500, never a traceback
        return 500, json.dumps({"error": "internal"})
    return code, json.dumps(body)
'''

SOUND = '''import json
import sqlite3

MAX_ID = 2 ** 63 - 1


def connect(path):
    return sqlite3.connect(path)


def parse_id(text):
    if not isinstance(text, str) or not text.isascii() or not text.isdigit() or len(text) > 19:
        raise ValueError("invalid id")
    value = int(text)
    if not 0 < value <= MAX_ID:
        raise ValueError("invalid id")
    return value


def remove(path, raw):
    try:
        item = parse_id(raw)
    except ValueError:
        return 400, {"error": "invalid id"}
    try:
        conn = connect(path)
    except sqlite3.Error:
        return 500, {"error": "storage unavailable"}
    try:
        with conn:
            removed = conn.execute("DELETE FROM items WHERE id = ?", (item,)).rowcount
    except sqlite3.Error:
        return 500, {"error": "storage error"}
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass  # the change, if any, is already durable
    return (200, {"removed": item}) if removed else (404, {"error": "not found"})
''' + ENTRIES

#: Rejects the over-limit decimal cleanly (int() raises ValueError, caught),
#: leaks the 64-bit edge (the binding raises OverflowError, not caught here)
#: and reports a committed delete as a storage error when the release fails.
LEAKING = '''import json
import sqlite3


def connect(path):
    return sqlite3.connect(path)


def remove(path, raw):
    try:
        item = int(raw)
    except ValueError:
        return 400, {"error": "invalid id"}
    if item <= 0:
        return 400, {"error": "invalid id"}
    try:
        conn = connect(path)
        with conn:
            removed = conn.execute("DELETE FROM items WHERE id = ?", (item,)).rowcount
        conn.close()
    except sqlite3.Error:
        return 500, {"error": "storage error"}
    return (200, {"removed": item}) if removed else (404, {"error": "not found"})
''' + ENTRIES

#: The project's own tests: every refusal is checked against the state read
#: afterwards, at both entry points, for the derived boundary classes.
NEGATIVE = '''import json
import sqlite3
import sys

import pytest

import store

LIMIT = getattr(sys, "get_int_max_str_digits", lambda: 0)()
BOUNDARY = ["abc", "", "-1", "0", str(2 ** 63)] + (["1" + "0" * LIMIT] if LIMIT else [])


def _db(tmp_path):
    path = str(tmp_path / "s.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items VALUES (1, 'a')")
    return path


def _rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT id FROM items").fetchall()


class _ReleaseFails:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def close(self):
        self._conn.close()
        raise sqlite3.OperationalError("release failed")


def _cli(raw, path):
    """The command line as a caller runs it: a crash raises here, raw (PR73
    6693873055: no assertion wrapper is needed for a repair to follow)."""
    return store.cli([raw], path)


@pytest.mark.parametrize("raw", BOUNDARY, ids=lambda v: f"len{len(v)}")
def test_cli_refuses_each_boundary_and_changes_nothing(tmp_path, capsys, raw):
    path = _db(tmp_path)
    assert _cli(raw, path) == 2
    assert "error" in json.loads(capsys.readouterr().out)
    assert _rows(path) == [(1,)]


@pytest.mark.parametrize("raw", BOUNDARY, ids=lambda v: f"len{len(v)}")
def test_http_refuses_each_boundary_and_changes_nothing(tmp_path, raw):
    path = _db(tmp_path)
    status, body = store.http("DELETE", "/items/" + raw, path)
    assert status == 400 and "error" in json.loads(body)
    assert _rows(path) == [(1,)]


@pytest.mark.parametrize("entry", ["cli", "http"])
def test_a_storage_failure_before_the_change_is_a_json_error_and_changes_nothing(tmp_path, monkeypatch, capsys,
                                                                                 entry):
    path = _db(tmp_path)

    def unavailable(_path):
        raise sqlite3.OperationalError("unable to open")
    monkeypatch.setattr(store, "connect", unavailable)
    if entry == "cli":
        assert _cli("1", path) == 1 and "error" in json.loads(capsys.readouterr().out)
    else:
        status, body = store.http("DELETE", "/items/1", path)
        assert status == 500 and "error" in json.loads(body)
    assert _rows(path) == [(1,)]


@pytest.mark.parametrize("entry", ["cli", "http"])
def test_a_release_failure_after_the_change_reports_the_change(tmp_path, monkeypatch, capsys, entry):
    path = _db(tmp_path)
    real = store.connect
    monkeypatch.setattr(store, "connect", lambda p: _ReleaseFails(real(p)))
    if entry == "cli":
        assert _cli("1", path) == 0 and json.loads(capsys.readouterr().out) == {"removed": 1}
    else:
        assert store.http("DELETE", "/items/1", path) == (200, json.dumps({"removed": 1}))
    assert _rows(path) == []
'''
HAPPY = '''import sqlite3

import store


def test_remove(tmp_path):
    path = str(tmp_path / "s.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items VALUES (1, 'a')")
    assert store.remove(path, "1") == (200, {"removed": 1})
'''
FILES = {"store.py": "def remove(path, raw):\n    raise NotImplementedError\n", "tests/test_store.py": HAPPY}
VERBOSE = H.GATE.replace(" -q ", " -v ")
PROOF = H.proofs_for("R1", "tests/test_store.py", operation="remove ID 2**63 through the command line",
                     state="one stored item", expect="exit status 2 with a JSON error and the item kept")[0]
PLAN = ("REQUIREMENTS:\nR1: " + R1 + "\nKIND: backend simple\nSCOPE: "
        + json.dumps({"permitted_paths": ["store.py", "tests/test_store.py"], "intended_result": "remove refuses",
                      "acceptance": ["remove refuses invalid IDs"], "max_lines": 200, "proofs": [PROOF]})
        + "\nImplement remove with its refusals and tests.\nCOVERS: R1")
CASE = "tests/test_store.py::test_cli_refuses_each_boundary_and_changes_nothing"
SHOWN = f"the project check executed {CASE} on the delivered source, which refuses 2**63 and keeps the item"


def _run(tmp_path, monkeypatch, *, source, tests, review, repaired=None, plan=PLAN, proof=PROOF, case=CASE,
         shown=SHOWN, r1=R1, files=FILES):
    def lead(call, replay):
        H.write(call, {"store.py": source, "tests/test_store.py": tests})
        proves = f"PROVES: P1 - {case}[len19]\n" if tests != files["tests/test_store.py"] else ""
        return "Implemented remove.\n" + proves + 'CHANGED: ["store.py", "tests/test_store.py"]'

    def gate_fix(call, replay):
        H.write(call, {"store.py": repaired})
        return 'Repaired the failing cases.\nCHANGED: ["store.py"]'

    script = Script(orchestrator=lambda c, r: plan if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                    collaborator=lambda c, r: review,
                    auditor=judging_scenarios(judging(lambda c, r: f"R1: MET - {case}[len19]"), proof, basis=shown),
                    **{"requirements-review": lambda c, r: "COMPLETE",
                       "revision": lambda c, r: "Nothing to change.\nCHANGED: []",
                       **({"gate-fix": gate_fix} if repaired else {})})
    return H.run(tmp_path, monkeypatch, script, files=files, max_tasks=1, record_complete=False, check=VERBOSE,
                 goal="Add item removal by ID to the store's command line and HTTP handler. " + r1)


def _final(replay):
    return H.result_json(replay)["requirements"]["audits"][-1]


def test_the_classes_come_from_the_runtime_and_name_no_candidate(monkeypatch):
    limit = sys.get_int_max_str_digits() if hasattr(sys, "get_int_max_str_digits") else None
    ((rid, classes),) = S.boundary_classes([("R1", R1), ("R2", "README documents remove")])
    assert rid == "R1" and any("2**63" in c for c in classes)
    if limit:
        assert any(f"{limit + 1} digits" in c for c in classes)
    monkeypatch.delattr(sys, "get_int_max_str_digits", raising=False)
    assert not any("digits" in c for c in S.boundary_classes([("R1", R1)])[0][1]), "no limit, no class"
    assert S.boundary_classes([("R1", "remove returns the removed item")]) == [], "not an error requirement"
    note = S.boundary_note([("R1", R1)])
    assert "candidates to check, not new requirements" in note and "Do not lower Python's limit" in note
    assert "applies only where its condition holds" in note and "must stay accepted" in note
    assert "must refuse each" not in note and "where values reach a signed 64-bit or SQLite INTEGER store" in note
    assert "never a defect anyone observed" in note and "crash fails an assertion" not in note
    for word in ("ticket", "sqlite3", "OverflowError", "argparse", "depend", "close"):
        assert word not in note, word


@pytest.mark.requirements_ledger
def test_a_sound_candidate_is_accepted_on_its_executed_state_based_cases(tmp_path, monkeypatch):
    """Clean rejection where the code supports it: every boundary class, the
    pre-change storage failure and the post-change release failure pass at
    both entry points, the review's MET cites an executed case, and R1 is
    accepted. The lead and the reviewer were shown the classes first."""
    replay = _run(tmp_path, monkeypatch, source=SOUND, tests=NEGATIVE,
                  review=f"NO FINDINGS\nR1: MET - {CASE}[len19]")
    assert replay.result.completed, replay.result.error
    assert _final(replay)["verdicts"]["R1"]["met"] is True
    lead, review = replay.of("lead")[0].prompt, replay.of("collaborator")[0].prompt
    for prompt in (lead, review):
        assert "## Input boundaries these requirements may reach" in prompt and "2**63" in prompt
    assert S._ERROR_BOUNDARY_LENS in review
    check = H.result_json(replay)["checks"][-1]
    assert check["passed"], check
    assert not replay.of("revision") and not replay.of("gate-fix")


@pytest.mark.requirements_ledger
def test_a_leaking_candidate_fails_its_own_cases_and_buys_the_ordinary_repair(tmp_path, monkeypatch):
    """The real leak is an observed counterexample: the 64-bit edge escapes
    the command line as a raw OverflowError, the HTTP handler returns 500,
    and a release failure after a committed delete is reported as a failure
    at both entry points. The over-limit decimal is refused cleanly by the
    same candidate. A raw exception mixed with assertion failures used to
    stop the run with no repair; it now buys the ordinary gate fix with the
    harness's diagnosis, and the repaired source passes (PR73 6693873055,
    root's baseline control 1)."""
    replay = _run(tmp_path, monkeypatch, source=LEAKING, tests=NEGATIVE,
                  review=f"NO FINDINGS\nR1: MET - {CASE}[len19]", repaired=SOUND)
    (fix,) = replay.of("gate-fix")
    shown = fix.prompt
    assert "FAILED tests/test_store.py::test_cli_refuses_each_boundary_and_changes_nothing[len19]" in shown
    assert "FAILED tests/test_store.py::test_http_refuses_each_boundary_and_changes_nothing[len19]" in shown
    assert "test_a_release_failure_after_the_change_reports_the_change[cli]" in shown
    assert "test_a_release_failure_after_the_change_reports_the_change[http]" in shown
    assert "FAILED tests/test_store.py::test_a_storage_failure_before" not in shown, "refused before the change"
    assert "Harness diagnosis" in shown and "builtins.OverflowError" in shown
    check = H.result_json(replay)["checks"]
    assert check[-1]["passed"]
    limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if limit:
        assert f"FAILED tests/test_store.py::test_cli_refuses_each_boundary_and_changes_nothing[len{limit + 1}]" \
            not in shown, \
            "the over-limit decimal is refused cleanly by the leaking candidate too"
    assert replay.result.completed, replay.result.error
    assert _final(replay)["verdicts"]["R1"]["met"] is True


@pytest.mark.requirements_ledger
def test_a_met_without_a_run_is_an_untested_risk_naming_the_route(tmp_path, monkeypatch):
    """Sound code, only a happy-path test: nothing observed a failure, and
    nothing exercised a refusal. The review's MET is held as an untested
    risk with the boundary route, never written up as a defect."""
    replay = _run(tmp_path, monkeypatch, source=SOUND, tests=HAPPY, review="NO FINDINGS\nR1: MET - store.py:20")
    (revision,) = replay.of("revision")
    assert "R1: UNVERIFIED - MET claimed without executed negative-path" in revision.prompt
    assert "an untested risk, no counterexample observed" in revision.prompt
    assert "at each listed input boundary that applies to it" in revision.prompt
    notes = H.result_json(replay).get("review_notes") or []
    held = [n for n in notes if n.get("risk") == "untested"]
    assert held and "no counterexample observed" in held[0]["text"]
    assert _final(replay)["verdicts"]["R1"]["met"] is False
    assert not replay.result.completed


#: A contract under which large IDs are valid (root review of a3de6d6, item
#: 7): the same classes are shown, and nothing makes them refusals.
R_OPAQUE = ("IDs are arbitrary-precision integers: removing an item by any such ID succeeds, and an ID with no item "
            "returns a JSON error object with HTTP 404")
OPAQUE = '''import json
from pathlib import Path


def remove(path, raw):
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        return 400, {"error": "invalid id"}
    items = json.loads(Path(path).read_text())
    if raw not in items:
        return 404, {"error": "not found"}
    del items[raw]
    Path(path).write_text(json.dumps(items))
    return 200, {"removed": raw}
'''
OPAQUE_TESTS = '''import json
import sys

import pytest

import store

LIMIT = getattr(sys, "get_int_max_str_digits", lambda: 0)()
LARGE = [str(2 ** 63)] + (["1" + "0" * LIMIT] if LIMIT else [])


def test_remove(tmp_path):
    path = tmp_path / "items.json"
    path.write_text(json.dumps({"1": "a"}))
    assert store.remove(str(path), "1") == (200, {"removed": "1"})


@pytest.mark.parametrize("raw", LARGE, ids=lambda v: f"len{len(v)}")
def test_large_ids_are_accepted(tmp_path, raw):
    path = tmp_path / "items.json"
    path.write_text(json.dumps({raw: "a"}))
    assert store.remove(str(path), raw) == (200, {"removed": raw})
    assert json.loads(path.read_text()) == {}


def test_an_unknown_id_is_a_json_404_and_changes_nothing(tmp_path):
    path = tmp_path / "items.json"
    path.write_text(json.dumps({"1": "a"}))
    assert store.remove(str(path), "2") == (404, {"error": "not found"})
    assert json.loads(path.read_text()) == {"1": "a"}
'''
OPAQUE_HAPPY = '''import json

import store


def test_remove(tmp_path):
    path = tmp_path / "items.json"
    path.write_text(json.dumps({"1": "a"}))
    assert store.remove(str(path), "1") == (200, {"removed": "1"})
'''
OPAQUE_FILES = {"store.py": "def remove(path, raw):\n    raise NotImplementedError\n",
                "tests/test_store.py": OPAQUE_HAPPY}
OPAQUE_CASE = "tests/test_store.py::test_large_ids_are_accepted"
OPAQUE_PROOF = H.proofs_for("R1", "tests/test_store.py", operation="remove ID 2**63", state="that item stored",
                            expect="it is removed")[0]
OPAQUE_PLAN = ("REQUIREMENTS:\nR1: " + R_OPAQUE + "\nKIND: backend simple\nSCOPE: "
               + json.dumps({"permitted_paths": ["store.py", "tests/test_store.py"], "intended_result": "remove works",
                             "acceptance": ["large IDs are accepted"], "max_lines": 200, "proofs": [OPAQUE_PROOF]})
               + "\nImplement remove for arbitrary-precision IDs with tests.\nCOVERS: R1")


@pytest.mark.requirements_ledger
def test_valid_large_ids_stay_accepted_where_the_contract_says_so(tmp_path, monkeypatch):
    """The ID requirement is an error clause, so the candidate classes are
    shown, conditionally; the candidate accepts 2**63 and an over-limit
    decimal as the contract says, its tests prove it, and the run is
    accepted. No class turns into a refusal the goal never asked for."""
    replay = _run(tmp_path, monkeypatch, source=OPAQUE, tests=OPAQUE_TESTS,
                  review=f"NO FINDINGS\nR1: MET - {OPAQUE_CASE}[len19]", plan=OPAQUE_PLAN, proof=OPAQUE_PROOF,
                  case=OPAQUE_CASE, shown=f"the project check executed {OPAQUE_CASE}[len19], which removes 2**63",
                  r1=R_OPAQUE, files=OPAQUE_FILES)
    assert replay.result.completed, replay.result.error
    assert _final(replay)["verdicts"]["R1"]["met"] is True
    lead = replay.of("lead")[0].prompt
    assert "## Input boundaries these requirements may reach" in lead and "must stay accepted" in lead
    assert H.result_json(replay)["checks"][-1]["passed"]
