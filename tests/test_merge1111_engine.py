"""The development line's engine code held to the review batch's rules
(conformance audit of the merged tree, 2026-11-12).

Each test names the audit finding it closes. All of them run offline:
scripted replies, a fake CLI, a temporary folder, never a model.
"""

import json

import pytest

import tests.test_review1110_compete as arena
from quadratus.providers import PermissionBlocked
from quadratus.session import _never_replaced
from tests.test_review1110_compete import _three_seat_trust  # noqa: F401 -- autouse fixture

# -- transport:1: a contest never replaces a routed lead that hit a permission block -------


def _blocked():
    return PermissionBlocked("Grok stopped: Fetch on a file target needed approval",
                             permission_block=dict(tool="Fetch", scheme="file"))


def test_a_routed_lead_stopped_on_a_permission_block_is_not_replaced_by_the_challenger(tmp_path):
    """With one lead the run stops on the block. A contest must not be a
    quiet way round it: the challenger's finished draft is not merged."""
    assert _never_replaced(_blocked())
    script = arena.Script([arena._task(), "DONE"],
                          {arena.OPUS: arena._raises(_blocked()), arena.SOL: arena._sub(), arena.GROK: arena._sub()})
    session, project = arena._session(tmp_path, script, compete="complex")
    with pytest.raises(PermissionBlocked):
        session.run(max_tasks=3)
    record = arena._held(session)
    assert record["winner_model"] is None and record["decided_by"] == arena.contest.ROUTED_STOP
    assert "def sub" not in (project / "calc.py").read_text() and not session.completed


def test_a_challenger_stopped_on_a_permission_block_only_loses_the_contest(tmp_path):
    script = arena.Script([arena._task(), "DONE"],
                          {arena.OPUS: arena._sub(), arena.SOL: arena._raises(_blocked()), arena.GROK: arena._sub()})
    session, project = arena._session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = arena._held(session)
    assert record["winner_model"] == arena.OPUS and record["decided_by"] == "completed"
    assert "def sub" in (project / "calc.py").read_text() and session.completed


# -- transport:5: a replay refused by the finish hold ends in the call's own failure -------

_503 = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "api_error_status": 503,
        "result": "API Error: 503 Service Unavailable", "session_id": "s",
        "usage": {"input_tokens": 0, "output_tokens": 0}}


def test_an_editing_replay_refused_to_keep_the_finish_ends_in_the_first_calls_own_failure(tmp_path, monkeypatch):
    """The editing call was sent once and failed. FinishReserved means
    "refused before it started", and the session reads it as no call made."""
    import tests.test_review1110_transport as T
    from quadratus.config import Settings
    from quadratus.delegation import DelegationLedger
    from quadratus.providers import ProviderError
    from quadratus.run_budget import FinishReserved, RunBudget, RunLimits
    from quadratus.runtime import Fleet

    monkeypatch.setattr("quadratus.providers.random.uniform", lambda a, b: 0.0)
    root = T._project(tmp_path)
    script, runs = T._fake_cli(tmp_path, "claude", f"print(json.dumps({_503!r}))\nsys.exit(1)\n")
    monkeypatch.setenv("QUADRATUS_CLI_BINARY_CLAUDE", str(script))
    budget = RunBudget(RunLimits(max_calls=3, max_reported_tokens=1_000_000, wall_seconds=600,
                                 finish_wall_seconds=0))
    budget.protect_finish(["goal", "audit"])
    ledger = DelegationLedger()
    fleet = Fleet(Settings(backend="cli", retry_base_delay=0.0, max_retries=4, cli_timeout=30.0),
                  project=root, allow_writes=True, delegation_ledger=ledger, run_budget=budget)
    with pytest.raises(ProviderError) as caught:
        fleet.invoke("claude:opus", "edit the module", allow_writes=True)
    assert not isinstance(caught.value, FinishReserved) and T._runs(runs) == 1
    assert caught.value.transport_status == 503 and caught.value.retryable is True
    assert "keep the calls held for the finish" in str(caught.value) and caught.value.resend_refused
    # The refusal itself is still on the ledger, as a call that was not made.
    assert [(e.outcome, e.invoked) for e in ledger.events][-1] == ("FinishReserved", False)


# -- trust:3: a kept final audit is not reused when the unmeasured changes differ ----------


def test_a_kept_final_audit_is_not_reused_once_the_unmeasured_changes_differ(tmp_path):
    """The audit prompt lists what editing calls changed where the measured
    diff does not read. A kept audit answered a prompt with another list."""
    from tests.test_unexecuted_acceptance import UNIT, _session

    s, prompts = _session(tmp_path, f"R1: MET - {UNIT}\nR5: NOT MET - no console check\n")
    s._audit_requirements(final=True)
    before = s._audit_binding(None)
    s._audit_requirements(final=True)
    assert len(prompts) == 1, "nothing changed: the kept audit stands"
    s.unmeasured_changes["t1"] = ["build/helper.txt"]
    changed = s._audit_binding(None)
    assert changed != before
    s._audit_requirements(final=True)
    assert len(prompts) == 2 and "build/helper.txt" in prompts[-1]
    # Order of tasks and names is not a difference; another name is.
    s.unmeasured_changes = {"t2": ["b", "a"], "t1": ["build/helper.txt"]}
    one = s._audit_binding(None)
    s.unmeasured_changes = {"t1": ["build/helper.txt"], "t2": ["a", "b"]}
    assert s._audit_binding(None) == one != changed


# -- intake:2: a lead held for a sent-back task does not lead another task of the same id --


@pytest.mark.requirements_ledger
def test_a_lead_picked_for_a_sent_back_task_is_not_kept_for_a_different_task_under_its_id(tmp_path, monkeypatch):
    """Naming spends no id, so the task named after a send-back has the id
    of the one sent back. The lead asked about the first task's packet was
    picked for a complex backend task; the simple one named next is routed
    on its own labels."""
    import json

    from quadratus.session import Session
    from tests.lifecycle import harness as H
    from tests.lifecycle import test_packet_extension as px

    def task(scope, kind):
        return f"KIND: {kind}\nSCOPE: {json.dumps(scope)}\nImplement add in app.py.\nCOVERS: R1"

    picks = []
    pick = Session._pick_lead

    def record(self, spec):
        lead = pick(self, spec)
        picks.append((spec.task_id, spec.complexity, lead))
        return lead
    monkeypatch.setattr(Session, "_pick_lead", record)
    replay = H.run(tmp_path, monkeypatch,
                   px._script(px.FIRST_REQ + task(px._oversized(), "backend complex"),
                              task(px._narrow(), "backend simple")),
                   files=px.FILES, max_tasks=2, record_complete=False)
    assert replay.of("packet-justification"), "the first task was sent back over its packet"
    assert [(tid, level) for tid, level, _ in picks] == [("t1", "complex"), ("t1", "simple")]
    led = {call.model for call in replay.of("lead")}
    asked = {call.model for call in replay.of("packet-justification")}
    assert picks[0][2] != picks[1][2] and led and led != asked, "the simple task has its own routed lead"


# -- intake:5: a refused resume is the command line's error, not a traceback ---------------


def _cli_with_runner(monkeypatch, name, raised):
    from quadratus import baseline, cli, project_run

    def run(*args, **kwargs):
        raise raised
    monkeypatch.setattr(project_run if name == "run_project" else baseline, name, run)
    return cli


def test_a_refused_resume_is_said_in_words_with_exit_1(tmp_path, monkeypatch, capsys):
    from quadratus.checkpoint import CheckpointRefused

    cli = _cli_with_runner(monkeypatch, "run_project", CheckpointRefused("the run is sealed"))
    assert cli.main(["goal", "--project", str(tmp_path), "--resume", "20261112T000000Z-aaaaaaaa"]) == 1
    assert "Error: resume refused: the run is sealed" in capsys.readouterr().out


@pytest.mark.parametrize("flags, runner", [((), "run_project"), (("--baseline", "openai:sol"), "run_baseline")])
def test_a_stale_client_is_said_in_words_and_any_other_fault_keeps_its_traceback(
        tmp_path, monkeypatch, capsys, flags, runner):
    from quadratus.cli_providers import StaleClient

    cli = _cli_with_runner(monkeypatch, runner, StaleClient("codex 0.1 is older than 0.9"))
    assert cli.main(["goal", "--project", str(tmp_path), *flags]) == 1
    assert "Error: codex 0.1 is older than 0.9" in capsys.readouterr().out
    cli = _cli_with_runner(monkeypatch, runner, RuntimeError("a fault"))
    with pytest.raises(RuntimeError, match="a fault"):
        cli.main(["goal", "--project", str(tmp_path), *flags])


# -- intake:7: a baseline runs under a project run's client preflight ----------------------

from tests.test_review1110_baseline import env  # noqa: E402, F401 -- fixture


def test_a_baseline_refuses_a_stale_client_before_any_call_and_records_the_release(env, monkeypatch):  # noqa: F811
    import tests.test_review1110_baseline as B

    project, _, trace = env
    result = B.run(env)
    assert result.error == "" and len(B.calls(trace)) == 1
    resolved = B.record(result)["model_resolution"]
    assert isinstance(resolved, dict) and "claude:opus" in json.dumps(resolved), "the release the key ran as"

    # The same client, now older than the version the engine qualified.
    monkeypatch.setattr("quadratus.cli_providers.client_version", lambda path, args: "2.1.296")
    monkeypatch.setattr("quadratus.cli_providers._CLIENT_VERSIONS", {}, raising=False)
    result = B.run(env)
    assert result.error.startswith("StaleClient: claude: installed client 2.1.296")
    assert "openai:" not in result.error and "grok:" not in result.error, "another vendor's client is not this baseline's"
    assert len(B.calls(trace)) == 1, "no call was made"


# -- intake:3: a listed task the intake would refuse is found before any listed task runs --


def _listed_run(tmp_path, monkeypatch, second):
    from tests.lifecycle import test_explicit_tasks as X

    replay = X._run(tmp_path, monkeypatch, [X.FAVICON_TASK, second], record_complete=False)
    return replay, json.loads((replay.result.run_dir / "result.json").read_text())["explicit_tasks"]


def test_a_listed_path_in_another_letter_case_is_refused_before_the_first_listed_task_runs(tmp_path, monkeypatch):
    from tests.lifecycle import test_explicit_tasks as X

    scope = dict(X.README_T2, permitted_paths=["readme.md"])
    replay, record = _listed_run(tmp_path, monkeypatch,
                                 "KIND: docs simple\nSCOPE: " + json.dumps(scope) + "\nDocument add in readme.md.")
    assert replay.calls == [], "no model call: the whole list is read first"
    assert "TaskListInvalid" in replay.result.error and "listed task 2: SCOPE paths are exact" in replay.result.error
    assert record["ran"] == [] and record["invalid"]["index"] == 2


def test_a_listed_role_packet_over_the_default_is_refused_before_the_first_listed_task_runs(tmp_path, monkeypatch):
    from tests.lifecycle import test_explicit_tasks as X

    scope = dict(X.README_T2, acceptance=[f"README names add, case {n}: " + "x" * 200 for n in range(70)])
    replay, record = _listed_run(tmp_path, monkeypatch,
                                 "KIND: docs simple\nSCOPE: " + json.dumps(scope) + "\nDocument add in README.md.")
    assert replay.calls == []
    assert "listed task 2: The lead role packet for this task would be" in replay.result.error
    assert "A listed task has no orchestrator to grant a larger allowance" in replay.result.error
    assert record["ran"] == []


# -- intake:6: a child numbers a packet request past every request its parent holds --------


def test_a_child_numbers_its_packet_request_past_the_requests_its_parent_kept_back():
    """The child is handed only its task's granted requests. Numbered from
    that list, its lead's request repeated the granted request's id."""
    from types import SimpleNamespace

    from quadratus.session import Session

    narrowed = dict(id="PX-t2-1", task="t2", state="narrow")
    granted = dict(id="PX-t2-2", task="t2", state="granted")
    parent = SimpleNamespace(packet_requests=[narrowed, granted, dict(id="PX-t3-1", task="t3", state="granted")])
    child = SimpleNamespace(packet_requests=[])
    Session._packet_requests_for_child(parent, child, "t2")
    assert child.packet_requests == [granted] and child.packet_requests[0] is granted, "the same record"
    first = Session._packet_request_id(child, "t2")
    assert first == "PX-t2-3" and first not in {r["id"] for r in parent.packet_requests}
    child.packet_requests.append(dict(id=first, task="t2", state="requested"))
    assert Session._packet_request_id(child, "t2") == "PX-t2-4"
    # The parent's own numbering is the count it always was.
    assert Session._packet_request_id(parent, "t2") == "PX-t2-3"
    assert Session._packet_request_id(parent, "t9") == "PX-t9-1"


def test_no_packet_request_id_is_built_from_a_list_outside_the_one_numbering():
    """Both request sites and both child hand-overs go through the two
    methods above, so the count and the hand-over cannot drift apart."""
    import inspect

    from quadratus import session, session_packets

    text = inspect.getsource(session) + inspect.getsource(session_packets)
    assert text.count('f"PX-{') == 1 and text.count("_packet_requests_withheld = len(") == 1
    assert text.count("self._packet_requests_for_child(") == 2 and text.count("self._packet_request_id(") == 2


# -- intake:8: a batch that was not started spends its ids ---------------------------------


@pytest.mark.requirements_ledger
def test_a_batch_named_after_one_that_was_not_started_does_not_read_as_not_started(tmp_path, monkeypatch):
    """The not-started records are keyed by task id. While those ids were
    issued again, a later batch that was only sent back read as not started."""
    from quadratus.session import Session
    from tests.lifecycle import harness as H
    from tests.lifecycle import test_cycle21_dispatch_intake as intake

    admit, asked = Session._admit_batch, []

    def short_once(self, parsed):
        asked.append([spec.task_id for spec, *_ in parsed])
        return "2 calls are left and the batch needs 6" if len(asked) == 1 else admit(self, parsed)
    monkeypatch.setattr(Session, "_admit_batch", short_once)
    unpaid = intake.REQS + "PARALLEL\n" + intake._task(intake._narrow()) + "\n---\n" + intake._readme()
    oversized = "PARALLEL\n" + intake._task(intake._oversized()) + "\n---\n" + intake._readme()
    replay = H.run(tmp_path, monkeypatch, intake._script(unpaid, oversized, "DONE"), files=intake.FILES,
                   max_tasks=3, record_complete=False)
    assert asked == [["t1", "t2"]], "the second batch was sent back at intake, before admission"
    events = [(e["event"], e["tasks"]) for e in intake._ledger(replay) if e["event"] in ("not_started", "sent_back")]
    assert events[0] == ("not_started", ["t1", "t2"])
    assert events[1] == ("sent_back", ["t3", "t4"]), "its own ids, and its own reason"


# -- check of engine: the same holes by their other doors ---------------------------------


@pytest.mark.requirements_ledger
def test_a_lead_held_by_the_capture_check_is_not_kept_for_a_different_task_under_its_id(tmp_path, monkeypatch):
    """The capture check also picks and holds a lead, and the path and packet
    checks after it can still send the task back. Its hold says what was
    named too, so a different task named next under the id is routed anew."""
    from quadratus.session import Session
    from tests.lifecycle import harness as H
    from tests.lifecycle import test_packet_extension as px

    picks = []
    pick = Session._pick_lead

    def record(self, spec):
        lead = pick(self, spec)
        picks.append((spec.task_id, spec.complexity, lead))
        return lead
    monkeypatch.setattr(Session, "_pick_lead", record)
    ui = dict(px._narrow(), permitted_paths=["index.html", "readme.md"])
    first = px.FIRST_REQ + f"KIND: frontend complex\nSCOPE: {json.dumps(ui)}\nImplement add in app.py.\nCOVERS: R1"
    second = f"KIND: backend simple\nSCOPE: {json.dumps(px._narrow())}\nImplement add in app.py.\nCOVERS: R1"
    H.run(tmp_path, monkeypatch, px._script(first, second), files=dict(px.FILES, **{"index.html": "<p>x</p>\n"}),
          max_tasks=2, record_complete=False)
    assert [(tid, level) for tid, level, _ in picks] == [("t1", "complex"), ("t1", "simple")], picks


def test_a_refused_replay_of_a_call_measured_unchanged_is_not_a_partial_work_stop(monkeypatch):
    """A failure that could have written is PartialWorkSuspected until the
    project is measured. Measured unchanged and then refused its replay, it is
    the ordinary failure a second failed attempt would have been."""
    from quadratus import runtime
    from quadratus.providers import PartialWorkSuspected, ProviderError
    from quadratus.run_budget import FinishReserved

    fleet = object.__new__(runtime.Fleet)
    raised = [PartialWorkSuspected("did not complete", cause=OSError("reset"), interrupted=False, retryable=True),
              FinishReserved("held for the finish")]

    def generate(*a, **k):
        raise raised.pop(0)
    fleet._generate = generate
    fleet._project_unchanged = lambda before: True
    fleet._note_replay = lambda key, exc: None
    monkeypatch.setattr(runtime.time, "sleep", lambda s: None)
    with pytest.raises(ProviderError) as caught:
        fleet._generate_editing("claude:opus", type("V", (), {"retry_base_delay": 0})(), "p", {}, "")
    assert not isinstance(caught.value, PartialWorkSuspected) and caught.value.resend_refused
    assert "measured unchanged" in str(caught.value)


def test_a_childs_own_child_counts_the_requests_kept_back_above_it():
    from quadratus.session import Session

    parent, child = object.__new__(Session), object.__new__(Session)
    parent.packet_requests = [dict(id="PX-t2-2", task="t2", state="granted")]
    parent._packet_requests_withheld = 1
    parent._packet_requests_for_child(child, "t2")
    assert child._packet_request_id("t2") == "PX-t2-3"
