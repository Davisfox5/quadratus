"""A task's scope and gate are settled before its contract is built (map P3.4).

With a repository policy, ``_run_task_recorded`` used to choose the task's
gate and narrow its scope after ``run_task`` had already built the contract,
so the contract named the previous task's gate. Preparation now runs first;
the refusals, the operator's path bound and ``task_gate`` composition are
unchanged, and nothing is run or called during it. Whole Session on the
parallel harness with a real policy file.
"""

import sys

import pytest

from quadratus.integration import IntegrationGate
from quadratus.policy import PolicyError, load_policy
from tests.test_parallel_tasks import Orchestrated, _block
from tests.test_parallel_tasks import _session as parallel_session
from tests.test_policy import document, write_policy


def _policy(project):
    """a.py is pure-logic (gate ``ut``); b.py is test-author (gate ``lint``)."""
    doc = document()
    doc["path_rules"] = [{"paths": ["b.py"], "families": ["test-author"]}]
    doc["gates"] += [{"id": "ut", "runner": "command", "argv": ["true"], "required": True},
                     {"id": "lint", "runner": "command", "argv": ["true"], "required": True}]
    doc["gate_bindings"] = {"unit-tests": {"gate": "ut"},
                            "test-both-ways": {"absent": True, "because": "fixture"},
                            "typecheck": {"absent": True, "because": "fixture"}}
    write_policy(project, doc)
    return load_policy(project)


def _run(tmp_path, plan, *, policy=True, gate_argv=("true",)):
    project = tmp_path / "project"
    project.mkdir()
    config = dict(integration_gate=IntegrationGate(list(gate_argv), cwd=project), requirements_ledger=False)
    if policy:
        config["repository_policy"] = _policy(project)
    script = Orchestrated(plan, lead_delay=0)
    session, _ = parallel_session(tmp_path, script, **config)
    leads = []
    invoke = session.invoke

    def counting(model, prompt, system=None, allow_writes=False):
        if "You are leading" in prompt:
            leads.append(prompt)
        return invoke(model, prompt, system, allow_writes)
    session.invoke = counting
    return session, project, leads


def _ran(session):
    return [[r["id"] for r in check["receipts"]] for check in session.checks]


def test_each_task_contract_names_the_gate_it_ran(tmp_path):
    session, _, _ = _run(tmp_path, [_block("a.py"), _block("b.py"), "DONE"])
    session.run(max_tasks=4)
    named = [list((o.contract or {}).get("required_checks") or ()) for o in session.task_outcomes]
    assert named == [["ut"], ["lint"]]
    assert _ran(session) == named, "the contract describes the gate the task actually ran"


def test_the_contract_scope_is_the_scope_the_task_ran_under(tmp_path, monkeypatch):
    from quadratus.contract import canonical
    from quadratus.session import Session
    seen = []
    gate = Session._run_integration_gate

    def recording(self, lead, spec, task, **kw):
        seen.append(canonical(spec.scope.to_dict()))
        return gate(self, lead, spec, task, **kw)
    monkeypatch.setattr(Session, "_run_integration_gate", recording)
    session, _, _ = _run(tmp_path, [_block("a.py"), "DONE"])
    session.run(max_tasks=2)
    import json
    ran_under = json.loads(seen[0])
    assert session.task_outcomes[0].contract["scope"] == ran_under
    assert ran_under["forbidden_paths"] == [".env", ".git/**"], "the policy's narrowing is in the contract"


def test_an_operator_gate_is_kept_beside_the_policy_gates(tmp_path):
    session, _, _ = _run(tmp_path, [_block("a.py"), "DONE"], gate_argv=(sys.executable, "-c", "pass"))
    session.run(max_tasks=2)
    named = list(session.task_outcomes[0].contract["required_checks"])
    assert "ut" in named and len(named) >= 2, named
    assert _ran(session) == [named]


def test_a_blocked_policy_path_stops_before_any_lead_call(tmp_path):
    session, _, leads = _run(tmp_path, [_block(".env"), "DONE"])
    with pytest.raises(PolicyError, match="Write denied: .env"):
        session.run(max_tasks=2)
    (outcome,) = session.task_outcomes
    assert outcome.closed_as == "stopped:PolicyError" and outcome.dispatch["state"] == "not_dispatched"
    assert outcome.contract is None and not leads and not session.checks
    # The source record is kept as before preparation moved (Codex, 5861147842).
    assert outcome.source_before == session._source_identity()
    assert len(outcome.source_before) == 64, outcome.source_before


def test_a_task_without_a_policy_keeps_the_operator_gate(tmp_path):
    session, _, _ = _run(tmp_path, [_block("a.py"), "DONE"], policy=False)
    session.run(max_tasks=2)
    assert list(session.task_outcomes[0].contract["required_checks"]) == ["check"]
    assert len(session.checks) == 1 and session.checks[0]["passed"], "the operator's own gate ran"
    assert session.checks[0]["command"] == "true"
