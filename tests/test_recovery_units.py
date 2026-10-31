"""Units of the recovery lifecycle and improvement loop (2026-10-31).

The allowlist that decides what an observed call may pass on, the cost of an
outer invocation with its unknowns, family aggregation and its proposals, and
the ``unreviewed`` finding kind: owned review debt is never a defect, never
carried to the audit, and never settled by an id line.
"""
from types import SimpleNamespace

from quadratus import delivery, recovery
from quadratus.judgment import RecheckReading, Verdict


def _event(**kw):
    base = dict(invoked=True, invocation_id="inv-1", task="t1", role="collaborator", requested_model="claude:opus",
                outcome="TurnLimitReached", turn_limited=True, model_turns=8, max_turns=8, input_tokens=300,
                output_tokens=40, seconds=12.5)
    base.update(kw)
    return SimpleNamespace(**base)


# -- the allowlist ------------------------------------------------------------------------

def test_only_successful_reads_of_existing_project_files_pass_and_nothing_else_is_quoted(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("")
    trace = dict(cwd="/scratch/copy", tool_calls=[
        dict(name="Read", target="/scratch/copy/app.py", outcome="success"),
        dict(name="Read", target="./tests/test_app.py", outcome="completed"),
        dict(name="Read", target="missing.py", outcome="success"),
        dict(name="Read", target="/etc/passwd", outcome="success"),
        dict(name="Read", target="/scratch/copy/../secret.txt", outcome="success"),
        dict(name="Read", target="pending|BEGIN|in_transaction", outcome="success"),
        dict(name="Read", target="app.py", outcome="error"),
        dict(name="Grep", target="API_KEY=[A-Z0-9]+", outcome="success"),
        dict(name="Bash", target="env | grep TOKEN", outcome="success"),
        dict(name="Bash", target="pytest -q", outcome="error"),
        dict(name="Edit", target="app.py", outcome="denied by policy"),
        dict(name="mcp__weird", target="anything", outcome="success"),
    ], files_read=["API_KEY=[A-Z0-9]+"], reasoning=["hidden"], texts=["narration"],
        integrity=dict(lines=12, invalid=0, missing_files=0, truncated=False, complete=True))
    obs = recovery.safe_observations(trace, project_root=tmp_path)
    assert obs["status"] == "observed"
    assert obs["files_read"] == ["app.py", "tests/test_app.py"]
    assert obs["searches"] == 1 and obs["commands"] == 2 and obs["command_outcomes"] == {"error": 1, "success": 1}
    assert obs["writes_attempted"] == 1 and obs["tools"]["Edit"] == {"denied": 1}
    assert obs["tools"]["other"] == {"success": 1} and obs["tool_calls"] == 12
    text = repr(obs)
    for leaked in ("API_KEY", "TOKEN", "passwd", "secret", "pending|BEGIN", "missing.py", "hidden", "narration",
                   "mcp__weird", "/scratch/copy", "pytest -q"):
        assert leaked not in text, leaked


def test_missing_context_is_an_explicit_reason_never_an_empty_observation():
    assert recovery.safe_observations(None) == dict(status="unavailable", reason="no tool record was found for the call")
    got = recovery.safe_observations({"tool_calls": []}, unavailable="the call's transcript was not found")
    assert got == dict(status="unavailable", reason="the call's transcript was not found")


def test_an_absolute_path_outside_the_calls_own_directory_never_passes(tmp_path):
    (tmp_path / "app.py").write_text("")
    trace = dict(cwd="/scratch/copy", tool_calls=[dict(name="Read", target="/scratch/copy-2/app.py",
                                                       outcome="success")])
    assert recovery.safe_observations(trace, project_root=tmp_path)["files_read"] == []
    trace["cwd"] = None
    trace["tool_calls"][0]["target"] = "/scratch/copy/app.py"
    assert recovery.safe_observations(trace, project_root=tmp_path)["files_read"] == []


# -- measured cost: outer invocations, unknowns stay unknown --------------------------------

def test_an_outer_call_is_measured_from_its_ledger_event_and_unknown_stays_none():
    assert recovery.call_cost(None) is None
    assert recovery.call_cost(_event(invoked=False)) is None, "a selection row is no attempt"
    got = recovery.call_cost(_event())
    assert got["tokens"] == 340 and got["model_turns"] == 8 and got["seconds"] == 12.5
    unknown = recovery.call_cost(_event(input_tokens=None, output_tokens=None, seconds=None))
    assert unknown["tokens"] is None and unknown["input_tokens"] is None and unknown["seconds"] is None


def test_costs_count_each_invocation_once_and_report_unknowns_as_counts():
    a = recovery.call_cost(_event())
    b = recovery.call_cost(_event(invocation_id="inv-2", input_tokens=None, output_tokens=None, seconds=None))
    records = [dict(failed_call=a, attempts=[dict(call=b), dict(call=None)]),
               dict(failed_call=a, attempts=[dict(call=dict(tokens=5))])]
    got = recovery.costs(records)
    assert got == dict(calls=2, tokens_known=340, calls_with_unknown_usage=1, seconds_known=12.5,
                       calls_with_unknown_duration=1, unidentified_calls=1)
    nothing = recovery.costs([dict(failed_call=b, attempts=[])])
    assert nothing["tokens_known"] is None and nothing["seconds_known"] is None, "never zero for unknown"


# -- families and proposals -----------------------------------------------------------------

def _occurrence(rid, cls="review_capped", seat="claude:opus", role="collaborator", result="held: owned review debt"):
    return dict(id=rid, task=rid.split("-")[0][2:], role=role, seat=seat, outcome_class=cls, result=result,
                failed_call=recovery.call_cost(_event(invocation_id=f"inv-{rid}")), attempts=[
                    dict(action="complete_judgment", outcome="capped: no verdict",
                         call=recovery.call_cost(_event(invocation_id=f"inv-{rid}-c")))])


def test_proposals_aggregate_by_family_deterministically_and_are_never_adopted():
    records = [_occurrence("RVt1-1"), _occurrence("RVt3-1", result="recovered: the owner completed the owed review"),
               _occurrence("RVt2-1", seat="openai:sol"), _occurrence("RVt4-1", cls="provider_quota",
                                                                     role="run", result="stopped: WindowExhausted")]
    first, second = recovery.proposals(records), recovery.proposals(list(reversed(records)))
    assert [p["family"] for p in first] == [p["family"] for p in second], "order-independent families"
    assert [p["family"] for p in first] == ["provider_quota|run|claude", "review_capped|collaborator|claude",
                                            "review_capped|collaborator|openai"]
    claude = first[1]
    assert sorted(claude["occurrences"]) == ["RVt1-1", "RVt3-1"]
    assert claude["evidence"] == "2 occurrence(s): 1 recovered, 1 held as owned debt, 0 stopped, 0 other"
    assert claude["cost"]["calls"] == 4 and claude["cost"]["tokens_known"] == 4 * 340
    assert "recurrence, not a confirmed cause" in claude["confidence"]
    for p in first:
        assert p["status"].startswith("proposed: not adopted")
        assert p["regression_scenario"] and p["recommendation"]
    assert "observed once; the cause is not confirmed" == first[0]["confidence"]


def test_the_report_section_says_unknown_usage_rather_than_zero():
    record = _occurrence("RVt1-1")
    record["failed_call"] = recovery.call_cost(_event(input_tokens=None, output_tokens=None))
    record["attempts"] = []
    text = recovery.render([record], recovery.proposals([record]))
    assert "failed call usage unknown" in text and "tokens unknown" in text and "1 with unknown usage" in text
    assert "Status: proposed: not adopted" in text
    assert recovery.render([], []) == ""


def test_every_outcome_class_has_a_meaning_and_protective_classes_are_stops():
    assert recovery.PROTECTIVE <= set(recovery.OUTCOME_CLASSES)
    assert all(recovery.OUTCOME_CLASSES.values())
    assert {"complete_judgment", "hold_owned_debt", "owner_settlement", "stop", "reconcile"} == set(recovery.ACTIONS)


# -- the unreviewed kind ------------------------------------------------------------------------

def _owed(**kw):
    base = dict(fid="RFT1-1", author="claude:opus", role="collaborator",
                claim="UNREVIEWED: the review of t1 by claude:opus did not finish",
                basis=delivery.FindingBasis("none"), impact="unstated", source_digest="src", task="t1",
                kind="unreviewed")
    base.update(kw)
    return delivery.Finding(**base)


def test_owed_review_debt_is_not_a_defect_and_is_not_carried_to_the_audit():
    owed = _owed()
    assert delivery.owed_review(owed) and not owed.audit_carried and not owed.holds_effects
    risky = _owed(impact="data-loss")
    assert not risky.holds_effects, "a debt record holds no effect"
    assert delivery.Finding.from_dict(owed.to_dict()) == owed
    legacy = owed.to_dict()
    legacy.pop("kind")
    assert delivery.Finding.from_dict(legacy).kind == "defect", "records written before the kind stay defects"


def test_an_id_line_never_settles_owed_debt_whoever_writes_it():
    owed = _owed()
    for state in (delivery.RESOLVED, delivery.UNRESOLVED, delivery.INVALIDATED):
        reading = RecheckReading(dispositions={"RFT1-1": Verdict(state, "looked again")}, new=(), foreign=(),
                                 notes=(), coverage=None)
        (got,) = delivery.apply_reading([owed], reading, author="claude:opus", role="recheck", binding="b",
                                        judged_binding="b", source_digest="src", resolve=lambda _: True)
        assert got.history[-1].state == "unjudged" and got.state == "open" and not got.closed, state
        assert "never an id line" in got.history[-1].why


def test_an_unknown_kind_is_refused():
    try:
        _owed(kind="maybe")
    except ValueError as exc:
        assert "kind must be one of" in str(exc)
    else:
        raise AssertionError("an unknown finding kind was accepted")


# -- review findings on this batch (2026-10-31) -------------------------------------------

def test_a_search_scoped_to_an_existing_file_is_never_a_read(tmp_path):
    """A Grep or Glob whose target is an existing project path (trace._target
    prefers 'path') is a search, not an opened file."""
    (tmp_path / "app.py").write_text("")
    trace = dict(cwd="/c", tool_calls=[dict(name="Grep", target="app.py", outcome="success"),
                                       dict(name="Glob", target="/c/app.py", outcome="success"),
                                       dict(name="LS", target="app.py", outcome="success")])
    obs = recovery.safe_observations(trace, project_root=tmp_path)
    assert obs["files_read"] == [] and obs["searches"] == 3


def _session(tmp_path, invoke=lambda *a, **k: ""):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    return Session("goal", ArtifactStore(tmp_path / "store"), invoke, config=SessionConfig())


def test_a_terminal_stop_is_classified_by_type_never_by_the_text_a_model_wrote(tmp_path):
    from quadratus.providers import ProviderError
    session = _session(tmp_path)
    narrated = ProviderError("grok: turn ended cancelled: Running pytest now; the first attempt timed out "
                             "and unknown_usage was reported")
    session._record_terminal_recovery(narrated)
    (record,) = session.recoveries
    assert record["outcome_class"] == "provider_error" and record["decision"] == "stop: the run's own stop rule"
    assert record["detail"] == "ProviderError" and "pytest" not in repr(record)
    session._record_terminal_recovery(TimeoutError("claude CLI timed out after 900s"))
    assert session.recoveries[-1]["outcome_class"] == "provider_timeout"
    caused = ProviderError("wrapped")
    caused.__cause__ = TimeoutError("x")
    session._record_terminal_recovery(caused)
    assert session.recoveries[-1]["outcome_class"] == "provider_timeout", "the cause chain is read by type"


def test_a_stop_after_successful_calls_names_no_failed_call(tmp_path):
    from quadratus.delegation import DelegationLedger
    from quadratus.integration import CheckUnattributable
    session = _session(tmp_path)
    ledger = DelegationLedger(tmp_path / "invocations.jsonl")
    session.config.delegation_ledger = ledger
    session._active_spec = SimpleNamespace(task_id="t1")
    ledger.events.append(SimpleNamespace(invoked=True, task="t1", role="collaborator", requested_model="claude:opus",
                                         canonical_model=None, outcome="ok", invocation_id="ok-1"))
    session._record_terminal_recovery(CheckUnattributable("the check failed"))
    (record,) = session.recoveries
    assert record["outcome_class"] == "stop_rule" and record["failed_call"] is None


def test_a_context_for_debt_no_call_was_made_for_says_so(tmp_path):
    session = _session(tmp_path)
    record = dict(outcome_class="reviewer_unavailable", meaning=recovery.OUTCOME_CLASSES["reviewer_unavailable"],
                  failed_call=None, observations=dict(status="unavailable", reason="not read for this outcome"),
                  source=None, question="a cross-vendor review of t1's capped final work")
    text = session._recovery_context(SimpleNamespace(task_id="t1"), "openai:sol", record, changed=["app.py"])
    assert "No earlier call of yours on this work was made" in text
    assert "previous reply" not in text and "could not observe" not in text and "earlier source" not in text


def test_owner_settlements_are_counted_across_every_occurrence_that_names_the_debt(tmp_path):
    """A later occurrence that re-owes the same record never resets the
    owner's attempts or the per-source rule (review of a8a9af2, debt:2)."""
    calls = []

    def invoke(model, prompt, **k):
        calls.append(model)
        return "I looked and it seems fine."

    session = _session(tmp_path, invoke)
    session._available = lambda key: True
    spec = SimpleNamespace(task_id="t1", description="Implement add.", scope=None, kind="review",
                           complexity="simple", lead=None, needs=frozenset())
    first = session._recovery_open(spec, role="collaborator", seat="claude:opus", kind="review",
                                   outcome_class="review_capped", question="q", observe=False)
    (fid,) = session._owe_review(spec, "claude:opus", "review", "capped", role="collaborator", record=first)
    record = next(r for r in session.review_findings if r["fid"] == fid)
    row = session._settle_owed(spec, record, situation="s", site="one")
    assert row["verdict"] == "kept: the owner's reply gave no valid verdict" and len(calls) == 1
    second = session._recovery_open(spec, role="collaborator", seat="claude:opus", kind="review",
                                    outcome_class="review_capped", question="q", observe=False)
    assert session._owe_review(spec, "claude:opus", "review", "capped again", role="collaborator",
                               record=second) == [fid], "one record, never a second for the same debt"
    row = session._settle_owed(spec, record, situation="s", site="two")
    assert row["verdict"] == "kept: the owner already judged this exact source without a verdict"
    assert len(calls) == 1 and session._owed_attempts(fid) == 1
