"""Contests and the scoreboard (whole-repo review 2026-11-10, design 1 and 2).

The session engine had collaboration and no competition. These tests drive
the first contest offline with scripted replies: two blind drafts of one
complex task in private copies, a winner by measured facts, the merge, and
the records that say whose work held up.
"""

import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadratus import contest
from quadratus.artifacts import ArtifactStore
from quadratus.integration import IntegrationGate, gate_in_copy
from quadratus.providers import ProviderError, ProviderRefusal, TurnLimitReached
from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits
from quadratus.session import CapabilityUnavailable, Session, SessionConfig, TaskSpec

# -- the decision, on measured facts alone ------------------------------------------------


def _draft(**facts):
    base = dict(completed=True, in_scope=True, changed_lines=10,
                gate=dict(status="passed", failing_commands=0, failed_cases=0))
    return dict(base, **facts)


def _failed(commands=1, cases=None):
    return dict(status="failed", failing_commands=commands, failed_cases=cases)


def test_the_first_rule_that_separates_two_drafts_decides():
    capped = _draft(completed=False)
    outside = _draft(in_scope=False)
    assert contest.decide_measured(_draft(), capped) == ("routed", "completed")
    assert contest.decide_measured(capped, _draft()) == ("challenger", "completed")
    assert contest.decide_measured(_draft(), outside) == ("routed", "scope")
    assert contest.decide_measured(outside, _draft()) == ("challenger", "scope")
    assert contest.decide_measured(_draft(), _draft(gate=_failed())) == ("routed", "gate")
    assert contest.decide_measured(_draft(gate=dict(status="not_run")), _draft(gate=_failed())) == ("challenger", "gate")
    # Both failed: fewer failing commands, then fewer failed cases where both are counted.
    assert contest.decide_measured(_draft(gate=_failed(2, 1)), _draft(gate=_failed(1, 9))) == ("challenger", "gate")
    assert contest.decide_measured(_draft(gate=_failed(1, 2)), _draft(gate=_failed(1, 5))) == ("routed", "gate")
    assert contest.decide_measured(_draft(gate=_failed(1, None)), _draft(gate=_failed(1, 5))) == (None, "")
    assert contest.decide_measured(_draft(), _draft()) == (None, "")


def test_a_draft_that_is_not_usable_never_wins():
    """Out of scope beats capped on rule 1 alone, and still cannot be merged."""
    assert contest.decide_measured(_draft(in_scope=False), _draft(completed=False)) == (None, "none usable")
    assert contest.decide_measured(_draft(in_scope=None), _draft(completed=False)) == (None, "none usable")


def test_a_tie_goes_to_the_judge_then_the_smaller_diff_then_the_routed_lead():
    small, large = _draft(changed_lines=4), _draft(changed_lines=40)
    assert contest.decide_tied(large, small, "routed") == ("routed", "judge")
    assert contest.decide_tied(large, small, None) == ("challenger", "size")
    assert contest.decide_tied(small, large, None) == ("routed", "size")
    assert contest.decide_tied(small, _draft(changed_lines=4), None) == ("routed", "default")
    assert contest.decide_tied(small, large, "nobody") == ("routed", "size")


def test_a_draft_that_changed_nothing_never_beats_real_work_on_the_gate_or_on_size():
    """A check that passes on unchanged source is the starting source's
    result, and no lines is not the smaller attempt. Between one draft that
    changed nothing and one that changed something, only a judge can pick
    the first; with no verdict the routed lead's draft stands, as it would
    have alone."""
    nothing = _draft(changed=[], changed_lines=0)
    real = _draft(changed=["calc.py"], changed_lines=12)
    failing = _draft(changed=["calc.py"], changed_lines=12, gate=_failed())
    for pair in ((failing, nothing), (nothing, failing), (real, nothing), (nothing, real)):
        assert contest.one_unchanged(*pair)
        assert contest.decide_measured(*pair) == (None, ""), "the gate does not separate them"
        assert contest.decide_tied(*pair, None) == ("routed", "default"), "nor does size"
        assert contest.decide_tied(*pair, "challenger") == ("challenger", "judge")
    # Two drafts that both changed something, or both changed nothing, are compared as before.
    assert not contest.one_unchanged(real, failing) and not contest.one_unchanged(nothing, dict(nothing))
    assert contest.decide_measured(failing, real) == ("challenger", "gate")
    assert contest.decide_tied(real, _draft(changed=["calc.py"], changed_lines=3), None) == ("challenger", "size")
    # A draft whose changed files were never measured is not read as unchanged.
    assert not contest.one_unchanged(_draft(), real)
    stated = contest.judge_prompt(packet="## Role packet", task_text="t", draft_a="\nA", draft_b="\nB", unchanged=True)
    assert "One of them changed no file" in stated and "measured the same on the project's checks" not in stated
    usual = contest.judge_prompt(packet="## Role packet", task_text="t", draft_a="\nA", draft_b="\nB")
    assert "measured the same on the project's checks" in usual and "changed no file" not in usual


def test_the_draft_order_comes_from_the_task_id_and_hides_the_routed_lead():
    seen = {contest.labels_for(f"t{i}")["routed"] for i in range(1, 40)}
    assert seen == {"A", "B"}, "the routed lead is not always Draft A"
    assert contest.labels_for("t1") == contest.labels_for("t1")
    for labels in map(contest.labels_for, ("t1", "t2", "t3")):
        assert sorted(labels.values()) == ["A", "B"]


# -- the judge's reply ---------------------------------------------------------------------

@pytest.mark.parametrize("reply, verdict", [
    ("Draft B handles the empty list.\nWINNER: B", ("B", "Draft B handles the empty list.")),
    ("A is simpler.\n**WINNER: A**", ("A", "A is simpler.")),
    ("reason\n- `winner: a.`", ("A", "reason")),
    ("WINNER: A", ("A", "")),
])
def test_a_plain_closing_winner_line_is_a_verdict(reply, verdict):
    assert contest.read_winner(reply) == verdict


@pytest.mark.parametrize("reply", [
    "",
    "Both are fine.",
    "WINNER: A\nbut B is close",                    # not the closing line
    "x\nWINNER: A or B",
    "x\nWINNER: both",
    "x\nWINNER: A if the tests pass",
    "WINNER: A\nx\nWINNER: B",
    "WINNER: A\nWINNER: A",
    "x\nWINNER: C",
    "x\nWINNER:",
    "x\nThe WINNER: A",                            # the marker does not open the line
])
def test_anything_else_is_no_verdict(reply):
    assert contest.read_winner(reply) is None


def test_the_judge_prompt_states_the_rule_its_reply_is_held_to():
    prompt = contest.judge_prompt(packet="## Role packet\nScope: a.py", task_text="Write a.py",
                                  draft_a="\nDIFF-A", draft_b="\nDIFF-B")
    assert contest.JUDGE_RULE in prompt
    for said in ("'WINNER: A' or 'WINNER: B'", "one line of reason", "is no verdict",
                 "no second round", "with two WINNER lines"):
        assert said in contest.JUDGE_RULE, said
    assert prompt.index("## Draft A") < prompt.index("DIFF-A") < prompt.index("## Draft B") < prompt.index("DIFF-B")
    assert "routed" not in prompt and "challenger" not in prompt


# -- gate facts ----------------------------------------------------------------------------

def _receipt(status, report=None, required=True, rid="check"):
    return SimpleNamespace(id=rid, status=status, reason=status, required=required, report=report)


def _parsed(failed, errors=0):
    return dict(state="parsed", counts=dict(passed=1, failed=failed, skipped=0, errors=errors))


def test_gate_facts_separate_a_failure_from_a_run_that_reached_no_verdict():
    passed = SimpleNamespace(passed=True, receipts=(_receipt("passed"),))
    assert contest.gate_facts(passed)["status"] == "passed"
    failed = SimpleNamespace(passed=False, receipts=(_receipt("failed", _parsed(2, 1)), _receipt("passed", rid="b")))
    assert contest.gate_facts(failed) == dict(status="failed", failing_commands=1, failed_cases=3,
                                              reason="check: failed: failed")
    uncounted = SimpleNamespace(passed=False, receipts=(_receipt("failed"),))
    assert contest.gate_facts(uncounted)["failed_cases"] is None
    for status in ("error", "blocked"):
        hung = SimpleNamespace(passed=False, receipts=(_receipt(status),))
        assert contest.gate_facts(hung)["status"] == "not_run", status
    optional = SimpleNamespace(passed=False, receipts=(_receipt("failed", required=False), _receipt("error", rid="b")))
    assert contest.gate_facts(optional)["status"] == "not_run"
    assert contest.gate_facts(None, reason="the run has no check")["reason"] == "the run has no check"
    single = SimpleNamespace(passed=False, receipts=(), returncode=1, report=_parsed(4), output="boom")
    assert contest.gate_facts(single) == dict(status="failed", failing_commands=1, failed_cases=4, reason="")
    timed_out = SimpleNamespace(passed=False, receipts=(), returncode=None, report=None, output="(timed out)")
    assert contest.gate_facts(timed_out)["status"] == "not_run"


# -- the scoreboard file and its reader ----------------------------------------------------

def _contest_record(**changes):
    record = dict(task="t1", kind="backend", difficulty="complex", formed=True, winner="B",
                  winner_model="openai:gpt-5.6-sol", decided_by="gate", judge=None,
                  drafts=[dict(label="A", role="routed", model="claude:opus", completed=True, in_scope=True,
                               gate=dict(status="failed"), changed_lines=9, reported_tokens=None, seconds=1.0),
                          dict(label="B", role="challenger", model="openai:gpt-5.6-sol", completed=True,
                               in_scope=True, gate=dict(status="passed"), changed_lines=12,
                               reported_tokens=None, seconds=1.0)])
    return dict(record, **changes)


def _review_record(**changes):
    return dict(dict(task="t1", model="grok:default", role="collaborator", complete=True, blocking_raised=2,
                     blocking_resolved=2, blocking_unresolved=0, recheck="resolved"), **changes)


def test_the_scoreboard_gets_one_private_line_per_contest_and_review_and_only_grows(tmp_path):
    board = tmp_path / "scoreboard.jsonl"
    skipped = dict(task="t2", formed=False, reason="no second vendor")
    rows = contest.scoreboard_rows("run-1", [_contest_record(), skipped], [_review_record()])
    assert [row["type"] for row in rows] == ["contest", "review"], "a contest that did not form is not a line"
    assert all(row["run"] == "run-1" for row in rows)
    assert rows[0]["drafts"][0]["gate"] == "failed" and rows[0]["winner_model"] == "openai:gpt-5.6-sol"
    assert contest.append_scoreboard(board, rows) == 2
    assert stat.S_IMODE(os.stat(board).st_mode) == 0o600
    first = board.read_text()
    assert contest.append_scoreboard(board, contest.scoreboard_rows("run-2", [_contest_record()], [])) == 1
    assert board.read_text().startswith(first) and len(board.read_text().splitlines()) == 3
    assert contest.append_scoreboard(board, []) == 0
    link = tmp_path / "link.jsonl"
    link.symlink_to(board)
    with pytest.raises(OSError):
        contest.append_scoreboard(link, rows)


def test_nothing_in_the_engine_reads_the_scoreboard():
    """A record only: the file is named where it is written and nowhere else,
    and the module that writes it has no function that reads it."""
    root = Path(contest.__file__).parent
    named = sorted(path.name for path in root.rglob("*.py")
                   if "scoreboard.jsonl" in path.read_text() or "scoreboard_rows" in path.read_text())
    # project_watch.py names the file only to watch it: an editing call that
    # writes the scoreboard fails its task. It never opens it.
    assert named == ["contest.py", "project_run.py", "project_watch.py"]
    assert sorted(name for name in contest.__all__ if "scoreboard" in name) == ["append_scoreboard",
                                                                               "scoreboard_rows"]
    source = (root / "project_run.py").read_text()
    assert source.count("'scoreboard.jsonl'") == 1 and "append_scoreboard(state / 'scoreboard.jsonl'" in source


def test_the_scoreboard_tool_counts_wins_losses_and_findings_and_skips_what_it_cannot_read(tmp_path, capsys):
    from tools import scoreboard as tool
    board = tmp_path / "scoreboard.jsonl"
    rows = contest.scoreboard_rows("run-1", [_contest_record(),
                                             _contest_record(task="t2", winner="A", winner_model="claude:opus",
                                                             decided_by="judge"),
                                             _contest_record(task="t3", winner=None, winner_model=None,
                                                             decided_by="none usable")],
                                   [_review_record(),
                                    _review_record(blocking_raised=1, blocking_resolved=0, blocking_unresolved=1,
                                                   recheck="unresolved"),
                                    _review_record(complete=False, blocking_raised=3, blocking_resolved=0,
                                                   recheck="capped")])
    contest.append_scoreboard(board, rows)
    with open(board, "a") as handle:
        handle.write("not json\n[1, 2]\n" + json.dumps(dict(type="other")) + "\n\n"
                     + json.dumps(dict(type="contest", drafts=[])) + "\n")
    found, skipped = tool.read([board, tmp_path / "absent.jsonl"])
    assert len(found) == 7 and skipped == dict(missing_files=1, malformed_lines=2, unknown_lines=1)
    summary = tool.summarise(found)
    opus, sol = summary["models"]["claude:opus"], summary["models"]["openai:gpt-5.6-sol"]
    assert (opus["contests"], opus["won"], opus["lost"], opus["no_winner"]) == (3, 1, 1, 1)
    assert opus["won_by"]["judge"] == 1 and opus["lost_by"]["gate"] == 1
    assert (sol["won"], sol["lost"]) == (1, 1) and sol["won_by"]["gate"] == 1
    grok = summary["reviewers"]["grok:default"]
    assert grok == dict(reviews=3, complete=2, blocking_raised=6, blocking_resolved=2,
                        blocking_unresolved=1, not_rechecked=3)
    assert summary["malformed_rows"] == 1
    assert tool.main([str(board)]) == 0
    shown = capsys.readouterr().out
    assert "claude:opus: 3 entered, 1 won (judge 1), 1 lost (gate 1), 1 with no usable draft" in shown
    assert "grok:default: 3 reviews, 2 with a complete verdict, 6 blocking raised" in shown
    assert "Skipped:" in shown and "2 malformed lines" in shown
    assert tool.main([str(tmp_path / "absent.jsonl")]) == 1
    # A project folder is read as its own scoreboard.
    (tmp_path / "project" / ".quadratus").mkdir(parents=True)
    (tmp_path / "project" / ".quadratus" / "scoreboard.jsonl").write_text(board.read_text())
    assert len(tool.read([tmp_path / "project"])[0]) == 7


def test_the_scoreboard_tool_imports_nothing_from_the_engine(tmp_path):
    source = Path("tools/scoreboard.py").resolve()
    assert "quadratus" not in {line.split()[1].split(".")[0] for line in source.read_text().splitlines()
                               if line.startswith(("import ", "from "))}
    # Isolated mode: no project folder on the path, so an engine import would fail.
    done = subprocess.run([sys.executable, "-I", str(source), "--json", str(tmp_path / "none.jsonl")],
                          capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert done.returncode == 1 and json.loads(done.stdout)["rows"] == 0


# -- a contest inside a session, offline ---------------------------------------------------

OPUS, SOL, GROK = "claude:opus", "openai:gpt-5.6-sol", "grok:default"
START = "def add(a, b):\n    return a + b\n"
#: The project's own check: fails while calc.py holds the word BROKEN.
CHECK = "import pathlib\nimport sys\nsys.exit(1 if 'BROKEN' in pathlib.Path('calc.py').read_text() else 0)\n"


def _task(label="backend complex", paths=("calc.py",), text="Add sub(a, b) to calc.py"):
    scope = json.dumps({"permitted_paths": list(paths), "intended_result": "calc.py has sub",
                        "acceptance": ["sub(3, 1) == 2"], "max_lines": 40})
    return f"KIND: {label}\nSCOPE: {scope}\n{text}"


def _sub(body="    return a - b", *, extra=None, reply='Added sub.\nCHANGED: ["calc.py"]'):
    """A scripted lead: writes calc.py (and any extra files) where its call runs."""
    def act(root):
        (root / "calc.py").write_text(START + f"\n\ndef sub(a, b):\n{body}\n")
        for name, text in (extra or {}).items():
            (root / name).write_text(text)
        return reply
    return act


def _capped(root):
    (root / "calc.py").write_text(START + "\n\n# half a thought\n")
    raise TurnLimitReached("turn limit", partial_text="still reading", turns=20)


def _raises(exc):
    def act(root):
        raise exc
    return act


class Script:
    """Scripted orchestrator, leads, reviewers and judge. Every call is logged
    with the folder it ran in: the project, or a private copy."""

    def __init__(self, plan, drafts, *, reviews=None, judge="Both do the task.\nWINNER: A",
                 revision="Revised.\nCHANGED: []", recheck="RESOLVED"):
        self.plan, self.drafts, self.reviews = list(plan), dict(drafts), dict(reviews or {})
        self.judge, self.revision, self.recheck = judge, revision, recheck
        self.calls, self.prompts, self.roots = [], [], []
        self.lock = threading.RLock()
        self.project = None

    @staticmethod
    def kind(prompt):
        for marker, kind in (("Name the single next task", "orchestrator"), ("The task is finished", "closeout"),
                             ("## Draft A", "judge"), ("You are leading", "lead"), ("Revise your work", "revision"),
                             ("Check only your BLOCKING findings", "recheck")):
            if marker in prompt:
                return kind
        return "review"

    def invoke_for(self, root):
        root = Path(root).resolve()

        def invoke(model, prompt, system=None, allow_writes=False):
            kind = self.kind(prompt)
            with self.lock:
                self.calls.append((model, kind, "project" if root == self.project else "copy", bool(allow_writes)))
                self.prompts.append((model, kind, prompt))
            if kind == "orchestrator":
                return self.plan.pop(0) if self.plan else "DONE"
            if kind == "closeout":
                return "SUMMARY: done\nDECISIONS: none recorded\nDEAD ENDS: none"
            if kind == "judge":
                if isinstance(self.judge, BaseException):
                    raise self.judge
                return self.judge
            if kind == "lead":
                return self.act(model, prompt)(root)
            if kind == "revision":
                return self.revision
            if kind == "recheck":
                return self.recheck
            return self.reviews.get(model, "NO FINDINGS")
        return invoke

    def act(self, model, prompt):
        return self.drafts[model]

    def of(self, kind):
        return [call for call in self.calls if call[1] == kind]

    def prompt(self, kind, model=None):
        return next(p for m, k, p in self.prompts if k == kind and model in (None, m))


def _session(tmp_path, script, *, gate=True, available=None, **config):
    project = (tmp_path / "project").resolve()
    project.mkdir(parents=True, exist_ok=True)
    (project / "calc.py").write_text(START)
    (project / "check.py").write_text(CHECK)
    script.project = project

    def fork(root):
        script.roots.append(Path(root))
        return script.invoke_for(root), (lambda: None)

    conf = SessionConfig(
        project=project, allow_writes=True, fork=fork,
        integration_gate=IntegrationGate([sys.executable, "check.py"], cwd=project) if gate else None, **config)
    session = Session("Add sub to calc", ArtifactStore(tmp_path / "artifacts"), script.invoke_for(project),
                      config=conf, **({"available": available} if available else {}))
    return session, project


def _held(session):
    assert len(session.contests) == 1
    return session.contests[0]


def _by_role(record):
    return {draft["role"]: draft for draft in record["drafts"]}


def test_the_winner_by_gate_is_merged_and_the_task_carries_on_as_its_draft(tmp_path):
    """Both drafts are usable; only the challenger's passes the project's own
    check in its copy. Its files are merged, and the task's diff, gate, review
    and close-out all read the merged project."""
    script = Script([_task(), "DONE"], {OPUS: _sub("    return a - b  # BROKEN"), SOL: _sub(), GROK: _sub()})
    session, project = _session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = _held(session)
    drafts = _by_role(record)
    assert (drafts["routed"]["model"], drafts["challenger"]["model"]) == (OPUS, SOL)
    assert record["formed"] and record["decided_by"] == "gate" and record["winner_model"] == SOL
    assert record["winner"] == drafts["challenger"]["label"] == contest.labels_for("t1")["challenger"]
    assert drafts["routed"]["gate"]["status"] == "failed" and drafts["challenger"]["gate"]["status"] == "passed"
    assert all(d["completed"] and d["in_scope"] and d["usable"] for d in record["drafts"])
    assert record["judge"] is None and not script.of("judge"), "the measurements decided; nobody was asked"
    # Both drafts were made in private copies, at the same stage, and nowhere else.
    assert sorted(script.of("lead")) == [(OPUS, "lead", "copy", True), (SOL, "lead", "copy", True)]
    assert len(script.roots) == 2 and all(not root.exists() for root in script.roots)
    # The project holds the winner's work and nothing of the loser's.
    assert "BROKEN" not in (project / "calc.py").read_text() and "return a - b" in (project / "calc.py").read_text()
    assert record["merged"] == ["calc.py"]
    # The task's own records describe the merged work, once.
    assert session.completed and [s.author for s in session.history] == [SOL]
    outcome = session.task_outcomes[-1]
    assert outcome.lead == SOL and outcome.partial["changed"] == ["calc.py"]
    assert outcome.owner_changes == [{"from": OPUS, "to": SOL, "reason": "contest", "decided_by": "gate"}]
    assert outcome.contract["owner"] == OPUS, "the contract keeps its dispatched owner"
    assert session.scope_reports and all(r.changed == ["calc.py"] for r in session.scope_reports)
    assert session.scope_reports[-1].changed_lines == drafts["challenger"]["changed_lines"]
    assert session.checks[-1]["passed"] and session.checks[-1]["cwd"] == str(project)
    closeout = script.prompt("closeout")
    assert "+def sub(a, b):" in closeout and "BROKEN" not in closeout
    # The winner's reviewers are the other two seats, reading the merged project.
    assert sorted(script.of("review")) == [(OPUS, "review", "project", False), (GROK, "review", "project", False)]
    assert script.of("closeout") == [(SOL, "closeout", "project", False)]
    # The ledger entry names the contest, as the harness's record.
    assert "contest: two blind drafts" in session.memory.render(current="")
    assert f"{SOL}'s was merged, decided by gate" in session.memory.render(current="")
    # No revision round was drawn, so the losing draft was only recorded.
    assert record["loser_note_delivered"] is False and record["loser_diff_artifact"]
    assert "BROKEN" in session.store.get(record["loser_diff_artifact"])


def test_a_draft_outside_the_task_scope_loses_on_scope_and_none_of_it_is_merged(tmp_path):
    script = Script([_task(), "DONE"],
                    {OPUS: _sub(extra={"other.py": "x = 1\n"}, reply='Done.\nCHANGED: ["calc.py", "other.py"]'),
                     SOL: _sub(), GROK: _sub()})
    session, project = _session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = _held(session)
    drafts = _by_role(record)
    assert record["decided_by"] == "scope" and record["winner_model"] == SOL
    assert drafts["routed"]["completed"] is True and drafts["routed"]["in_scope"] is False
    assert drafts["routed"]["ended"] == "delivered, outside the task's scope"
    assert drafts["routed"]["gate"]["status"] == "not_run"
    assert not (project / "other.py").exists() and "def sub" in (project / "calc.py").read_text()
    assert session.completed and not session.open_findings


@pytest.mark.parametrize("capped, winner", [(OPUS, SOL), (SOL, OPUS)])
def test_a_capped_entrant_loses_on_completion_whichever_it_is(tmp_path, capped, winner):
    script = Script([_task(), "DONE"], {OPUS: _sub(), SOL: _sub(), GROK: _sub(), capped: _capped})
    session, project = _session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = _held(session)
    assert record["decided_by"] == "completed" and record["winner_model"] == winner
    loser = next(d for d in record["drafts"] if d["model"] == capped)
    assert loser["completed"] is False and loser["ended"] == "capped at the turn limit (20 rounds)"
    assert "half a thought" not in (project / "calc.py").read_text()
    assert session.completed and not session.turn_limited, "the task was not capped: one draft finished"
    assert [s.author for s in session.history] == [winner]


def test_equal_drafts_go_to_a_judge_from_the_third_vendor_who_is_shown_no_names(tmp_path):
    labels = contest.labels_for("t1")
    script = Script([_task(), "DONE"], {OPUS: _sub("    return a - b"), SOL: _sub("    return a + (-b)"), GROK: _sub()},
                    judge=f"The second is the plain form.\nWINNER: {labels['challenger']}")
    session, project = _session(tmp_path, script, compete="complex", lead_max_turns=12)
    session.run(max_tasks=3)
    record = _held(session)
    assert record["decided_by"] == "judge" and record["winner_model"] == SOL
    assert record["judge"] == dict(model=GROK, verdict=labels["challenger"],
                                   reason="The second is the plain form.", note="")
    assert all(d["gate"]["status"] == "passed" for d in record["drafts"])
    # One read-only call, in the project (which holds neither draft), by neither author.
    assert script.of("judge") == [(GROK, "judge", "project", False)]
    prompt = script.prompt("judge")
    assert contest.JUDGE_RULE in prompt and "Add sub(a, b) to calc.py" in prompt and "calc.py" in prompt
    shown = {label: prompt.index(f"## Draft {label}") for label in "AB"}
    routed_text, challenger_text = prompt.index("+    return a - b"), prompt.index("+    return a + (-b)")
    assert (routed_text < challenger_text) == (shown[labels["routed"]] < shown[labels["challenger"]])
    assert shown["A"] < min(routed_text, challenger_text) < shown["B"]
    for name in (OPUS, SOL, "Opus", "Sol", "routed", "challenger", "contest"):
        assert name not in prompt, name
    assert "at most 12 tool rounds" in prompt and "no verdict is recorded" in prompt
    assert "return a + (-b)" in (project / "calc.py").read_text()


@pytest.mark.parametrize("judge, note", [
    ("They are both fine.", "did not end with one plain WINNER line"),
    ("x\nWINNER: A if the tests pass", "did not end with one plain WINNER line"),
    (TurnLimitReached("cap", turns=12), "stopped at its turn cap (12 rounds)"),
    (ProviderError("judge is down"), "did not return (ProviderError: judge is down)"),
])
def test_a_judge_with_no_verdict_leaves_the_smaller_diff_to_decide(tmp_path, judge, note):
    longer = "    difference = a - b\n    return difference"
    script = Script([_task(), "DONE"], {OPUS: _sub(longer), SOL: _sub(), GROK: _sub()}, judge=judge)
    session, project = _session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = _held(session)
    assert record["decided_by"] == "size" and record["winner_model"] == SOL
    assert record["judge"]["model"] == GROK and record["judge"]["verdict"] is None
    assert note in record["judge"]["note"]
    assert len(script.of("judge")) == 1, "asked once; no second round"
    assert "difference" not in (project / "calc.py").read_text() and session.completed


def test_identical_size_and_no_verdict_keeps_the_routed_lead(tmp_path):
    script = Script([_task(), "DONE"], {OPUS: _sub("    return a - b"), SOL: _sub("    return b - a"), GROK: _sub()},
                    judge="no idea")
    session, project = _session(tmp_path, script, compete="complex", gate=False)
    session.run(max_tasks=3)
    record = _held(session)
    assert record["decided_by"] == "default" and record["winner_model"] == OPUS
    assert all(d["gate"] == dict(status="not_run", failing_commands=None, failed_cases=None,
                                 reason="the task has no check") for d in record["drafts"])
    assert "return a - b" in (project / "calc.py").read_text()
    assert session.task_outcomes[-1].owner_changes == []


def test_two_drafts_that_are_the_same_change_are_not_sent_to_a_judge(tmp_path):
    script = Script([_task(), "DONE"], {OPUS: _sub(), SOL: _sub(), GROK: _sub()})
    session, project = _session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = _held(session)
    assert record["decided_by"] == "default" and record["winner_model"] == OPUS
    assert record["judge"]["note"] == "not asked: the two drafts are the same change"
    assert not script.of("judge") and session.completed


def _blocked(root):
    return 'I could not find where sub belongs, so I changed nothing.\nCHANGED: []'


@pytest.mark.parametrize("judge, decided_by", [(None, "judge"), ("no idea", "default")])
def test_a_draft_that_did_nothing_does_not_win_because_unchanged_source_passes_the_check(tmp_path, judge, decided_by):
    """The routed lead's real draft fails the check in its copy; the
    challenger changed nothing, so its copy passes as the starting source
    does. That pass is not a measurement of an attempt: the judge is asked,
    told one attempt changed no file, and with no verdict the routed lead's
    draft stands. The task's own gate then judges it, as it would have."""
    labels = contest.labels_for("t1")
    script = Script([_task(), "DONE"], {OPUS: _sub("    return a - b  # BROKEN"), SOL: _blocked, GROK: _sub()},
                    judge=judge or f"Only one attempt adds sub.\nWINNER: {labels['routed']}")
    session, project = _session(tmp_path, script, compete="complex", max_gate_fixes=0)
    try:
        session.run(max_tasks=3)
    except Exception:  # noqa: BLE001 -- the merged draft fails the task's own check, as it would alone
        pass
    record = _held(session)
    drafts = _by_role(record)
    assert (drafts["routed"]["gate"]["status"], drafts["challenger"]["gate"]["status"]) == ("failed", "passed")
    assert drafts["challenger"]["changed"] == [] and drafts["challenger"]["usable"]
    assert record["winner_model"] == OPUS and record["decided_by"] == decided_by
    assert len(script.of("judge")) == 1 and "def sub" in (project / "calc.py").read_text()
    prompt = script.prompt("judge")
    assert "One of them changed no file" in prompt and "This attempt changed no file." in prompt
    assert "measured the same on the project's checks" not in prompt
    # No false pass: the check that counts is the task's own, on the merged project.
    assert session.checks and session.checks[-1]["passed"] is False and not session.completed


def test_with_no_second_vendor_no_contest_forms_and_the_record_says_why(tmp_path):
    script = Script([_task(), "DONE"], {OPUS: _sub()})
    session, project = _session(tmp_path, script, compete="complex",
                                available=lambda key: key.startswith("claude:"))
    session.run(max_tasks=3)
    record = _held(session)
    assert record["formed"] is False and "no brain-trust member from a second vendor" in record["reason"]
    assert "never formed with a stand-in" in record["reason"]
    assert script.of("lead") == [(OPUS, "lead", "project", True)] and not script.roots
    assert session.completed and "def sub" in (project / "calc.py").read_text()
    assert "contest" not in session.memory.render(current="")


def test_a_budget_that_cannot_cover_two_drafts_and_a_judge_forms_no_contest(tmp_path):
    asked = []
    script = Script([_task(), "DONE"], {OPUS: _sub(), SOL: _sub(), GROK: _sub()})
    session, project = _session(tmp_path, script, compete="complex",
                                budget_covers=lambda calls: asked.append(calls) or False)
    session.run(max_tasks=3)
    record = _held(session)
    assert asked == [3] and record["formed"] is False
    assert record["reason"] == "the run's remaining budget does not cover two drafts and a judge"
    assert script.of("lead") == [(OPUS, "lead", "project", True)] and not script.roots and session.completed


def test_the_run_budget_says_whether_it_covers_a_contest_without_reserving_anything():
    budget = RunBudget(RunLimits(max_calls=5, max_reported_tokens=1000, reserve_tokens_per_call=300))
    assert budget.covers(3) and not budget.covers(4), "4 reserves of 300 do not fit in 1000"
    assert budget.snapshot()["reserved_attempts"] == 0
    ticket, _ = budget.reserve()
    assert not budget.covers(3), "one call in flight holds its reserve"
    budget.finish(ticket, dict(input_tokens=10, output_tokens=10))
    assert budget.covers(3)
    calls = RunBudget(RunLimits(max_calls=2, max_reported_tokens=1000))
    assert calls.covers(2) and not calls.covers(3)
    spent = RunBudget(RunLimits(max_calls=9, max_reported_tokens=20))
    ticket, _ = spent.reserve()
    with pytest.raises(RunBudgetExceeded):
        spent.finish(ticket, dict(input_tokens=15, output_tokens=15))
    assert not spent.covers(1), "a latched stop covers nothing"


def _journey(tmp_path, name, plan, **config):
    script = Script(plan, {OPUS: _sub(), SOL: _sub(), GROK: _sub()},
                    reviews={SOL: "BLOCKING: name the operands\nREVIEW: COMPLETE"})
    session, project = _session(tmp_path / name, script, **config)
    session.run(max_tasks=3)
    root = str(tmp_path / name)
    return session, script, [(model, kind, prompt.replace(root, "<run>")) for model, kind, prompt in script.prompts]


def _records(session):
    return [o.to_dict() | {"source_before": 0, "source_after": 0} for o in session.task_outcomes]


def test_off_is_the_old_run_call_for_call_and_prompt_for_prompt(tmp_path):
    """The same scripted journey with the field unset and with it off: the
    same calls, the same prompts, the same records, and no contest anywhere."""
    unset, unset_script, unset_prompts = _journey(tmp_path, "unset", [_task(), "DONE"])
    off, off_script, off_prompts = _journey(tmp_path, "off", [_task(), "DONE"], compete="off")
    assert SessionConfig().compete == "off"
    assert unset_script.calls == off_script.calls and unset_prompts == off_prompts
    # The single-lead journey: one draft in the project, two reviews, one revision
    # answering the blocking one, its re-check, the close-out, and DONE.
    seat = off_script.calls[0][0]
    assert [(model, kind, where) for model, kind, where, _ in off_script.calls] == [
        (seat, "orchestrator", "project"), (OPUS, "lead", "project"),
        (SOL, "review", "project"), (GROK, "review", "project"), (OPUS, "revision", "project"),
        (SOL, "recheck", "project"), (OPUS, "closeout", "project"), (seat, "orchestrator", "project")]
    for session, script in ((unset, unset_script), (off, off_script)):
        assert session.contests == [] and not script.roots and not script.of("judge")
        assert all("Another attempt" not in prompt and "contest" not in prompt for _, _, prompt in script.prompts)
        assert "contest" not in session.memory.render(current="")
    assert _records(unset) == _records(off) and unset.reviewer_records == off.reviewer_records


def test_an_entrant_is_shown_what_a_single_lead_is_shown_and_nothing_of_a_contest(tmp_path):
    """The routed entrant's prompt in its copy is the single lead's prompt in
    the project, word for word; the challenger's differs only in whom it may
    consult. Neither is told a contest exists."""
    off, off_script, _ = _journey(tmp_path, "off", [_task(), "DONE"])
    on, on_script, _ = _journey(tmp_path, "on", [_task(), "DONE"], compete="complex")
    assert _held(on)["formed"]

    def lead_prompt(script, model, root):
        return script.prompt("lead", model).replace(str(root), "<run>")
    single = lead_prompt(off_script, OPUS, tmp_path / "off")
    assert lead_prompt(on_script, OPUS, tmp_path / "on") == single
    challenger = lead_prompt(on_script, SOL, tmp_path / "on")
    assert [line for line in challenger.splitlines() if line not in single.splitlines()] == [
        line for line in challenger.splitlines() if "Members you may consult" in line]
    for prompt in (single, challenger):
        for word in ("contest", "Another attempt", "another attempt", "Draft A", "WINNER", "challenger"):
            assert word not in prompt, word


def test_the_delivery_reading_agrees_with_the_drafting_loop():
    """A contest entrant's stopped reply counts as a delivery by the same
    reading the drafting loop applies: a request is never a draft."""
    from quadratus.session import _delivers
    trust = [OPUS, SOL, GROK]
    assert _delivers('Added sub.\nCHANGED: ["calc.py"]', trust)
    for request in ("FETCH: 0123456789ab", 'WORKER {"errand": "code", "instruction": "x"}',
                    "CONSULT Sol: is this right?", "ASK: which file?",
                    'One line first.\nWORKER {"errand": "code", "instruction": "x"}'):
        assert not _delivers(request, trust), request


def test_complex_mode_leaves_a_task_below_complex_exactly_as_off_does(tmp_path):
    plan = [_task("backend standard"), "DONE"]
    off, off_script, off_prompts = _journey(tmp_path, "off", plan, compete="off", spread_leads=False)
    on, on_script, on_prompts = _journey(tmp_path, "on", plan, compete="complex", spread_leads=False)
    assert off_script.calls == on_script.calls and off_prompts == on_prompts
    assert on.contests == [] and not on_script.roots, "no contest was considered, so nothing is recorded"


def test_the_losing_draft_reaches_the_winner_once_in_its_revision_unnamed_and_no_reviewer(tmp_path):
    script = Script([_task(), "DONE"], {OPUS: _sub("    return a - b  # BROKEN"), SOL: _sub(), GROK: _sub()},
                    reviews={GROK: "BLOCKING: name the operands\nREVIEW: COMPLETE"})
    session, project = _session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = _held(session)
    assert record["winner_model"] == SOL and record["loser_note_delivered"] is True
    assert script.of("revision") == [(SOL, "revision", "project", True)]
    revision = script.prompt("revision")
    assert "[Another attempt]" in revision and "+    return a - b  # BROKEN" in revision
    assert "Take from it only what is better" in revision and "raises no finding" in revision
    note = revision[revision.index("[Another attempt]"):]
    for name in (OPUS, "Opus", "contest", "loser", "routed"):
        assert name not in note, name
    assert "[Reviewer" in revision and "name the operands" in revision
    # The reviewers and the re-check read the merged project and are shown no other attempt.
    for model, seen, prompt in script.prompts:
        if seen in ("review", "recheck", "closeout"):
            assert "Another attempt" not in prompt and "BROKEN" not in prompt, (seen, model)
    assert session.completed


def test_the_winners_not_run_lines_come_over_and_the_losers_do_not(tmp_path):
    """What the winning draft said it did not run is acceptance still owed
    for the merged work, exactly as a single lead's would be. The losing
    draft's lines describe work the project does not hold."""
    said = 'Added sub.\nNOT RUN: tests/test_{who}.py - no pytest in this folder\nCHANGED: ["calc.py"]'
    script = Script([_task(), "DONE"],
                    {OPUS: _sub("    return a - b  # BROKEN", reply=said.format(who="loser")),
                     SOL: _sub(reply=said.format(who="winner")), GROK: _sub()})
    session, project = _session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    assert _held(session)["winner_model"] == SOL
    owed = [(e["task"], e["author"], e["role"], e["item"]) for e in session.unexecuted_acceptance]
    assert owed == [("t1", SOL, "lead", "tests/test_winner.py")]
    assert session.unexecuted_acceptance[0]["after_check"] == 0, "re-based: only a later check can discharge it"


def test_a_winning_draft_that_changed_no_file_still_brings_its_not_run_lines(tmp_path):
    """Both drafts found the work already there and changed nothing, and the
    routed lead said its test did not run. A single lead's NOT RUN line is
    recorded whether or not it changed a file; dropped here, the audit could
    mark met a requirement the lead itself said was not run."""
    def nothing(root):
        return 'calc.py already has what is asked.\nNOT RUN: tests/test_calc.py - no pytest here\nCHANGED: []'
    script = Script([_task(), "DONE"], {OPUS: nothing, SOL: nothing, GROK: _sub()})
    session, project = _session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = _held(session)
    assert record["winner_model"] == OPUS and record["merged"] == [] and record["decided_by"] == "default"
    assert [(e["task"], e["author"], e["item"]) for e in session.unexecuted_acceptance] == [
        ("t1", OPUS, "tests/test_calc.py")]
    # The ledger's line says what happened: nothing was merged.
    line = contest.ledger_line(record)
    assert f"{OPUS}'s was taken and changed no file, decided by default" in line and "merged" not in line


@pytest.mark.parametrize("stop, kind", [(ProviderError("entrant is down"), "transport"),
                                        (ProviderRefusal("declined by policy"), "refusal")])
def test_an_entrant_that_raises_does_not_lose_the_other_draft_or_stop_the_run(tmp_path, stop, kind):
    """A transport error is that draft's own result, as it is a parallel
    child's: the other draft wins and the run goes on. A refusal is too when
    it is the challenger's. The routed lead's refusal stops the run, as it
    does with one lead (integrator's ruling; tests/test_review1110_glue2.py)."""
    refused = isinstance(stop, ProviderRefusal)
    for broken, winner in ((SOL, OPUS),) if refused else ((SOL, OPUS), (OPUS, SOL)):
        script = Script([_task(), "DONE"], {OPUS: _sub(), SOL: _sub(), GROK: _sub(), broken: _raises(stop)})
        session, project = _session(tmp_path / broken.replace(":", "-"), script, compete="complex")
        session.run(max_tasks=3)
        record = _held(session)
        assert record["decided_by"] == "completed" and record["winner_model"] == winner
        lost = next(d for d in record["drafts"] if d["model"] == broken)
        assert lost["ended"] == f"stopped: {type(stop).__name__}" and lost["completed"] is False
        assert lost["error"] == f"{type(stop).__name__}: {stop}", "the record keeps what ended the draft"
        assert next(d for d in record["drafts"] if d["model"] == winner)["error"] == ""
        assert "def sub" in (project / "calc.py").read_text() and session.completed
    if refused:
        return
    # The routed lead's own stop is on the task's record, as recovered history.
    facts = [fact for fact in session.task_outcomes[-1].facts if "in its contest copy" in fact.detail]
    assert len(facts) == 1 and facts[0].recovered and str(stop) in facts[0].detail
    assert facts[0].kind == kind


@pytest.mark.parametrize("stop, broken, kept", [
    (RuntimeError("entrant exploded"), SOL, OPUS),
    (RuntimeError("entrant exploded"), OPUS, SOL),
    (CapabilityUnavailable("denied a command the harness requires"), SOL, OPUS),
])
def test_a_run_level_stop_in_an_entrant_stops_the_run_with_the_other_draft_kept(tmp_path, stop, broken, kept):
    """A denied capability, or a failure no outcome class names, is the run's
    stop whoever met it, by the rule a parallel child's stop is read with:
    never absorbed as one draft losing. The usable draft is merged first, so
    it is not lost with the copy. One case is stricter and is held in
    tests/test_review1110_glue2.py: the routed lead's own denied capability,
    where no draft stands in for its own (integrator's ruling)."""
    script = Script([_task(), "DONE"], {OPUS: _sub(), SOL: _sub(), GROK: _sub(), broken: _raises(stop)})
    session, project = _session(tmp_path, script, compete="complex")
    with pytest.raises(type(stop), match=str(stop)):
        session.run(max_tasks=3)
    record = _held(session)
    assert record["winner_model"] == kept and record["decided_by"] == "completed"
    assert record["stopped"] == f"{type(stop).__name__}: {stop}"
    lost = next(d for d in record["drafts"] if d["model"] == broken)
    assert lost["ended"] == f"stopped: {type(stop).__name__}" and lost["error"] == record["stopped"]
    assert "def sub" in (project / "calc.py").read_text(), "the finished draft is in the project, not lost"
    assert not script.of("review") and not script.of("closeout"), "no further model work after the stop"
    assert not session.completed and session.in_flight["changed"] == ["calc.py"]
    outcome = session.task_outcomes[-1]
    assert outcome.closed_as == f"stopped:{type(stop).__name__}"
    assert not any(fact.recovered for fact in outcome.facts), "a stop the run ends on is not recorded as recovered"


def test_a_contest_and_a_parallel_batch_read_a_stop_in_a_copy_by_one_rule():
    from quadratus.session import PartialWorkStopped, RunStalled, TaskFailed, _stops_the_run
    for own in (TurnLimitReached("cap"), ProviderError("down"), ProviderRefusal("no"), TaskFailed("scope", "x"),
                PartialWorkStopped("kept"), RunStalled("stalled")):
        assert not _stops_the_run(own), own
    for run in (RunBudgetExceeded("spent"), KeyboardInterrupt(), CapabilityUnavailable("denied"),
                RuntimeError("unknown"), OSError("unknown")):
        assert _stops_the_run(run), run
    import inspect
    assert "_stops_the_run(exc)" in inspect.getsource(Session._run_batch)
    assert "_stops_the_run(exc)" in inspect.getsource(Session._contest_draft)
    assert "_stops_the_run(exc)" in inspect.getsource(Session._ask_judge)


def test_an_unknown_failure_in_the_judge_stops_the_run_after_the_tie_is_settled_without_it(tmp_path):
    longer = "    difference = a - b\n    return difference"
    script = Script([_task(), "DONE"], {OPUS: _sub(longer), SOL: _sub(), GROK: _sub()},
                    judge=RuntimeError("judge exploded"))
    session, project = _session(tmp_path, script, compete="complex")
    with pytest.raises(RuntimeError, match="judge exploded"):
        session.run(max_tasks=3)
    record = _held(session)
    assert record["decided_by"] == "size" and record["winner_model"] == SOL
    assert record["stopped"] == "RuntimeError: judge exploded" and record["judge"] is None
    assert "difference" not in (project / "calc.py").read_text() and "def sub" in (project / "calc.py").read_text()
    assert not script.of("review") and not session.completed


def test_a_budget_stop_in_one_entrant_still_stops_the_run_with_the_finished_draft_kept(tmp_path):
    script = Script([_task(), "DONE"], {OPUS: _sub(), SOL: _raises(RunBudgetExceeded("Run stopped: call_limit")),
                                        GROK: _sub()})
    session, project = _session(tmp_path, script, compete="complex")
    with pytest.raises(RunBudgetExceeded):
        session.run(max_tasks=3)
    record = _held(session)
    assert record["winner_model"] == OPUS and record["stopped"].startswith("RunBudgetExceeded")
    assert "def sub" in (project / "calc.py").read_text(), "the finished draft is in the project, not lost"
    assert not script.of("judge") and not script.of("review"), "no further call after the stop"
    assert session.in_flight["changed"] == ["calc.py"]


def test_when_neither_draft_is_usable_the_routed_leads_own_result_stands(tmp_path):
    """Both capped: the task is the capped task it would have been, with the
    routed lead's partial work kept in the project and the other's left out."""
    script = Script([_task(), "DONE"], {OPUS: _capped, SOL: _raises(TurnLimitReached("cap", turns=3)), GROK: _sub()})
    session, project = _session(tmp_path, script, compete="complex", max_turn_limited_in_a_row=0)
    session.run(max_tasks=3)
    record = _held(session)
    assert record["winner"] is None and record["winner_model"] is None and record["decided_by"] == "none usable"
    assert record["merged"] == ["calc.py"] and "half a thought" in (project / "calc.py").read_text()
    assert session.turn_limited == ["t1"] and session.history[-1].outcome == "turn_limited"
    assert session.history[-1].author == OPUS and not session.completed
    assert session.stop_reason.startswith("TurnLimitBreaker")
    assert "neither was usable" in session.memory.render(current="")


def test_an_unusable_routed_draft_outside_its_paths_is_kept_as_an_artifact_never_merged(tmp_path):
    script = Script([_task(), "DONE"],
                    {OPUS: _sub(extra={"other.py": "x = 1\n"}), SOL: _capped, GROK: _sub()})
    session, project = _session(tmp_path, script, compete="complex", max_turn_limited_in_a_row=0)
    session.run(max_tasks=3)
    record = _held(session)
    assert record["decided_by"] == "none usable" and record["merged"] == []
    assert (project / "calc.py").read_text() == START and not (project / "other.py").exists()
    assert session.failed == ["t1"] and session.failed_records["t1"]["cause"] == "scope"
    assert session.failed_records["t1"]["changed"] == [], "the project was not changed, and the record says so"
    kept = json.loads(session.store.get(record["unmerged_artifact"]))
    assert sorted(kept) == ["calc.py", "other.py"] and "def sub" in kept["calc.py"]
    assert f"kept in artifact {record['unmerged_artifact']}" in session.history[-1].summary
    assert not any("outside its declared scope" in text for text in session.open_findings)


def test_each_task_that_is_not_contested_records_its_reason(tmp_path):
    script = Script([], {})
    session, project = _session(tmp_path, script, compete="complex")
    spec = TaskSpec("t9", "Add sub", complexity="complex", kind="backend")
    assert session._no_contest_reason(spec) == ""
    review = TaskSpec("t9", "Read calc.py", complexity="complex", kind="review")
    assert "review or audit" in session._no_contest_reason(review)
    session._current_resolves = ["F1"]
    assert "RESOLVES" in session._no_contest_reason(spec)
    session._current_resolves = []
    pinned = TaskSpec("t9", "Add sub", complexity="complex", kind="backend", lead=OPUS)
    assert "named by the caller" in session._no_contest_reason(pinned)
    session.config.fork = None
    assert "could not be isolated in a copy" in session._no_contest_reason(spec)
    session.config.allow_writes = False
    assert "no project with a write grant" in session._no_contest_reason(spec)
    # Security work never reaches the contest: it is recorded where it branches off.
    session._run_security_task = lambda spec: "excursion"
    assert session._run_task(TaskSpec("t9", "Rotate the key", complexity="complex", kind="security")) == "excursion"
    assert session.contests[-1] == dict(task="t9", kind="security", difficulty="complex", formed=False,
                                        reason="security work runs inside its own bounded excursion")
    # Below complex, or with contests off, nothing is recorded at all.
    session._skip_contest(TaskSpec("t9", "x", complexity="standard"), "never")
    assert len(session.contests) == 1


def test_a_direct_tier_task_is_not_contested(tmp_path):
    script = Script([_task() + "\nTIER: direct", "DONE"], {OPUS: _sub(), SOL: _sub(), GROK: _sub()})
    session, project = _session(tmp_path, script, compete="complex", direct_tier=True)
    session.run(max_tasks=3)
    record = _held(session)
    assert session.task_outcomes[-1].contract["required"]["tier"] == "direct"
    assert record["formed"] is False and record["reason"] == "a direct-tier task is one small pass"
    assert script.of("lead") == [(OPUS, "lead", "project", True)] and not script.roots


def test_tasks_inside_a_parallel_batch_are_not_contested(tmp_path):
    class Batch(Script):
        def act(self, model, prompt):
            name = "a.py" if "Write a.py" in prompt.rsplit("verbatim", 1)[-1] else "b.py"

            def write(root):
                (root / name).write_text("x = 1\n")
                return f'Wrote it.\nCHANGED: ["{name}"]'
            return write

    batch = ("PARALLEL\n" + _task(paths=("a.py",), text="Write a.py") + "\n---\n"
             + _task(paths=("b.py",), text="Write b.py"))
    script = Batch([batch, "DONE"], {})
    session, project = _session(tmp_path, script, compete="complex", gate=False, max_parallel_tasks=2)
    session.run(max_tasks=4)
    assert [(r["task"], r["formed"], r["reason"]) for r in session.contests] == [
        ("t1", False, "a task inside a parallel batch is not contested"),
        ("t2", False, "a task inside a parallel batch is not contested")]
    assert len(script.of("lead")) == 2 and len(script.roots) == 2, "one draft per task, one copy per task"
    assert session.parallel_batches and (project / "a.py").exists() and (project / "b.py").exists()


# -- the gate on a copy --------------------------------------------------------------------

def test_the_tasks_gate_runs_on_a_copy_with_the_projects_dependencies_linked_for_the_run(tmp_path):
    project, copy = tmp_path / "project", tmp_path / "copy"
    for root in (project, copy):
        root.mkdir()
        (root / "check.py").write_text(
            "import pathlib\nimport sys\nsys.path.insert(0, 'node_modules')\nimport helper\n"
            "sys.exit(0 if helper.OK and pathlib.Path('here.txt').read_text() == sys.argv[1] else 1)\n")
    (project / "here.txt").write_text("project")
    (copy / "here.txt").write_text("copy")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "helper.py").write_text("OK = True\n")
    gate = IntegrationGate([sys.executable, str(project / "check.py"), "copy"], cwd=project)
    with gate_in_copy(gate, project, copy) as bound:
        assert (copy / "node_modules").is_symlink()
        assert str(copy.resolve() / "check.py") in bound.commands[0].argv, "a path naming the project names the copy"
        result = bound.run()
    assert result.passed, result.output
    assert not (copy / "node_modules").exists() and (project / "node_modules" / "helper.py").exists()
    assert contest.gate_facts(result)["status"] == "passed"
    with gate_in_copy(object(), project, copy) as unknown:
        assert unknown is None, "a gate whose commands cannot be read is not run in a copy"


# -- reviewer records ----------------------------------------------------------------------

def test_each_review_is_recorded_with_what_it_raised_and_what_the_recheck_said(tmp_path):
    script = Script([_task(), "DONE"], {OPUS: _sub(), SOL: _sub(), GROK: _sub()},
                    reviews={SOL: "BLOCKING: name the operands\nBLOCKING: none\nBLOCKING: add a test\nREVIEW: COMPLETE",
                             GROK: "I looked at some of it."}, recheck="UNRESOLVED: the test is still missing")
    session, project = _session(tmp_path, script, max_fix_cycles=1)
    session.run(max_tasks=3)
    by_model = {record["model"]: record for record in session.reviewer_records}
    assert by_model[SOL] == dict(task="t1", model=SOL, role="collaborator", complete=True, capped=False,
                                 blocking_raised=2, blocking_resolved=0, blocking_unresolved=2,
                                 recheck="unresolved", recheck_rounds=1)
    assert by_model[GROK]["complete"] is False and by_model[GROK]["blocking_raised"] == 0
    assert by_model[GROK]["recheck"] is None
    assert len(script.of("review")) == 2 and len(script.of("recheck")) == 1, "read from the task's own calls"
    resolved = Script([_task(), "DONE"], {OPUS: _sub(), SOL: _sub(), GROK: _sub()},
                      reviews={SOL: "BLOCKING: name the operands\nREVIEW: COMPLETE"})
    session, project = _session(tmp_path / "second", resolved)
    session.run(max_tasks=3)
    record = next(r for r in session.reviewer_records if r["model"] == SOL)
    assert (record["blocking_raised"], record["blocking_resolved"], record["recheck"]) == (1, 1, "resolved")


def test_a_capped_review_and_a_capped_recheck_are_recorded_as_no_verdict(tmp_path):
    class Capping(Script):
        def invoke_for(self, root):
            inner = super().invoke_for(root)

            def invoke(model, prompt, system=None, allow_writes=False):
                kind = self.kind(prompt)
                if (kind, model) in (("review", GROK), ("recheck", SOL)):
                    inner(model, prompt, system=system, allow_writes=allow_writes)
                    raise TurnLimitReached("cap", partial_text="BLOCKING: I was about to", turns=20)
                return inner(model, prompt, system=system, allow_writes=allow_writes)
            return invoke

    script = Capping([_task(), "DONE"], {OPUS: _sub(), SOL: _sub(), GROK: _sub()},
                     reviews={SOL: "BLOCKING: name the operands\nREVIEW: COMPLETE"})
    session, project = _session(tmp_path, script)
    session.run(max_tasks=3)
    by_model = {record["model"]: record for record in session.reviewer_records}
    assert by_model[GROK]["capped"] and not by_model[GROK]["complete"] and by_model[GROK]["blocking_raised"] == 0
    assert (by_model[SOL]["recheck"], by_model[SOL]["blocking_resolved"], by_model[SOL]["blocking_unresolved"]) == \
           ("capped", 0, 0)


def test_a_verifiers_findings_are_counted_from_either_protocol(tmp_path):
    session, project = _session(tmp_path, Script([], {}))
    spec = TaskSpec("t1", "Rotate the key", complexity="standard", kind="security")
    session._record_review(spec, SOL, "verifier", "BLOCKING: the key is logged\nVERDICT: REJECT", complete=True)
    session._record_review(spec, SOL, "verifier", json.dumps(dict(verdict="reject", blocking_findings=["a", "b"])),
                           complete=True)
    session._record_review(spec, SOL, "verifier", "{not json", complete=True)
    session._record_review(spec, SOL, "design-review", "BLOCKING: x\nBLOCKING: y", complete=True, raised=2)
    assert [r["blocking_raised"] for r in session.reviewer_records] == [1, 2, 0, 2]
    assert [r["role"] for r in session.reviewer_records] == ["verifier"] * 3 + ["design-review"]


def test_a_design_review_that_gave_no_verdict_is_not_recorded_as_a_finding_it_raised(tmp_path):
    """The harness writes one blocker for a design review that said neither
    APPROVED nor a BLOCKING line. That blocker is the harness's, so the
    reviewer's record shows no complete verdict and nothing raised."""
    session, project = _session(tmp_path, Script([], {}))
    spec = TaskSpec("t1", "Restyle the header", complexity="standard", kind="frontend")
    for reply in ("APPROVED", "BLOCKING: the hint overlaps the field\nBLOCKING: no focus ring",
                  "Looks good to me overall.", "Approved.", ""):
        session._record_review(spec, GROK, "design-review", reply, complete=True)
    assert [(r["complete"], r["blocking_raised"]) for r in session.reviewer_records] == [
        (True, 0), (True, 2), (False, 0), (False, 0), (False, 0)]


# -- the pieces the contest leans on -------------------------------------------------------

def test_a_task_memory_fork_starts_equal_and_shares_nothing_after(tmp_path):
    from quadratus.memory import TaskMemory
    first = TaskMemory("t1", OPUS, ArtifactStore(tmp_path / "artifacts"))
    first.record("user", "the task")
    first.keep("scope", kind="task-scope")
    second = first.fork(SOL)
    assert second.author == SOL and second.task_id == "t1"
    assert [t.content for t in second.turns()] == ["the task"] and second.refs == first.refs
    second.record("assistant", "only mine")
    second.keep("draft", kind="draft")
    assert len(first.turns()) == 1 and len(first.refs) == 1 and len(second.refs) == 2


def test_a_copy_that_cannot_be_made_leaves_the_routed_lead_to_draft_in_the_project(tmp_path):
    script = Script([_task(), "DONE"], {OPUS: _sub(), SOL: _sub(), GROK: _sub()})
    session, project = _session(tmp_path, script, compete="complex")

    def no_fork(root):
        raise OSError("no room for a copy")
    session.config.fork = no_fork
    session.run(max_tasks=3)
    record = _held(session)
    assert record["formed"] is False
    assert record["reason"] == "its draft could not be isolated in a copy (OSError: no room for a copy)"
    assert script.of("lead") == [(OPUS, "lead", "project", True)] and session.completed
    assert "def sub" in (project / "calc.py").read_text() and "contest" not in session.memory.render(current="")


def test_the_judge_is_capped_like_a_review_seat_and_is_told_no_check_commands(monkeypatch, tmp_path):
    from quadratus.config import Settings
    from quadratus.delegation import invocation
    from quadratus.runtime import JUDGE_ROLE, REVIEW_CAPPED_ROLES, Fleet
    assert JUDGE_ROLE == contest.JUDGE_ROLE and JUDGE_ROLE not in REVIEW_CAPPED_ROLES
    monkeypatch.setattr("shutil.which", lambda _: "/unused/cli")
    views, said = [], []
    monkeypatch.setattr(Fleet, "_generate",
                        lambda self, key, provider, prompt, system, **kw: (views.append(provider),
                                                                           said.append(system)) and "ok")
    (tmp_path / "a.py").write_text("x = 1\n")
    fleet = Fleet(Settings(backend="cli", lead_max_turns=20), project=tmp_path)
    fleet.check_commands = ("python -m pytest -q",)
    with invocation("t1", JUDGE_ROLE):
        assert fleet.invoke(GROK, "p") == "ok"
    assert views[0].max_turns == 20
    assert "APPROVED CHECK COMMANDS" not in said[0] and "ALREADY RUN" not in said[0]
    assert "fresh source copy" in said[0], "read-only, in a copy of the project as it stands"


def test_a_contest_runs_through_the_real_fleet_and_its_forks(monkeypatch, tmp_path):
    """The other tests script ``invoke`` and ``fork``. Here the session is the
    one ``new_session`` wires to a real Fleet, with only the vendor call
    itself replaced: the entrants edit through forked Fleets in private
    copies, and the judge is a read-only call whose copy holds neither draft."""
    from quadratus.config import Settings
    from quadratus.delegation import invocation_context
    from quadratus.runtime import Fleet, new_session
    monkeypatch.setattr("shutil.which", lambda _: "/unused/cli")
    project = (tmp_path / "project").resolve()
    project.mkdir()
    (project / "calc.py").write_text(START)
    (project / "check.py").write_text(CHECK)
    seen, plan = [], [_task(), "DONE"]
    labels = contest.labels_for("t1")

    def generate(self, key, provider, prompt, system, **kw):
        kind = Script.kind(prompt)
        where = Path(provider._workdir).resolve()
        seen.append((key, (invocation_context.get() or {}).get("role"), where,
                     getattr(provider, "max_turns", None), provider._allow_writes))
        if kind == "orchestrator":
            return plan.pop(0) if plan else "DONE"
        if kind == "closeout":
            return "SUMMARY: done\nDECISIONS: none recorded\nDEAD ENDS: none"
        if kind == "judge":
            assert "def sub" not in (where / "calc.py").read_text(), "the judge's copy holds neither draft"
            return f"The plain form.\nWINNER: {labels['challenger']}"
        if kind == "lead":
            body = "    return a + (-b)" if key == OPUS else "    return a - b"
            (where / "calc.py").write_text(START + f"\n\ndef sub(a, b):\n{body}\n")
            return 'Added sub.\nCHANGED: ["calc.py"]'
        return "NO FINDINGS"
    monkeypatch.setattr(Fleet, "_generate", generate)
    fleet = Fleet(Settings(backend="cli", lead_max_turns=20), project=project, allow_writes=True)
    gate = IntegrationGate([sys.executable, "check.py"], cwd=project)
    session = new_session("Add sub", ArtifactStore(tmp_path / "artifacts"), fleet=fleet,
                          config=SessionConfig(compete="complex", integration_gate=gate))
    session.run(max_tasks=3)
    record = _held(session)
    assert record["formed"] and record["decided_by"] == "judge" and record["winner_model"] == SOL
    assert record["judge"]["model"] == GROK and session.completed
    assert "return a - b" in (project / "calc.py").read_text()
    by_role = {}
    for key, role, where, turns, writes in seen:
        by_role.setdefault(role, []).append((key, where, turns, writes))
    # Two editing calls, each in its own copy, neither in the project, both under the lead's cap.
    leads = by_role["lead"]
    assert sorted(key for key, *_ in leads) == [OPUS, SOL]
    assert all(writes and turns == 20 for *_, turns, writes in leads)
    assert len({where for _, where, *_ in leads}) == 2 and project not in {where for _, where, *_ in leads}
    # One judge call: read-only, capped as a review seat is, in a third folder.
    (judge, where, turns, writes), = by_role["judge"]
    assert judge == GROK and not writes and turns == 20
    assert where != project and where not in {w for _, w, *_ in leads}
    assert not any(where.exists() for _, where, *_ in leads), "the entrants' copies are gone"


def test_the_project_runner_hands_the_session_the_budgets_own_answer(monkeypatch, tmp_path):
    from quadratus.config import Settings
    from quadratus.runtime import Fleet, new_session
    monkeypatch.setattr("shutil.which", lambda _: "/unused/cli")
    (tmp_path / "a.py").write_text("x = 1\n")
    budget = RunBudget(RunLimits(max_calls=2, max_reported_tokens=1000))
    fleet = Fleet(Settings(backend="cli"), project=tmp_path, run_budget=budget)
    session = new_session("goal", ArtifactStore(tmp_path / "artifacts"), fleet=fleet)
    assert session.config.budget_covers(2) is True and session.config.budget_covers(3) is False
    assert session.config.compete == "off"
    unbounded = new_session("goal", ArtifactStore(tmp_path / "more"),
                            fleet=Fleet(Settings(backend="cli"), project=tmp_path))
    assert unbounded.config.budget_covers is None, "a run with no limits always covers a contest"
    with pytest.raises(ValueError, match="compete must be one of off, complex"):
        Session("goal", ArtifactStore(tmp_path / "bad"), lambda *a, **k: "", config=SessionConfig(compete="always"))


def test_a_drafts_reported_tokens_are_its_own_lead_calls(tmp_path):
    from quadratus.delegation import DelegationLedger, InvocationEvent, invocation_context

    class Metered(Script):
        def invoke_for(self, root):
            inner = super().invoke_for(root)

            def invoke(model, prompt, system=None, allow_writes=False):
                context = invocation_context.get() or {}
                self.ledger.record(InvocationEvent(
                    task=context.get("task", "run"), role=context.get("role", "direct"), requested_model=model,
                    canonical_model=model, invoked=True, outcome="ok",
                    input_tokens=1000 if model == OPUS else 400, output_tokens=50))
                return inner(model, prompt, system=system, allow_writes=allow_writes)
            return invoke

    script = Metered([_task(), "DONE"], {OPUS: _sub("    return a - b  # BROKEN"), SOL: _sub(), GROK: _sub()})
    script.ledger = DelegationLedger()
    session, project = _session(tmp_path, script, compete="complex", delegation_ledger=script.ledger)
    session.run(max_tasks=3)
    drafts = _by_role(_held(session))
    assert drafts["routed"]["reported_tokens"] == 1050 and drafts["challenger"]["reported_tokens"] == 450
    assert all(isinstance(d["seconds"], float) for d in drafts.values())


# -- the run's records ---------------------------------------------------------------------

class _QuietFleet:
    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


def _scripted_factory(script):
    """A session factory for run_project: the real Session over the script."""
    import dataclasses

    def factory(goal, store, *, config, invariants=None, **kwargs):
        script.project = Path(config.project).resolve()

        def fork(root):
            script.roots.append(Path(root))
            return script.invoke_for(root), (lambda: None)
        return Session(goal, store, script.invoke_for(script.project),
                       config=dataclasses.replace(config, fork=fork), invariants=invariants)
    return factory


def _run_project(tmp_path, monkeypatch, script, **options):
    from quadratus.config import Settings
    from quadratus.project_run import run_project
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "calc.py").write_text(START)
    (project / "check.py").write_text(CHECK)
    monkeypatch.setattr("quadratus.runtime.Fleet", _QuietFleet)
    monkeypatch.setattr("quadratus.runtime.new_session", _scripted_factory(script))
    return project, run_project("Add sub to calc", project, Settings(), allow_writes=True,
                                check=f"{sys.executable} check.py", declared_checks=False, max_tasks=3, **options)


def test_result_json_and_the_scoreboard_record_the_contest_and_the_reviews(tmp_path, monkeypatch):
    script = Script([_task(), "DONE"], {OPUS: _sub("    return a - b  # BROKEN"), SOL: _sub(), GROK: _sub()},
                    reviews={GROK: "BLOCKING: name the operands\nREVIEW: COMPLETE"})
    project, result = _run_project(tmp_path, monkeypatch, script, compete="complex")
    record = json.loads((result.run_dir / "result.json").read_text())
    held = record["contests"]
    assert len(held) == 1 and held[0]["task"] == "t1" and held[0]["formed"] is True
    assert (held[0]["kind"], held[0]["difficulty"], held[0]["decided_by"]) == ("backend", "complex", "gate")
    assert held[0]["winner_model"] == SOL and held[0]["judge"] is None and held[0]["loser_note_delivered"] is True
    for draft in held[0]["drafts"]:
        assert set(draft) >= {"label", "role", "model", "completed", "in_scope", "gate", "changed_lines",
                              "reported_tokens", "seconds", "ended"}
        assert set(draft["gate"]) == {"status", "failing_commands", "failed_cases", "reason"}
    assert {(r["model"], r["role"], r["complete"], r["blocking_raised"], r["recheck"]) for r in record["reviewers"]} == {
        (OPUS, "collaborator", True, 0, None), (GROK, "collaborator", True, 1, "resolved")}
    assert "contest: two blind drafts" in (result.run_dir / "ledger.md").read_text()
    board = project / ".quadratus" / "scoreboard.jsonl"
    assert stat.S_IMODE(os.stat(board).st_mode) == 0o600
    rows = [json.loads(line) for line in board.read_text().splitlines()]
    assert [row["type"] for row in rows] == ["contest", "review", "review"]
    assert all(row["run"] == result.run_dir.name for row in rows)
    assert rows[0]["winner_model"] == SOL and rows[0]["decided_by"] == "gate"
    # A second run only adds to it, and the standalone tool reads the file as written.
    again = Script([_task(), "DONE"], {OPUS: _sub(), SOL: _sub("    return a - b  # BROKEN"), GROK: _sub()})
    (project / "calc.py").write_text(START)
    _, second = _run_project(tmp_path, monkeypatch, again, compete="complex")
    lines = board.read_text().splitlines()
    assert len(lines) == 6 and lines[:3] == [json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows]
    from tools import scoreboard as tool
    summary = tool.summarise(tool.read([project])[0])
    assert (summary["models"][OPUS]["won"], summary["models"][OPUS]["lost"]) == (1, 1)
    assert summary["models"][SOL]["won_by"]["gate"] == 1 and summary["models"][SOL]["lost_by"]["gate"] == 1
    assert summary["reviewers"][GROK]["blocking_raised"] == 1 and summary["reviewers"][GROK]["blocking_resolved"] == 1


def test_a_run_with_contests_off_records_empty_sections_and_writes_no_scoreboard(tmp_path, monkeypatch):
    script = Script([_task("backend simple"), "DONE"], {OPUS: _sub(), SOL: _sub(), GROK: _sub()})
    project, result = _run_project(tmp_path, monkeypatch, script)
    record = json.loads((result.run_dir / "result.json").read_text())
    assert record["contests"] == [] and record["reviewers"] == []
    assert not (project / ".quadratus" / "scoreboard.jsonl").exists(), "nothing to record is no file"
    assert not script.roots
    from quadratus.config import Settings
    from quadratus.project_run import run_project
    with pytest.raises(ValueError, match="compete must be one of off, complex"):
        run_project("goal", project, Settings(), compete="everything")
