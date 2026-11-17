"""Cycle17 lifecycle: a real ``python -m unittest discover -s tests`` command,
required, under the harness's unittest report producer, beside the pytest
gate, on the real controller with scripted seats.

- R2 names exactly that command. Its receipt is cited, R2 is covered for the
  audit, the auditor's MET with no file citation stands on that citation, and
  the run completes (on 5cfb7da the citation refused the receipt as "a
  selection, not the whole command", and R2 stayed uncovered).
- A custom ``load_tests`` narrows what discovery runs. The citation still
  says only that this command ran and passed; a NOT RUN item naming the
  unittest file is not discharged by the aggregate discover receipt."""
import sys

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_proof_binding import CASE, PROOF_R1, SHOWN, _at, _plan, _proofs, _scope
from tests.lifecycle.test_workflow_repair import Seen, _script

pytestmark = pytest.mark.requirements_ledger

R2 = "`python3 -m unittest discover -s tests` passes on the delivered source."
DISCOVER = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "--quadratus-report={report}"]
PYTEST_FILE = H.GATE.replace(" -m pytest -q ", " -m pytest -q tests/test_app.py ")
UNIT = ("import unittest\n\nfrom app import add\n\n\nclass AddTests(unittest.TestCase):\n"
        "    def test_add_unit(self):\n        self.assertEqual(add(1, 2), 3)\n")
LOADER = UNIT + ("\n    def test_left_out(self):\n        self.assertEqual(add(2, 2), 4)\n\n\n"
                 "def load_tests(loader, tests, pattern):\n"
                 "    return unittest.TestSuite([AddTests('test_add_unit')])\n")


def _run(tmp_path, monkeypatch, name, unit, *, note=None):
    plan = _plan(_scope(proofs=_proofs("R1")), requirements=f"R1: add(a, b) returns the sum\nR2: {R2}")
    auditor = judging_scenarios(lambda c, r: f"R1: MET - {CASE}\nR2: MET - the named command passed on the delivered "
                                             "source", PROOF_R1, basis=SHOWN)
    replies = iter([plan, "DONE", "DONE", "DONE"])
    lead = None
    if note:
        from tests.lifecycle.test_reachable_evidence import _lead
        lead = _lead(note=note)
    script = _script(Seen(), orchestrator=lambda c, r: next(replies, "DONE"), auditor=auditor,
                     **({"lead": lead} if lead else {}))
    return H.run(_at(tmp_path, name), monkeypatch, script, files={**FILES, "tests/test_unit.py": unit},
                 check=PYTEST_FILE, extra_checks=[DISCOVER], max_tasks=2, record_complete=False)


def _discover_receipt(record):
    return [r for c in record["checks"] for r in c.get("receipts") or () if "discover" in str(r.get("command"))][-1]


def test_the_exact_discover_receipt_covers_the_claim_and_the_auditors_met_stands(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, "discover", UNIT)
    record = H.result_json(replay)
    receipt = _discover_receipt(record)
    assert receipt["status"] == "passed" and receipt["required"] and receipt["tests"] == 1
    assert "selection not established" in (receipt.get("selection") or ""), "the file-level reading is unchanged"
    (cover,) = record["receipt_coverage"]
    assert cover["requirement"] == "R2" and cover["command"] == "python3 -m unittest discover -s tests"
    audit = record["requirements"]["audits"][-1]
    assert audit["verdicts"]["R2"]["met"] and audit["citation_receipts"]["R2"]["command"] == cover["command"], audit
    assert not any("does not count" in c.prompt for c in replay.of("orchestrator"))
    assert replay.result.completed, replay.result.error


def test_a_custom_loader_run_is_cited_as_a_command_and_discharges_no_file(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, "loader", LOADER,
                  note="NOT RUN: tests/test_unit.py - I did not run the unittest file myself")
    record = H.result_json(replay)
    assert _discover_receipt(record)["tests"] == 1, "the loader ran one of the file's two cases (test_left_out not)"
    (entry,) = [e for e in record["unexecuted_acceptance"] if e["item"] == "tests/test_unit.py"]
    assert entry["discharge_state"] == "standing", "an aggregate discover receipt discharges no file"
    (row,) = [o for o in record["evidence_obligations"] if o.get("oid") == entry["oid"]]
    assert row["execution"] != "ran"
    cited = [c for c in record["receipt_coverage"] if c["requirement"] == "R2"]
    assert cited and cited[0]["command"] == "python3 -m unittest discover -s tests", \
        "the command ran and passed: that, and only that, is cited"
    assert not replay.result.completed, "the NOT RUN item still holds R1"
