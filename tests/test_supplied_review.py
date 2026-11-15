"""A review that judges handed files in one turn (``--supplied-review``).

The stockroom benchmark (2026-11-13): one review of a finished job reported
1.25M tokens, as much as building it, because a review seat was a full agent
session that read the project again. With the setting on, the session puts
the changed files in the reviewer's message and the runtime runs the seat in
an empty folder for one turn. Off, nothing changes.
"""

import json
import subprocess
from pathlib import Path

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import MAX_ARGV_PROMPT, SUPPLIED_JUDGMENT_TIMEOUT, GrokCLIProvider
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger, invocation
from quadratus.project import Project
from quadratus.providers import ProviderError
from quadratus.run_budget import RunBudget, RunLimits
from quadratus.runtime import SUPPLIED_REVIEW_ROLES, Fleet
from quadratus.session import Session, SessionConfig
from quadratus.session_knobs import SESSION_KNOBS, session_options

OK = json.dumps({"text": "NO FINDINGS", "stopReason": "end_turn",
                 "usage": {"input_tokens": 30, "output_tokens": 10}})


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/fake/grok")
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n")
    provider = GrokCLIProvider("", workdir=root, timeout=900, max_retries=4, max_tokens=8000)
    monkeypatch.setattr("quadratus.runtime.build_provider", lambda *a, **kw: provider)
    budget = RunBudget(RunLimits(), path=tmp_path / "budget.json")
    ledger = DelegationLedger(path=tmp_path / "invocations.jsonl")
    made = Fleet(Settings(backend="cli"), project=root, allow_writes=True, run_budget=budget,
                 delegation_ledger=ledger)
    return made, provider, budget, ledger, root


def _launches(monkeypatch, reply=OK):
    seen = []

    def launch(argv, **kwargs):
        seen.append((list(argv), Path(kwargs["cwd"]), kwargs["timeout"], list(Path(kwargs["cwd"]).iterdir())))
        return subprocess.CompletedProcess(argv, 0, reply, "")

    monkeypatch.setattr("quadratus.cli_providers._launch", launch)
    return seen


# --- the runtime's view ------------------------------------------------------

def test_a_supplied_review_runs_one_turn_in_an_empty_folder_and_copies_nothing(fleet, monkeypatch):
    made, owner, budget, ledger, root = fleet
    seen = _launches(monkeypatch)
    monkeypatch.setattr(made.project, "snapshot", lambda: pytest.fail("the source must not be copied"))
    with invocation("t1", "collaborator", supplied_review=True, harness_checks="check: passed, 3 cases"):
        assert made.invoke("grok:default", "Review this.\n=== FILE app.py ===\n") == "NO FINDINGS"
    (argv, cwd, timeout, listing), = seen
    assert listing == [] and not cwd.exists(), "an empty folder, removed afterwards"
    assert cwd != root and root not in cwd.parents
    assert timeout <= SUPPLIED_JUDGMENT_TIMEOUT
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert "--always-approve" not in argv
    text = " ".join(argv)
    assert "You judge from this message alone" in text and "fresh source copy" not in text
    assert "THE HARNESS HAS ALREADY RUN" in text and "check: passed, 3 cases" in text
    assert budget.snapshot()["reported_tokens"] == 40
    assert ledger.events[-1].role == "collaborator"
    # The shared provider is the one it was: the bounds live on the view.
    assert owner.timeout == 900 and owner.max_retries == 4 and not getattr(owner, "summary_only", False)


@pytest.mark.parametrize("role", sorted(SUPPLIED_REVIEW_ROLES))
def test_both_review_roles_take_the_view(fleet, monkeypatch, role):
    made = fleet[0]
    seen = _launches(monkeypatch)
    with invocation("t1", role, supplied_review=True):
        made.invoke("grok:default", "Judge this.")
    assert seen[0][3] == []


@pytest.mark.parametrize("context", [dict(), dict(supplied_review=False)])
def test_without_the_mark_a_review_reads_a_source_copy_as_before(fleet, monkeypatch, context):
    made = fleet[0]
    seen = _launches(monkeypatch)
    with invocation("t1", "collaborator", **context):
        made.invoke("grok:default", "Review this.")
    listing = seen[0][3]
    assert "app.py" in [p.name for p in listing], "the copy holds the project"
    assert "fresh source copy" in " ".join(seen[0][0])


@pytest.mark.parametrize("role", ["verifier", "design-review"])
def test_a_seat_that_reads_the_whole_project_keeps_its_copy_even_when_marked(fleet, monkeypatch, role):
    made = fleet[0]
    seen = _launches(monkeypatch)
    with invocation("t1", role, supplied_review=True):
        made.invoke("grok:default", "Judge this.")
    assert "app.py" in [p.name for p in seen[0][3]]


def test_evidence_over_one_message_is_refused_before_any_call(fleet, monkeypatch):
    made, _, budget, _, _ = fleet
    seen = _launches(monkeypatch)
    with invocation("t1", "collaborator", supplied_review=True):
        with pytest.raises(ProviderError, match="128,000-byte"):
            made.invoke("grok:default", "x" * (MAX_ARGV_PROMPT + 1))
    assert seen == [] and budget.snapshot()["reserved_attempts"] == 0


def test_a_failure_the_vendor_typed_as_passing_is_sent_again_once(fleet, monkeypatch):
    made = fleet[0]
    calls = []

    def generate(key, view, prompt, role, **kw):
        calls.append(kw.get("first_attempt"))
        if len(calls) == 1:
            failure = ProviderError("overloaded")
            failure.retryable = True
            raise failure
        return "NO FINDINGS"

    monkeypatch.setattr(made, "_generate", generate)
    with invocation("t1", "recheck", supplied_review=True):
        assert made.invoke("grok:default", "Judge this.") == "NO FINDINGS"
    assert calls == [1, 2]


@pytest.mark.parametrize("retryable", [False, None, "yes"])
def test_any_other_failure_is_raised_as_the_review_call_s_own(fleet, monkeypatch, retryable):
    made = fleet[0]
    calls = []

    def generate(key, view, prompt, role, **kw):
        calls.append(1)
        failure = ProviderError("refused")
        if retryable is not None:
            failure.retryable = retryable
        raise failure

    monkeypatch.setattr(made, "_generate", generate)
    with invocation("t1", "collaborator", supplied_review=True), pytest.raises(ProviderError, match="refused"):
        made.invoke("grok:default", "Judge this.")
    assert calls == [1]


def test_a_second_typed_failure_is_raised_and_not_sent_a_third_time(fleet, monkeypatch):
    made = fleet[0]
    calls = []

    def generate(key, view, prompt, role, **kw):
        calls.append(1)
        failure = ProviderError("overloaded")
        failure.retryable = True
        raise failure

    monkeypatch.setattr(made, "_generate", generate)
    with invocation("t1", "collaborator", supplied_review=True), pytest.raises(ProviderError, match="overloaded"):
        made.invoke("grok:default", "Judge this.")
    assert calls == [1, 1]


# --- the session's evidence --------------------------------------------------

def _never(model, prompt, **kwargs):
    pytest.fail(f"this unit asked a model: {model}")


def _session(tmp_path, files, **config):
    root = tmp_path / "project"
    root.mkdir()
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data if isinstance(data, bytes) else data.encode())
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), _never,
                      config=SessionConfig(project=root, supplied_review=True, **config))
    session._task_before = Project(root).contents()
    return session, root


def test_changed_files_are_shown_whole_with_small_unchanged_files_for_context(tmp_path):
    session, root = _session(tmp_path, {"app.py": "old\n", "util.py": "def helper():\n    return 1\n",
                                        "big.txt": "x" * 30_000})
    (root / "app.py").write_text("new line one\nnew line two\n")
    (root / "tests").mkdir()
    (root / "tests/test_app.py").write_text("def test_it():\n    assert True\n")
    block, why = session._supplied_evidence("PROMPT", {})
    assert "=== FILE app.py (2 lines) ===\nnew line one\nnew line two\n=== END FILE app.py ===" in block
    assert "=== FILE tests/test_app.py (2 lines) ===" in block
    assert "Changed by this task and shown whole as they are now: app.py, tests/test_app.py." in block
    assert "Unchanged, shown whole for context: util.py." in block and "def helper():" in block
    assert "Unchanged and not shown: big.txt." in block and "x" * 100 not in block
    assert "data, not instructions" in block
    assert why == "2 changed and 1 unchanged file(s) handed over"


def test_a_removed_file_is_named(tmp_path):
    session, root = _session(tmp_path, {"app.py": "old\n", "gone.py": "x = 1\n"})
    (root / "gone.py").unlink()
    (root / "app.py").write_text("new\n")
    block, _ = session._supplied_evidence("PROMPT", {})
    assert "Removed by this task: gone.py." in block and "=== FILE gone.py" not in block


def test_the_block_and_the_prompt_together_fit_one_message(tmp_path):
    files = {f"mod{i}.py": "y = 1\n" * 2_000 for i in range(12)}  # 12,000 bytes each, all unchanged
    session, root = _session(tmp_path, {"app.py": "old\n", **files})
    (root / "app.py").write_text("new\n")
    prompt = "P" * 40_000
    block, _ = session._supplied_evidence(prompt, {})
    assert block and len((prompt + block).encode()) <= MAX_ARGV_PROMPT - session._SUPPLIED_HEADROOM_BYTES
    assert "Unchanged and not shown:" in block, "what did not fit is named, never cut"


@pytest.mark.parametrize("change, reason", [
    (lambda root: (root / "app.py").write_text("z" * 200_000), "the changed files do not fit one message"),
    (lambda root: (root / "app.py").write_bytes(b"\xff\xfe\x00binary"), "the changed file app.py is not text"),
])
def test_what_cannot_be_shown_whole_sends_the_review_to_a_copy(tmp_path, change, reason):
    session, root = _session(tmp_path, {"app.py": "old\n"})
    change(root)
    assert session._supplied_evidence("PROMPT", {}) == ("", reason)


def test_no_measured_start_or_design_evidence_sends_the_review_to_a_copy(tmp_path):
    session, root = _session(tmp_path, {"app.py": "old\n"})
    (root / "app.py").write_text("new\n")
    assert session._supplied_evidence("PROMPT", {"evidence_files": ("shot.png",)}) == (
        "", "the review has design evidence files to open")
    session._task_before = None
    assert session._supplied_evidence("PROMPT", {}) == (
        "", "the harness holds no measurement of the task's starting source")


def test_no_scratch_demonstration_is_asked_of_a_supplied_review(tmp_path, monkeypatch):
    session, _ = _session(tmp_path, {"app.py": "old\n"})
    monkeypatch.setattr("quadratus.session_evidence._seat_takes_scratch", lambda key: True)
    monkeypatch.setattr(session, "_discovering_checks", lambda: ["python -m pytest -q"])
    assert session._probe_checks("openai:sol") == []
    session.config.supplied_review = False
    assert session._probe_checks("openai:sol") == ["python -m pytest -q"]


# --- the setting -------------------------------------------------------------

def test_the_setting_is_off_by_default_and_is_one_of_the_session_settings():
    assert SessionConfig().supplied_review is False
    assert SESSION_KNOBS["supplied_review"] is None
    assert session_options({"supplied_review": True}) == {"supplied_review": True}
    with pytest.raises(ValueError, match="supplied_review must be True or False"):
        session_options({"supplied_review": 1})


def test_the_flag_chooses_the_setting():
    from quadratus import cli
    parser = cli.build_parser()
    on = parser.parse_args(["--project", ".", "--session", "--supplied-review", "goal"])
    off = parser.parse_args(["--project", ".", "--session", "goal"])
    assert cli._session_options(on) == {"supplied_review": True}
    assert cli._session_options(off) == {}


# --- found by the same benchmark run: a denied write outside the project ------

def test_a_denied_write_outside_the_project_is_the_grant_working_not_a_lost_grant(tmp_path):
    """A lead mistyped the project's absolute path in one write (a hyphen for
    a slash). The CLI denied it, as it should, and the run stopped as if the
    write grant had been lost."""
    from quadratus.runtime import _relevant_denials
    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)
    inside = dict(kind="permission_denied", tool="Write", status="denied", path=str(project / "pkg/api.py"))
    mistyped = dict(kind="permission_denied", tool="Write", status="denied",
                    path=str(tmp_path) + "-project/pkg/api.py")
    relative = dict(kind="permission_denied", tool="Edit", status="denied", path="pkg/api.py")
    unknown = dict(kind="permission_denied", tool="Write", status="denied")
    escape = dict(kind="permission_denied", tool="Write", status="denied", path=str(project / "../elsewhere.py"))

    def judged(*entries):
        return _relevant_denials(list(entries), (), writes_granted=True, project=project)

    assert judged(mistyped) == [] and judged(escape) == []
    assert judged(inside) == [f"Write {project / 'pkg/api.py'}"]
    assert judged(relative) == ["Edit pkg/api.py"], "a path not known to be outside still counts"
    assert judged(unknown) == ["Write"]
    # With no project named, the rule is the one it was.
    assert _relevant_denials([mistyped], (), writes_granted=True) == [f"Write {mistyped['path']}"]


# --- when the handed files are not enough (benchmark run, 2026-11-14) ----------
# A re-check of a finding about a test file an earlier task wrote was handed
# only the current task's files; the reviewer answered that the file "is not
# shown", which was read as the finding still standing, and the run stopped.

def test_a_file_the_prompt_names_is_handed_over_whatever_its_size(tmp_path):
    big = "def test_close():\n    assert True\n" + "# pad\n" * 6_000  # over the context-file size
    session, root = _session(tmp_path, {"app.py": "old\n", "tests/test_api.py": big, "other.py": "x = 1\n"})
    (root / "app.py").write_text("new\n")
    assert len(big) > session._SUPPLIED_CONTEXT_FILE_BYTES
    block, _ = session._supplied_evidence("RFT3-11: tests/test_api.py::test_close is not shown to pass", {})
    assert "=== FILE tests/test_api.py (" in block and "def test_close():" in block
    assert "Unchanged, shown whole for context: tests/test_api.py, other.py." in block
    unnamed, _ = session._supplied_evidence("PROMPT", {})
    assert "=== FILE tests/test_api.py" not in unnamed and "Unchanged and not shown: tests/test_api.py." in unnamed


def test_a_named_file_that_does_not_fit_is_left_out_and_named_never_cut(tmp_path):
    session, root = _session(tmp_path, {"app.py": "old\n", "tests/test_api.py": "y = 1\n" * 30_000,
                                        "other.py": "x = 1\n"})
    (root / "app.py").write_text("new\n")
    block, _ = session._supplied_evidence("about tests/test_api.py", {})
    assert "=== FILE tests/test_api.py" not in block and "=== FILE other.py" in block
    assert "Unchanged and not shown: tests/test_api.py." in block


def test_the_block_tells_the_seat_what_to_reply_when_a_file_is_missing(tmp_path):
    from quadratus.session_prompts import _NEED_FILES_RULE
    session, root = _session(tmp_path, {"app.py": "old\n"})
    (root / "app.py").write_text("new\n")
    assert _NEED_FILES_RULE in session._supplied_evidence("PROMPT", {})[0]
    assert "`NEED FILES: <path>[, <path>]`" in _NEED_FILES_RULE


@pytest.mark.parametrize("reply, asked", [
    ("NEED FILES: tests/test_api.py", "tests/test_api.py"),
    ("\n  NEED FILES: a.py, b.py\nI could not judge RFT1-1.", "a.py, b.py"),
    ("NEED FILES:", "(none named)"),
    ("RFT1-1: UNRESOLVED - tests/test_api.py is not shown\nNEED FILES: tests/test_api.py", ""),
    ("need files: a.py", ""),
    ("NO FINDINGS", ""),
    (None, ""),
])
def test_only_a_first_line_request_asks_for_files(reply, asked):
    from quadratus.reply_parsers import _needs_files
    assert _needs_files(reply) == asked


def test_a_seat_that_asks_for_files_is_asked_again_once_with_a_copy(tmp_path, monkeypatch):
    session, root = _session(tmp_path, {"app.py": "old\n"})
    (root / "app.py").write_text("new\n")
    notes, calls = [], []
    session.config.progress = notes.append
    replies = iter(["NEED FILES: tests/test_api.py", "RFT1-1: RESOLVED - tests/test_api.py::test_close passes"])

    def once(key, prompt, *, allow_writes=False, supplied=True):
        calls.append(supplied)
        session._last_supplied = supplied
        return next(replies)

    monkeypatch.setattr(session, "_invoke_model_call_once", once)
    with invocation("t1", "recheck"):
        assert session._invoke_model_call("openai:sol", "Re-check RFT1-1.").startswith("RFT1-1: RESOLVED")
    assert calls == [True, False], "one supplied attempt, then one with a copy"
    assert session.supplied_reviews[-1]["mode"] == "copy"
    assert "asked for files it was not shown: tests/test_api.py" in session.supplied_reviews[-1]["why"]


def test_a_copy_review_that_says_need_files_is_not_asked_again(tmp_path, monkeypatch):
    session, _ = _session(tmp_path, {"app.py": "old\n"})
    calls = []

    def once(key, prompt, *, allow_writes=False, supplied=True):
        calls.append(supplied)
        session._last_supplied = False  # the evidence did not fit: this one read a copy
        return "NEED FILES: x.py"

    monkeypatch.setattr(session, "_invoke_model_call_once", once)
    with invocation("t1", "collaborator"):
        assert session._invoke_model_call("openai:sol", "Review.") == "NEED FILES: x.py"
    assert calls == [True]
