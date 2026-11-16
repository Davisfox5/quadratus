"""The cheaper run shape (``--lean``, 2026-11-15).

After ``--supplied-review`` the stockroom run still reported 7.49M tokens:
revisions 2.1M, planning 1.8M and the audit 0.7M, each a full agent session
that read the project again. These settings hand those seats the files and
take one turn back. Each test here is offline.
"""

import json
import subprocess
from pathlib import Path

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import (
    CLAUDE_SPEC,
    CODEX_SPEC,
    GROK_SPEC,
    MAX_ARGV_PROMPT,
    GrokCLIProvider,
)
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger, invocation
from quadratus.project import Project
from quadratus.providers import ProviderError
from quadratus.run_budget import RunBudget, RunLimits
from quadratus.runtime import (
    SUPPLIED_STDIN_BYTES,
    Fleet,
    SuppliedEditUnusable,
    supplied_edit_files,
    supplied_prompt_limit,
)
from quadratus.session import Session, SessionConfig
from quadratus.session_knobs import LEAN_SETTINGS, SESSION_KNOBS


def _envelope(text):
    return json.dumps({"text": text, "stopReason": "end_turn", "usage": {"input_tokens": 30, "output_tokens": 10}})


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/fake/grok")
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("def add(a, b):\n    return 0\n")
    provider = GrokCLIProvider("", workdir=root, timeout=900, max_retries=4, max_tokens=8000)
    monkeypatch.setattr("quadratus.runtime.build_provider", lambda *a, **kw: provider)
    made = Fleet(Settings(backend="cli"), project=root, allow_writes=True,
                 run_budget=RunBudget(RunLimits(), path=tmp_path / "budget.json"),
                 delegation_ledger=DelegationLedger(path=tmp_path / "invocations.jsonl"))
    return made, root


def _launches(monkeypatch, reply):
    seen = []

    def launch(argv, **kwargs):
        seen.append((list(argv), Path(kwargs["cwd"]), list(Path(kwargs["cwd"]).iterdir())))
        return subprocess.CompletedProcess(argv, 0, _envelope(reply), "")

    monkeypatch.setattr("quadratus.cli_providers._launch", launch)
    return seen


# --- the reply form ----------------------------------------------------------

def test_whole_files_are_read_out_of_a_reply_and_the_rest_is_the_account():
    files, account = supplied_edit_files(
        "Fixed add for RFT1-1.\n=== WRITE FILE app.py ===\ndef add(a, b):\n    return a + b\n"
        "=== END FILE app.py ===\n=== WRITE FILE tests/test_new.py ===\nx = 1\n=== END FILE tests/test_new.py ===\n")
    assert files == {"app.py": "def add(a, b):\n    return a + b\n", "tests/test_new.py": "x = 1\n"}
    assert account == "Fixed add for RFT1-1."


@pytest.mark.parametrize("reply", [
    "I changed app.py.",                                             # no file and no NO CHANGES line
    "=== WRITE FILE app.py ===\ndef add(a, b):\n    return a",       # cut before its end line
    "=== WRITE FILE a.py ===\nx\n=== END FILE b.py ===\n",            # the end names another file
    "=== WRITE FILE a.py ===\n1\n=== END FILE a.py ===\n=== WRITE FILE a.py ===\n2\n=== END FILE a.py ===\n",
])
def test_a_reply_that_is_not_whole_files_is_unusable(reply):
    with pytest.raises(SuppliedEditUnusable):
        supplied_edit_files(reply)


def test_no_changes_with_a_reason_is_a_reply():
    assert supplied_edit_files("NO CHANGES: the finding is about a file this task does not own.") == (
        {}, "NO CHANGES: the finding is about a file this task does not own.")


# --- the runtime -------------------------------------------------------------

def test_a_one_turn_revision_is_written_by_the_harness_and_named(fleet, monkeypatch):
    made, root = fleet
    seen = _launches(monkeypatch, "Fixed add.\nCHANGED: [\"wrong.py\"]\n=== WRITE FILE app.py ===\n"
                                  "def add(a, b):\n    return a + b\n=== END FILE app.py ===\n")
    with invocation("t1", "revision", supplied_edit=True):
        reply = made.invoke("grok:default", "Revise.\n=== FILE app.py ===\n", allow_writes=True)
    (argv, cwd, listing), = seen
    assert listing == [] and cwd != root, "the seat ran in an empty folder, not the project"
    assert argv[argv.index("--max-turns") + 1] == "1" and "--always-approve" not in argv
    assert "You revise from this message alone" in " ".join(argv)
    assert (root / "app.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert reply.endswith('CHANGED: ["app.py"]') and "wrong.py" not in reply
    assert "=== WRITE FILE" not in reply, "the files are on disk; the account goes on"


@pytest.mark.parametrize("reply", ["NEED SESSION: tests/old.py must be deleted",
                                   "I would change add.",
                                   "=== WRITE FILE ../outside.py ===\nx = 1\n=== END FILE ../outside.py ===\n",
                                   "=== WRITE FILE .env ===\nKEY=1\n=== END FILE .env ===\n"])
def test_an_unusable_one_turn_revision_writes_nothing(fleet, monkeypatch, reply):
    made, root = fleet
    before = Project(root).contents()
    _launches(monkeypatch, reply)
    with invocation("t1", "revision", supplied_edit=True), pytest.raises(SuppliedEditUnusable):
        made.invoke("grok:default", "Revise.", allow_writes=True)
    assert Project(root).contents() == before
    assert not (root.parent / "outside.py").exists() and not (root / ".env").exists()


def test_without_the_mark_a_revision_is_the_ordinary_editing_call(fleet, monkeypatch):
    made, root = fleet
    seen = _launches(monkeypatch, 'Nothing.\nCHANGED: []')
    with invocation("t1", "revision"):
        made.invoke("grok:default", "Revise.", allow_writes=True)
    assert seen[0][1] == root, "the ordinary call runs in the project"


def test_a_stdin_cli_takes_a_larger_message_than_an_argv_one():
    assert supplied_prompt_limit(GROK_SPEC) == MAX_ARGV_PROMPT
    assert supplied_prompt_limit(CLAUDE_SPEC) == supplied_prompt_limit(CODEX_SPEC) == SUPPLIED_STDIN_BYTES
    assert supplied_prompt_limit(None) == MAX_ARGV_PROMPT


@pytest.mark.parametrize("role", ["orchestrator", "auditor"])
def test_the_planner_and_the_auditor_take_the_one_turn_view_when_marked(fleet, monkeypatch, role):
    made, root = fleet
    seen = _launches(monkeypatch, "DONE")
    with invocation("run", role, supplied_review=True):
        made.invoke("grok:default", "Decide.")
    assert seen[0][2] == [] and "You judge from this message alone" in " ".join(seen[0][0])


# --- the session -------------------------------------------------------------

def _never(model, prompt, **kwargs):
    pytest.fail(f"this unit asked a model: {model}")


def _session(tmp_path, files, **config):
    root = tmp_path / "project"
    root.mkdir()
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(data)
    return Session("Reject a negative quantity with invalid_quantity. Keep add unchanged.",
                   ArtifactStore(tmp_path / "artifacts"), _never, config=SessionConfig(project=root, **config)), root


def test_the_planner_is_shown_what_fits_and_told_what_does_not(tmp_path):
    session, root = _session(tmp_path, {"app.py": "a = 1\n", "big.py": "x = 1\n" * 40_000})
    block, why = session._supplied_evidence("PLAN", {}, before={}, require=False, since="this run", limit=100_000)
    assert "=== FILE app.py (1 lines) ===" in block and "=== FILE big.py" not in block
    assert "In the project and not shown: big.py." in block and "NEED FILES:" in block
    assert "Changed by this run" in block


def test_the_audit_is_shown_every_changed_file_or_reads_a_copy(tmp_path):
    session, root = _session(tmp_path, {"app.py": "a = 1\n", "big.py": "x = 1\n" * 40_000})
    assert session._supplied_evidence("AUDIT", {}, before={}, limit=100_000) == (
        "", "the changed files do not fit one message")
    block, _ = session._supplied_evidence("AUDIT", {}, before={}, limit=400_000)
    assert "=== FILE big.py (40000 lines) ===" in block and "=== FILE app.py" in block


def test_a_revision_block_does_not_offer_the_reviewers_file_request(tmp_path):
    session, root = _session(tmp_path, {"app.py": "a = 1\n"})
    block, _ = session._supplied_evidence("REVISE", {}, before={}, editing=True)
    assert "=== FILE app.py" in block and "NEED FILES:" not in block


BASED = 'BLOCKING: -1 is accepted | basis: R1 | impact: contract | goal: "Reject a negative quantity"'


@pytest.mark.parametrize("line, kept", [
    (BASED, True),
    ("BLOCKING: now=10**400 raises OverflowError | basis: app.py:3 | impact: correctness", False),
    ('BLOCKING: no rate limit | basis: R1 | impact: contract | goal: "must rate limit every caller"', False),
    ('BLOCKING: short quote | basis: R1 | impact: contract | goal: "Reject"', False),
    ("BLOCKING: the table is dropped on open | basis: app.py:9 | impact: data-loss", True),
])
def test_a_blocking_line_stands_on_quoted_goal_words_or_an_immediate_risk(tmp_path, line, kept):
    session, _ = _session(tmp_path, {"app.py": "a = 1\n"}, goal_basis=True)
    out = session._demote_unbased("openai:sol", "Read it all.\n" + line + "\nR1: MET - app.py\nREVIEW: COMPLETE",
                                  task="t1")
    assert (line in out) is kept
    if not kept:
        assert "NOTE (not blocking in this run" in out and "BLOCKING:" not in out
        assert session.goal_basis_demotions[0]["task"] == "t1"
    else:
        assert session.goal_basis_demotions == []


def test_a_review_left_with_no_blocking_line_is_a_finished_review(tmp_path):
    session, _ = _session(tmp_path, {"app.py": "a = 1\n"}, goal_basis=True)
    out = session._demote_unbased("openai:sol", "BLOCKING: an unspecified case | basis: app.py:1 | impact: other")
    assert out.endswith("REVIEW: COMPLETE")


def test_off_the_reply_is_untouched_and_lean_names_every_setting(tmp_path):
    assert set(LEAN_SETTINGS) <= set(SESSION_KNOBS)
    assert all(getattr(SessionConfig(), name) is False for name in LEAN_SETTINGS)
    from quadratus.cli import _session_options, build_parser
    assert _session_options(build_parser().parse_args(["--lean", "goal"])) == dict.fromkeys(LEAN_SETTINGS, True)


def test_a_write_outside_editable_source_is_refused_before_any_file_is_written(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "ok.py").write_text("old\n")
    with pytest.raises(ProviderError, match="outside editable project source"):
        Project(root).write_files({"ok.py": "new\n", ".git/hooks/pre-commit": "#!/bin/sh\n"})
    assert (root / "ok.py").read_text() == "old\n"


@pytest.mark.parametrize("command, whole", [
    ("python -m pytest -q '--quadratus-report=/tmp/r.json'", True),
    ("python3.13 -m pytest -q -p no:cacheprovider", True),
    ("pytest -vv", True),
    ("python -m pytest -q -k add", False),
    ("python -m pytest -q tests/test_app.py", False),
    ("python -m pytest -q -m slow", False),
    ("python -m unittest discover -s tests", False),
    ("sh -c 'pytest -q'", False),
])
def test_the_outside_checker_reads_a_whole_pytest_run_only_where_nothing_is_selected(command, whole):
    from tools.truth_check import _whole_pytest
    assert _whole_pytest(command) is whole


# --- changed parts only (2026-11-16) ------------------------------------------

TWO = "def add(a, b):\n    return 0\n\n\ndef sub(a, b):\n    return 0\n"
EDIT = ("Fixed add.\n=== EDIT FILE app.py ===\n<<<<<<< FIND\ndef add(a, b):\n    return 0\n=======\n"
        "def add(a, b):\n    return a + b\n>>>>>>> END\n=== END FILE app.py ===\n")


def test_an_edit_block_changes_only_the_text_it_finds():
    files, account = supplied_edit_files(EDIT, {"app.py": TWO})
    assert files == {"app.py": TWO.replace("return 0", "return a + b", 1)} and account == "Fixed add."


def test_blocks_apply_in_order_each_to_the_file_the_one_before_left():
    reply = ("=== EDIT FILE app.py ===\n<<<<<<< FIND\ndef add(a, b):\n    return 0\n=======\ndef add(a, b):\n"
             "    return a + b\n>>>>>>> END\n<<<<<<< FIND\n    return 0\n=======\n    return a - b\n>>>>>>> END\n"
             "=== END FILE app.py ===\n")
    files, _ = supplied_edit_files(reply, {"app.py": TWO})
    assert files["app.py"] == "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


@pytest.mark.parametrize("reply", [
    EDIT.replace("def add(a, b):\n    return 0\n=======", "    return 0\n======="),   # found twice
    EDIT.replace("return 0\n=======", "return 1\n======="),                             # not found
    EDIT.replace("app.py", "new.py"),                                                   # no such file
    EDIT.replace(">>>>>>> END\n", ""),                                                  # a block left open
    EDIT.replace("<<<<<<< FIND\ndef add(a, b):\n    return 0\n", "<<<<<<< FIND\n"),      # an empty FIND
    EDIT + "=== WRITE FILE app.py ===\nx = 1\n=== END FILE app.py ===\n",               # one file two ways
])
def test_an_edit_that_cannot_be_placed_exactly_writes_nothing(reply):
    with pytest.raises(SuppliedEditUnusable):
        supplied_edit_files(reply, {"app.py": TWO})


def test_the_harness_applies_an_edit_block_to_the_project(fleet, monkeypatch):
    made, root = fleet
    (root / "app.py").write_text(TWO)
    seen = _launches(monkeypatch, EDIT)
    with invocation("t1", "revision", supplied_edit=True):
        reply = made.invoke("grok:default", "Revise.", allow_writes=True)
    assert (root / "app.py").read_text() == TWO.replace("return 0", "return a + b", 1)
    assert reply.endswith('CHANGED: ["app.py"]') and "<<<<<<< FIND" not in reply
    assert "only the parts you change" in " ".join(seen[0][0])


def test_the_lead_is_told_to_keep_the_tests_fast_only_in_a_lean_run():
    from quadratus.session_prompts import _FAST_TESTS_RULE
    assert "never once per test" in _FAST_TESTS_RULE and "The whole suite should run in seconds" in _FAST_TESTS_RULE


def test_the_audit_reach_and_small_repair_rules_are_stated_only_in_a_lean_run(tmp_path):
    from quadratus.session_prompts import _AUDIT_REACH_RULE, _MINIMAL_REPAIR_RULE
    assert "only for something the goal's words ask for" in _AUDIT_REACH_RULE
    assert "RISK R<n>:" in _AUDIT_REACH_RULE and "buys no repair" in _AUDIT_REACH_RULE
    assert "never replaces a rule the goal states in plain words" in _MINIMAL_REPAIR_RULE
    assert "supplied_draft" in LEAN_SETTINGS and SessionConfig().supplied_draft is False


def test_the_audit_is_divided_only_when_it_is_long_and_never_past_four_calls(tmp_path):
    session, root = _session(tmp_path, {"app.py": "a = 1\n"}, split_audit=True)
    few = [(f"R{n}", "requirement") for n in range(1, 7)]
    assert session._audit_groups(few) == [few]
    many = [(f"R{n}", "requirement") for n in range(1, 31)] + [("O1", "row")]
    groups = session._audit_groups(many)
    assert len(groups) == 4 and sorted(pair for group in groups for pair in group) == sorted(many)
    assert ("O1", "row") in groups[0]
    (tmp_path / "off").mkdir()
    plain, _ = _session(tmp_path / "off", {"app.py": "a = 1\n"})
    assert plain._audit_groups(many) == [many]


def test_a_kept_verdict_lapses_when_a_file_it_named_changes(tmp_path):
    session, root = _session(tmp_path, {"app.py": "a = 1\n", "other.py": "b = 2\n"}, sticky_audit=True)
    session._remember_met("R1: MET - app.py shows it\nR2: MET - nothing named\nR3: NOT MET - no", {
        "R1": (True, "app.py"), "R2": (True, "x"), "R3": (False, "no")})
    required = [("R1", "requirement"), ("R2", "requirement"), ("R3", "requirement")]
    assert list(session._kept_met_lines(required)) == ["R1"]
    (root / "other.py").write_text("b = 3\n")
    assert list(session._kept_met_lines(required)) == ["R1"]
    (root / "app.py").write_text("a = 2\n")
    assert session._kept_met_lines(required) == {}


def test_a_question_file_waits_for_its_answer_file(tmp_path):
    import threading

    from quadratus.cli import _file_operator
    from quadratus.session import OperatorInputNeeded
    ask = _file_operator(tmp_path / "q", wait=5)
    threading.Timer(0.3, lambda: (tmp_path / "q" / "answer-1.txt").write_text("yes\n")).start()
    assert ask("May sub change?") == "yes"
    assert (tmp_path / "q" / "question-1.txt").read_text() == "May sub change?"
    with pytest.raises(OperatorInputNeeded):
        _file_operator(tmp_path / "q", wait=0.2)("And add?")
