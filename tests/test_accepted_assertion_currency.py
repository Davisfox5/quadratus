"""Accepted-assertion currency: unit evidence wiring for two sequential task edits.

Base: Davisfox5/quadratus PR73 bc63971914400116bf5b7b699e1c4222b22c47a2.

Scenario: task t1 adds a meaningful passing test after the run-start test
snapshot, so the snapshot cannot protect it. Task t2 then weakens the same
named test, either alone (the app stays correct) or together with an app
regression. The controls check that a sufficiency judgment made on t1's
evidence cannot keep acceptance established on the changed source, even
though the weakened suite is green.

Every reviewer verdict here is SYNTHETIC: an explicit '<O-id>: SUFFICIENT' or
'INSUFFICIENT' line fed straight to Session._judge_obligations. This is unit
evidence wiring, not a live handoff and not a whole-run proof. The Session
is given an invoke that raises if called; nothing here runs Session.run, a
provider or a model. The gate runs a real pytest subprocess through
IntegrationGate.run(), so reports and case ids are measured, never faked.
"""

import sys
from dataclasses import asdict, dataclass
from typing import Any

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.integration import IntegrationGate
from quadratus.session import Session, SessionConfig, _gate_identity

NEG_CASE = "tests/test_negative.py::test_negative"
APP_GOOD = "def add(a, b):\n    return a + b\n"
APP_BAD = "def add(a, b):\n    return abs(a + b)\n"
SMOKE = "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
NEG_STRONG = "from app import add\n\n\ndef test_negative():\n    assert add(-2, -3) == -5\n"
NEG_WEAK = (
    "from app import add\n\n\ndef test_negative():\n    assert isinstance(add(-2, -3), int)\n"
)
NEG_RENAMED = NEG_STRONG.replace("def test_negative", "def test_negative_renamed")
PYTEST_INI = "[pytest]\naddopts = -p no:cacheprovider\n"
REVIEWER = "synthetic-reviewer"
R1_TEXT = "add returns the arithmetic sum for negative operands"


def _forbidden_invoke(*args, **kwargs):
    raise AssertionError("this unit test must never call the model invoke")


@dataclass
class Env:
    session: Any
    root: Any
    gate: IntegrationGate


def _write(env, name, text):
    path = env.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _make_env(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    gate = IntegrationGate(
        [sys.executable, "-m", "pytest", "-q", "--quadratus-report={report}"],
        cwd=root,
        timeout=30,
        minimum_tests=1,
    )
    session = Session(
        "goal",
        ArtifactStore(tmp_path / "artifacts"),
        _forbidden_invoke,
        config=SessionConfig(project=str(root), integration_gate=gate, requirements_ledger=True),
    )
    session.memory.ledger.requirements["R1"] = R1_TEXT
    env = Env(session=session, root=root, gate=gate)
    _write(env, "pytest.ini", PYTEST_INI)
    _write(env, "app.py", APP_GOOD)
    _write(env, "tests/test_app.py", SMOKE)
    return env


def _run_gate(env):
    """A real subprocess gate run, recorded as the session's own check shape.

    The source fingerprint is captured before the run and must be unchanged
    afterward, so the recorded check cannot be labelled with the wrong source.
    """
    session = env.session
    before = session._source_fingerprint()
    result = env.gate.run()
    assert session._source_fingerprint() == before
    assert result.passed, result.output
    check = asdict(result)
    check.update(
        command_cwd=".",
        source=before,
        gate=_gate_identity(session._current_gate()),
        environment=session._environment_id(),
        requirements=session._requirement_revisions(),
    )
    session.checks.append(check)
    return check


def _bind_row(env):
    session = env.session
    session.evidence_obligations = [
        dict(
            oid="O1",
            task="t1",
            kind="acceptance",
            proof_id="P1",
            cases=[NEG_CASE],
            behavior="P1: add handles negative operands",
            requirements=[["R1", session._requirement_revisions()["R1"]]],
            gate=_gate_identity(session._current_gate()),
            runner="gate",
            environment=session._environment_id(),
            execution="missing",
            sufficiency="unjudged",
            judged_by="",
            judged_binding="",
            receipts=[],
            modality="test",
            source_digest=None,
            policy="review/2",
            location="tests/test_negative.py",
            scenario=[],
        )
    ]


def _row(env):
    return env.session.evidence_obligations[0]


def _judge(env, reply):
    """SYNTHETIC reviewer verdict: an explicit line, not a live handoff."""
    env.session._judge_obligations(REVIEWER, reply, {}, ids={"R1"})


def _permits(env):
    """Whether the acceptance predicate keeps a raw R1 MET."""
    found = {"R1": (True, "synthetic auditor: R1 MET")}
    lowered = env.session._apply_acceptance(found, {"R1"})
    return found["R1"][0] and not lowered, lowered


def _task_one(tmp_path):
    """t1: snapshot, add the meaningful test, run it, judge it, accept R1."""
    env = _make_env(tmp_path)
    env.session._snapshot_original_tests()
    original = env.session._original_tests
    assert original is not None
    assert "tests/test_app.py" in original
    assert "tests/test_negative.py" not in original
    _write(env, "tests/test_negative.py", NEG_STRONG)
    _bind_row(env)
    check = _run_gate(env)
    assert NEG_CASE in env.session._check_passed_cases(check)
    env.session._refresh_obligations()
    assert _row(env)["execution"] == "ran"
    _judge(env, "O1: SUFFICIENT")
    assert _row(env)["established"] is True
    permitted, lowered = _permits(env)
    assert permitted, lowered
    return env


def _task_two_weaken(env, regress):
    """t2: weaken the same named test; optionally regress the app too."""
    if regress:
        _write(env, "app.py", APP_BAD)
        # Prove with the real gate that the strong assertion now fails.
        # This expected-failure run is not recorded as a session check.
        result = env.gate.run()
        assert not result.passed
        assert result.returncode == 1
        report = result.report
        assert report["state"] == "parsed"
        assert report["counts"]["failed"] == 1
        assert report["counts"]["errors"] == 0
        failures = report["failures"]
        assert [f["nodeid"] for f in failures] == [NEG_CASE]
        failure = failures[0]
        assert failure["when"] == "call"
        assert failure["assertion"] is True
        assert failure["body"] is True
    _write(env, "tests/test_negative.py", NEG_WEAK)


@pytest.mark.parametrize("regress", [False, True], ids=["assertion_only", "assertion_and_app"])
def test_weakened_same_named_test_cannot_keep_old_acceptance(tmp_path, regress):
    env = _task_one(tmp_path)
    fingerprint_t1 = env.session._source_fingerprint()
    strong_bytes = (env.root / "tests" / "test_negative.py").read_bytes()

    _task_two_weaken(env, regress)
    weak_bytes = (env.root / "tests" / "test_negative.py").read_bytes()

    # New source, same bound case identity, different assertion bytes.
    assert weak_bytes != strong_bytes
    assert tuple(_row(env)["cases"]) == (NEG_CASE,)
    assert env.session._source_fingerprint() != fingerprint_t1

    # Before any new run: the old execution is stale and nothing stands.
    env.session._refresh_obligations()
    assert _row(env)["execution"] == "stale"
    assert _row(env)["established"] is False
    permitted, lowered = _permits(env)
    assert not permitted
    assert [item["requirement"] for item in lowered] == ["R1"]

    # The weakened suite is genuinely green and names the same case.
    check = _run_gate(env)
    assert NEG_CASE in env.session._check_passed_cases(check)
    env.session._refresh_obligations()
    assert _row(env)["execution"] == "ran"
    assert tuple(_row(env)["cases"]) == (NEG_CASE,)

    # Execution is current again, the old sufficiency judgment is not.
    assert _row(env)["sufficiency"] == "sufficient"
    assert _row(env)["sufficiency_current"] is False
    assert _row(env)["established"] is False
    assert not _permits(env)[0]

    # A raw R1 MET with no per-O1 verdict does not count.
    _judge(env, "R1: MET")
    assert _row(env)["established"] is False
    assert not _permits(env)[0]

    # An explicit synthetic INSUFFICIENT does not count either.
    _judge(env, "O1: INSUFFICIENT")
    assert _row(env)["sufficiency"] == "insufficient"
    assert _row(env)["established"] is False
    assert not _permits(env)[0]


def test_recovery_control_restores_acceptance_only_with_fresh_judgment(tmp_path):
    env = _task_one(tmp_path)
    _task_two_weaken(env, regress=True)
    _run_gate(env)
    env.session._refresh_obligations()
    assert not _permits(env)[0]

    # Restore the correct app and the meaningful assertion.
    _write(env, "app.py", APP_GOOD)
    _write(env, "tests/test_negative.py", NEG_STRONG)
    check = _run_gate(env)
    assert NEG_CASE in env.session._check_passed_cases(check)
    env.session._refresh_obligations()
    assert _row(env)["execution"] == "ran"
    assert _row(env)["established"] is False

    _judge(env, "O1: SUFFICIENT")  # synthetic fresh verdict
    assert _row(env)["established"] is True
    permitted, lowered = _permits(env)
    assert permitted, lowered


@pytest.mark.parametrize("variant", ["renamed", "removed"])
def test_removed_or_renamed_bound_test_cannot_redischarge(tmp_path, variant):
    env = _task_one(tmp_path)
    _write(env, "app.py", APP_GOOD)
    if variant == "renamed":
        _write(env, "tests/test_negative.py", NEG_RENAMED)
    else:
        (env.root / "tests" / "test_negative.py").unlink()

    # The suite is green (the smoke test stays) but the bound case is gone.
    check = _run_gate(env)
    assert NEG_CASE not in env.session._check_passed_cases(check)
    env.session._refresh_obligations()
    assert _row(env)["execution"] != "ran"

    _judge(env, "O1: SUFFICIENT")  # synthetic fresh verdict, still not enough
    assert _row(env)["established"] is False
    permitted, lowered = _permits(env)
    assert not permitted
    assert [item["requirement"] for item in lowered] == ["R1"]
