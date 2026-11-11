"""The documentation written after the 2026-11-10 review, held to the code.

The batch added flags, settings, records and stops, and every workstream left
notes on what the README, the example environment file and CLAUDE.md should
say. This file pins what was written to the code fact behind it, the way
tests/test_review1110_docs.py does for the earlier statements: each test reads
the code first and the text second, so a later change to either fails here.
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
CLAUDE_MD = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
BASE = "3a0e6f3b508ddf0dff9e95a2d4a8c306e3e2c70a"
#: Spelled by number so this file holds none itself.
EM_DASH = chr(0x2014)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    """One README section: from its heading to the next of the same depth."""
    start = text.index(heading)
    depth = heading.split(" ", 1)[0]
    end = re.search(rf"^{depth} ", text[start + len(heading):], re.MULTILINE)
    return text[start: start + len(heading) + end.start() if end else len(text)]


# -- .env.example --------------------------------------------------------------

#: Set by the harness for a child it starts itself (the gate's report plugin,
#: the worker tool's server). Nothing an operator writes in ``.env``.
_SET_BY_THE_HARNESS = {"QUADRATUS_GATE_NONCE", "QUADRATUS_WORKER_SOCKET", "QUADRATUS_WORKER_TOKEN",
                       "QUADRATUS_WORKER_SCHEMA"}


def _variables_in_the_package() -> set:
    """Every ``QUADRATUS_`` name that opens a string in the package. A name
    ending in ``_`` is a family completed at run time (one per vendor or
    roster key)."""
    names = set()
    for path in (ROOT / "quadratus").rglob("*.py"):
        names |= set(re.findall(r"""(?<=["'])QUADRATUS_[A-Z0-9_]+""", path.read_text(encoding="utf-8")))
    return names


def test_every_variable_the_package_reads_is_in_the_example():
    names = _variables_in_the_package() - _SET_BY_THE_HARNESS
    assert {"QUADRATUS_ENV_PASS", "QUADRATUS_SCRATCH_DIR", "QUADRATUS_TRUST_PROJECT_POLICY"} <= names
    missing = sorted(name for name in names
                     if not re.search(rf"^# {re.escape(name)}[A-Z0-9_]*=", ENV_EXAMPLE, re.MULTILINE))
    assert missing == [], f"read by the package and absent from .env.example: {missing}"


def test_the_example_names_no_quadratus_variable_the_package_lacks():
    known = _variables_in_the_package()
    families = tuple(name for name in known if name.endswith("_"))
    shown = set(re.findall(r"^# (QUADRATUS_[A-Z0-9_]+)=", ENV_EXAMPLE, re.MULTILINE))
    unknown = sorted(name for name in shown if name not in known and not name.startswith(families))
    assert unknown == [], f".env.example shows settings nothing reads: {unknown}"


def test_the_example_shows_each_setting_at_the_default_the_code_has():
    from quadratus import cli_providers, gui, project_watch

    shown = dict(re.findall(r"^# (QUADRATUS_[A-Z0-9_]+)=(.*)$", ENV_EXAMPLE, re.MULTILINE))
    assert int(shown["QUADRATUS_WATCH_MAX_ENTRIES"]) == project_watch.MAX_ENTRIES
    assert int(shown["QUADRATUS_WATCH_MAX_BYTES"]) == project_watch.MAX_BYTES
    assert float(shown["QUADRATUS_WATCH_SECONDS"]) == project_watch.DEADLINE_SECONDS
    assert int(shown["QUADRATUS_CLI_OUTPUT_LIMIT_MB"]) * 1024 * 1024 == cli_providers.MAX_CLI_OUTPUT_BYTES
    assert int(shown["QUADRATUS_GUI_PORT"]) == gui.DEFAULT_GUI_PORT
    assert shown["QUADRATUS_NATIVE_DELEGATION"] == cli_providers.native_delegation_mode({})
    # On unless set to 0: the example shows the value that changes nothing.
    assert shown["QUADRATUS_REQUIREMENTS_LEDGER"] == "1"
    assert 'QUADRATUS_REQUIREMENTS_LEDGER", "1").strip() != "0"' in _flat(
        (ROOT / "quadratus" / "session.py").read_text(encoding="utf-8"))
    # A switch or a list is shown empty, which is how each reads when unset.
    for name in ("QUADRATUS_ENV_PASS", "QUADRATUS_TRUST_PROJECT_POLICY", "QUADRATUS_ALLOW_PROJECT_AGENT_CONFIG",
                 "QUADRATUS_ACCEPT_WATCHED_PATHS", "QUADRATUS_SCRATCH_DIR", "QUADRATUS_GUI_NO_TOKEN",
                 "QUADRATUS_MONITOR_ALLOW_HOST", "QUADRATUS_MONITOR_ALLOW_PROJECT", "QUADRATUS_CONTAINED"):
        assert shown[name] == "", name


def test_the_example_names_the_credential_words_the_rule_has():
    from quadratus import child_env

    text = _flat(ENV_EXAMPLE.replace("#", ""))
    for word in child_env.CREDENTIAL_WORDS + ("PASS",):
        assert re.search(rf"\b{word}\b", text), word
    for name in child_env.NEVER_FOR_A_VENDOR_CLI:
        assert child_env.vendor_env("claude", {name: "x", "QUADRATUS_ENV_PASS": name})[0].get(name) is None
    assert "never given a billed API key" in text


# -- README: the options -------------------------------------------------------

def _parsers():
    from quadratus import cli

    return cli.build_parser()


def test_every_option_the_command_line_has_is_in_the_readme():
    """The other direction of test_the_readme_names_no_option_the_commands_lack:
    an option added to the parser and left out of the README fails here."""
    named = set(re.findall(r"(?<![\w-])--?[a-zA-Z][a-z-]*", README))
    missing = [action.option_strings for action in _parsers()._actions
               if action.option_strings and not set(action.option_strings) & named]
    assert missing == []
    for option in ("--port", "--no-browser"):
        assert f'"{option}"' in (ROOT / "quadratus" / "gui.py").read_text(encoding="utf-8")
        assert option in named


def test_the_defaults_the_readme_states_are_the_defaults_the_code_has():
    from quadratus import cli, run_budget
    from quadratus.session import SessionConfig

    config = {f.name: f.default for f in dataclasses.fields(SessionConfig)}
    limits = {f.name: f.default for f in dataclasses.fields(run_budget.RunLimits)}
    flat = _flat(README)
    assert (limits["max_concurrent_workers"], limits["reserve_tokens_per_call"]) == (2, 0)
    assert "worker errands at once (default 2), tokens that must remain before a call starts (default 0)" in flat
    assert (config["recent_entries"], config["max_fix_cycles"], config["max_consults"]) == (None, 2, 2)
    assert ("ledger entries shown in each prompt (default all), lead revisions per task (default 2), "
            "`CONSULT` questions per task (default 2)") in flat
    assert config["max_parallel_tasks"] == 3 and "tasks at once (default 3; needs `--allow-writes`)" in flat
    assert config["compete"] == "off" and "merge the better draft (default `off`" in flat
    assert cli._DEFAULTS["fix_rounds"] == 1 and "(`--fix-rounds N`, default 1)" in flat
    assert cli._DEFAULTS["history"] == 10 and "how many recent runs to list (default 10)" in flat


def _misplaced(*argv) -> str:
    """What the command line says about flags this run would ignore. Parsing
    only: nothing is started."""
    from quadratus import cli

    parser = cli.build_parser()
    return cli._misplaced(parser, parser.parse_args(list(argv)))


def test_the_readme_says_which_run_each_flag_needs():
    flat = _flat(README)
    assert "A flag the chosen run would ignore is an error" in flat
    assert "the engine flags need `--session` or `--project`" in flat
    assert "the run limits and the trust flags need `--project`" in flat
    assert "`--serve`, `--series` and `--history` need `--monitor`" in flat
    assert "--session or --project" in _misplaced("goal", "--plan-gate")
    for argv in (("goal", "--session", "--check", "true"), ("goal", "--session", "--allow-writes"),
                 ("goal", "--session", "--max-calls", "3"),
                 ("goal", "--session", "--trust-project-policy", "0" * 64),
                 ("goal", "--session", "--accept-watched-paths")):
        assert "require --project" in _misplaced(*argv), argv
    for argv in (("goal", "--history", "3"), ("goal", "--serve"), ("goal", "--series", "x")):
        assert "--monitor" in _misplaced(*argv), argv
    # The one limit that also works without a project, as the table says.
    assert _misplaced("goal", "--session", "--lead-max-turns", "8") == ""


def test_the_three_run_limits_go_together():
    from quadratus import cli

    assert "Give all three or none" in _flat(README)
    parser = cli.build_parser()
    with pytest.raises(ValueError, match="together"):
        cli._run_limits(parser.parse_args(["goal", "--project", "p", "--max-calls", "3"]))
    limits, _ = cli._run_limits(parser.parse_args(
        ["goal", "--project", "p", "--max-calls", "3", "--max-reported-tokens", "9", "--wall-seconds", "5"]))
    assert (limits.max_calls, limits.max_reported_tokens, limits.wall_seconds) == (3, 9, 5)
    assert cli._run_limits(parser.parse_args(["goal", "--project", "p"]))[0] is None


def test_the_baseline_exit_statuses_are_the_ones_the_readme_gives(tmp_path):
    from quadratus import cli

    flat = _flat(README)
    assert ("The exit status is 0 when it ran to its end and its last check passed, 1 when it stopped, "
            "and 2 when its last check failed or no check ran.") in flat

    def status(error, gate):
        (tmp_path / "result.json").write_text(json.dumps({"baseline": {"gate": gate}}), encoding="utf-8")
        return cli._baseline_status(SimpleNamespace(error=error, run_dir=tmp_path))[0]

    assert status("TurnLimitReached: stopped", {"passed": True, "configured": True}) == 1
    assert status(None, {"passed": True, "configured": True}) == 0
    assert status(None, {"passed": False, "configured": True}) == 2
    assert status(None, {"passed": None, "configured": False}) == 2


def test_the_baseline_reads_the_flags_the_readme_lists():
    from quadratus import cli

    section = _flat(_section(README, "### Measuring against one model alone"))
    listed = set(re.findall(r"`(--?[a-z][a-z-]*)", section.split("Beside the goal it reads", 1)[1]))
    dests = {action.dest for action in _parsers()._actions if set(action.option_strings) & listed}
    assert listed and dests <= cli._BASELINE_READS, sorted(dests - cli._BASELINE_READS)
    # A flag the section does not list is refused by name, as it says.
    assert "plan_gate" not in cli._BASELINE_READS and "compete" not in cli._BASELINE_READS


def test_the_two_record_readers_take_the_arguments_the_readme_shows():
    for tool, flags in (("compare_runs.py", ("--grade", "--json")), ("scoreboard.py", ("--json",))):
        run = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / tool), "--help"],
                             capture_output=True, text=True, timeout=60)
        assert run.returncode == 0
        for flag in flags:
            assert flag in run.stdout
    assert "python -I tools/compare_runs.py RUN_DIR RUN_DIR [--grade FILE ...] [--json]" in README
    assert "python tools/scoreboard.py <project folder>" in README
    # The comparison prints what the README says it prints.
    source = (ROOT / "tools" / "compare_runs.py").read_text(encoding="utf-8")
    for label in ("source changed", "final gate", "truth check", "stop reason"):
        assert label in source


# -- README: the security model -------------------------------------------------

def test_the_security_section_names_the_settings_the_code_reads():
    from quadratus import child_env, gui, monitor_server, policy, project_watch

    section = _section(README, "## Security model")
    for name in (policy.TRUST_ENV, project_watch.ALLOW_AGENT_CONFIG_ENV, project_watch.ACCEPT_WATCHED_PATHS_ENV,
                 child_env.PASS_VARIABLE, gui.NO_TOKEN_ENV, monitor_server.ALLOW_HOST_ENV,
                 monitor_server.ALLOW_PROJECT_ENV, "QUADRATUS_SCRATCH_DIR", "QUADRATUS_WATCH_MAX_ENTRIES",
                 "QUADRATUS_WATCH_MAX_BYTES", "QUADRATUS_WATCH_SECONDS"):
        assert name in section, name
    for flag in ("--trust-project-policy", "--allow-project-agent-config", "--no-declared-checks",
                 "--accept-watched-paths", "--allow-host", "--allow-project"):
        assert flag in section, flag
    assert EM_DASH not in section, "no em dashes in text this batch wrote"


def test_the_security_section_lists_the_denials_and_files_the_code_has():
    from quadratus import policy, project

    section = _flat(_section(README, "## Security model"))
    for pattern in policy.BUILTIN_DENY_WRITE:
        assert f"`{pattern}`" in section, pattern
    for name in sorted(project.AGENT_CONFIG_NAMES) + ["/".join(tail) for tail in project.AGENT_CONFIG_TAILS]:
        assert f"`{name}`" in section, name
    for folder in project.AGENT_CONFIG_DIRS:
        assert f"`{folder}/`" in section, folder
    named = {name for name in project.CREDENTIAL_NAMES if not name.startswith("id_")}
    for name in sorted(named) + ["/".join(tail) for tail in project.CREDENTIAL_TAILS]:
        assert f"`{name}`" in section, name
    assert "id_rsa" in project.CREDENTIAL_NAMES and "`id_rsa` and its siblings" in section
    # Build output is the observation class, and the README says which folders.
    for folder in project.PYTHON_OUTPUT_NAMES:
        assert f"`{folder}`" in section, folder


def test_a_policy_that_grants_is_refused_until_its_hash_is_given(tmp_path):
    """The section's first claim, run: a project policy that grants something
    does not load, the refusal prints the hash and both ways to approve, the
    hash loads it, and a policy that only adds a denial needs no approval."""
    from quadratus import policy
    from tests.test_review1110_trust import _policy_doc, _put_policy

    digest = _put_policy(tmp_path, _policy_doc(external_effects="explicit_run_grant"))
    with pytest.raises(policy.PolicyUntrusted) as refusal:
        policy.load_policy(tmp_path)
    message = str(refusal.value)
    assert f"--trust-project-policy {digest}" in message and f"{policy.TRUST_ENV}={digest}" in message
    assert policy.load_policy(tmp_path, trust_project_policy=digest).approved
    _put_policy(tmp_path, _policy_doc(deny_write=["secrets/**"]))
    narrowed = policy.load_policy(tmp_path).resolve(["secrets/a.txt"], writing=True)
    assert narrowed["blocked"] and set(policy.BUILTIN_DENY_WRITE) <= set(narrowed["deny_write"])
    section = _flat(_section(README, "## Security model"))
    assert "can add denials but never remove one" in section
    assert "until you approve that exact file by its sha256, which the refusal prints" in section


def test_the_readme_child_environment_examples_behave_as_it_says():
    from quadratus import child_env

    section = _flat(_section(README, "## Security model"))
    base = {"PATH": "/usr/bin", "HOME": "/home/x", "DOCKER_HOST": "unix:///x", "NODE_AUTH_TOKEN": "t",
            "GITHUB_TOKEN": "g", "DATABASE_URL": "postgres://u:p@h/db", "ANTHROPIC_API_KEY": "k"}
    kept, removed = child_env.check_env(base)
    for name in ("DOCKER_HOST", "NODE_AUTH_TOKEN", "GITHUB_TOKEN"):
        assert f"`{name}`" in section and name in removed
    # Only names are read: the value with a password in it goes through.
    assert "`DATABASE_URL`" in section and kept["DATABASE_URL"] == base["DATABASE_URL"]
    named = dict(base, QUADRATUS_ENV_PASS="DOCKER_HOST,ANTHROPIC_API_KEY")
    assert child_env.check_env(named)[0]["DOCKER_HOST"] == base["DOCKER_HOST"]
    vendor = child_env.vendor_env("claude", named)[0]
    assert vendor["DOCKER_HOST"] == base["DOCKER_HOST"], "naming it hands it to the vendor CLIs too"
    assert "ANTHROPIC_API_KEY" not in vendor, "a billed key is never handed to a vendor CLI"


def test_the_capture_escapes_the_readme_names_are_profile_fields():
    from quadratus.preview import CaptureProfile

    fields = {f.name: f.default for f in dataclasses.fields(CaptureProfile)}
    section = _section(README, "## Security model")
    for name in ("shared_workers", "outside_listener"):
        assert fields[name] is False and f'`"{name}": true`' in section


def test_the_scratch_root_is_where_the_readme_says(monkeypatch):
    from quadratus import providers

    monkeypatch.delenv("QUADRATUS_SCRATCH_DIR", raising=False)
    assert providers.scratch_root_path() == Path.home() / ".cache" / "quadratus" / "scratch"
    assert providers.SCRATCH_MAX_AGE_SECONDS == 24 * 60 * 60
    section = _flat(_section(README, "## Security model"))
    assert "`~/.cache/quadratus/scratch`" in section and "older than 24 hours" in section


# -- README: records ------------------------------------------------------------

def test_the_record_keys_the_readme_names_are_written():
    section = _flat(_section(README, "### What is recorded, and what leaves the machine"))
    source = (ROOT / "quadratus" / "project_run.py").read_text(encoding="utf-8")
    for key in ("selected_limits", "session_settings", "declared_checks", "trust", "child_environment",
                "project_watch", "contests", "reviewers"):
        assert f"`{key}`" in section and f"'{key}'" in source, key


def test_the_state_folder_ignores_everything_but_the_policy_file(tmp_path):
    from quadratus import safe_io

    state = tmp_path / ".quadratus"
    safe_io.prepare_state_dir(state)
    assert (state.stat().st_mode & 0o777) == 0o700
    ignore = (state / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*" in ignore and "!/policy.json" in ignore
    section = _flat(_section(README, "### What is recorded, and what leaves the machine"))
    assert "`.quadratus` carries its own `.gitignore` (everything but `policy.json`)" in section
    assert "owner-only from the moment they are made" in section


def test_the_decider_sentence_gives_the_limit_the_code_has():
    from quadratus import decisions

    section = _flat(_section(README, "### What is recorded, and what leaves the machine"))
    assert f"sends up to {decisions.TASK_TEXT_LIMIT:,} characters" in section


def test_the_layout_names_files_that_exist():
    layout = _section(README, "## Project layout")
    for line in layout.splitlines():
        found = re.match(r"\s+([a-z_]+\.py)(?:, ([a-z_]+\.py))?\s+#", line)
        if not found:
            continue
        for name in filter(None, found.groups()):
            assert (ROOT / "quadratus" / name).is_file() or (ROOT / "tools" / name).is_file(), name


# -- CLAUDE.md: the entries the review settled ---------------------------------

def _entry(title: str) -> str:
    """One ``- **Title...`` entry of CLAUDE.md, up to the next entry or heading."""
    start = CLAUDE_MD.index(title)
    ends = [end for end in (CLAUDE_MD.find("\n- **", start), CLAUDE_MD.find("\n## ", start)) if end != -1]
    return CLAUDE_MD[start: min(ends) if ends else len(CLAUDE_MD)]


#: The decisions the review settled, by the opening words of each entry.
REVIEW_ENTRIES = (
    "- **The project folder is not a source of trusted configuration**",
    "- **What the diff cannot see is listed around every editing call",
    "- **No child inherits credentials it does not need**",
    "- **Transport decisions read typed facts",
    "- **One reader for every verdict",
    "- **An audit that did not run is not NOT MET**",
    "- **Task ids come from one counter",
    "- **Review seats start from the measured diff**",
    "- **Every orchestrator call is a fresh vendor session",
    "- **Run records are private and written whole",
    "- **Loopback is not the lock**",
    "- **A command-line run can be bounded",
    "- **Reserves are held for calls in flight",
    "- **Two models may attempt the same task",
    "- **The scoreboard is a record and nothing reads it back**",
    "- **Whether several models beat one is measured, not assumed**",
    "- **The gate counts only what a runner printed as its summary",
    "- **Undeclared design work is decided from the changed file's own type**",
    "- **The repo's statements about itself are tested**",
)


def _package_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "quadratus").glob("*.py")))


def test_each_review_entry_is_dated_short_and_names_code_that_exists():
    source = _package_source()
    for title in REVIEW_ENTRIES:
        entry = _entry(title)
        assert "2026-11-10" in entry or "2026-11-11" in entry or "same review" in entry, title
        assert EM_DASH not in entry, f"{title}: no em dashes in text this batch wrote"
        assert len(entry.splitlines()) <= 12, f"{title}: {len(entry.splitlines())} lines"
        for name in re.findall(r"`([A-Za-z_./]+\.(?:py|jsonl))`", entry):
            # Two names are files of a project a run works on, not of this repository.
            assert any((ROOT / base / name).exists() for base in ("", "quadratus", "tools")) \
                or name in ("conftest.py", ".quadratus/scoreboard.jsonl"), f"{title}: {name}"
        # A dotted code name: its last part is defined somewhere in the package.
        for owner, name in re.findall(r"`([A-Za-z_]+)\.([A-Za-z_]+)(?:\(\))?`", entry):
            if name in ("py", "json", "jsonl", "md", "example", "toml") or owner == "Blocks":
                continue  # a file name, or Gradio's own Blocks.launch
            assert re.search(rf"\b(?:def|class) {name}\b|^\s*{name}\b[^=\n]*=", source, re.MULTILINE), \
                f"{title}: {owner}.{name}"


def test_the_verdict_entry_states_the_rule_the_reader_applies():
    from quadratus import verdicts

    entry = _flat(_entry("- **One reader for every verdict"))
    assert verdicts.security_verdict_line("The token must be fixed first.\nVERDICT: ACCEPT once fixed") is False
    assert verdicts.security_verdict_line("VERDICT: ACCEPT") is True
    assert verdicts.security_verdict_line("no verdict line here") is None
    assert "A `VERDICT` line that is not a plain `ACCEPT` is a rejection" in entry
    assert verdicts.review_complete("BLOCKING: none") is False
    assert verdicts.review_complete("BLOCKING: none\nREVIEW: COMPLETE") is True
    assert "`BLOCKING: none` is not a verdict" in entry
    assert verdicts.audit_lines("R1: MET - app.py")[0][:2] == ("R1", True)
    assert verdicts.audit_lines("R1: MET, but only partly - app.py")[0][1] is False
    assert "An audit line is `R1: MET - citation`, and no verdict is not met" in entry


def test_the_numbers_the_entries_give_are_the_constants():
    from quadratus import cli, codebase_map, contest, providers, session

    assert session._REVIEW_DIFF_BYTES == 12_000
    assert "bounded at 12,000 bytes" in _flat(_entry("- **Review seats start from the measured diff**"))
    assert codebase_map.MAX_RENDER_CHARS == 8_000 and cli.INTERRUPTED == 130
    records = _flat(_entry("- **Run records are private and written whole"))
    assert "capped at 8,000 characters" in records and "exit 130" in records
    transport = _flat(_entry("- **Transport decisions read typed facts"))
    for fact in providers.TRANSPORT_FACTS:
        assert f"`{fact}`" in transport, fact
    # The contest's rules, in the order the entry lists them.
    assert contest.RULES == ("completed", "scope", "gate", "judge", "size", "default")
    order = _flat(_entry("- **Two models may attempt the same task"))
    places = [order.index(word) for word in ("completed, scope", "gate run on each copy", "one judge",
                                             "the smaller diff", "the routed lead. Only")]
    assert places == sorted(places)


def test_the_audit_stop_is_an_operator_stop_and_the_entry_says_so():
    from quadratus import outcome

    assert outcome._STOP_PREFIX["AuditUnavailable"] == "operator"
    entry = _flat(_entry("- **An audit that did not run is not NOT MET**"))
    assert "the operator stop `AuditUnavailable`, never `RequirementsUnmet`" in entry
    row = next(line for line in (ROOT / "docs" / "workflow-map.md").read_text(encoding="utf-8").splitlines()
               if line.startswith("| Requirements audit did not run"))
    assert "`AuditUnavailable`" in row and row.rstrip().endswith("| `operator` |")


def test_the_corrected_entries_say_what_the_code_does_now():
    from quadratus import integration, registry

    flat = _flat(CLAUDE_MD)
    # --check and --allow-writes need a project; the engine entry no longer
    # lists them under --session.
    assert "require --project" in _misplaced("goal", "--session", "--check", "true")
    engine = _flat(_entry("- **The session engine runs from the command line**"))
    assert "with `--plan-gate`, `--check`" not in engine and "they need `--project`" in engine
    assert integration.PRODUCER == "quadratus-pytest/5" and "`/5` since 2026-11-10" in flat
    assert callable(registry.assert_senior_seats_unrestricted)
    assert "`registry.assert_senior_seats_unrestricted()` holds it at import" in flat
    assert "rendered as a bounded data block into every orchestrator and lead prompt" in flat
    # The history each corrected entry records is still there.
    for kept in ("Two tests pin that split so it cannot drift", "The review edge is now `None` when no peer reviewed",
                 "no line falls back to the scan", "Three asks per decision, then it is interrogating"):
        assert kept in flat, kept


def test_the_watch_rows_of_the_workflow_map_name_the_stops_the_code_raises():
    from quadratus import project_watch

    text = (ROOT / "docs" / "workflow-map.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| Watched path changed by an editing call"))
    assert '`TaskFailed("scope")`' in row and f"`{project_watch.SensitiveChangeInPlace.__name__}`" in row
    assert 'TaskFailed("scope", text' in (ROOT / "quadratus" / "runtime.py").read_text(encoding="utf-8")
    assert f"| `{project_watch.WatchUnavailable.__name__}` |" in text
    batch = text[text.index("- **Parallel batch**"): text.index("- **Turn cap**")]
    for name in ("_read_directives", "batch_evidence.py", "Fleet.reserve_shortfall"):
        assert name in batch, name


# -- the development record ------------------------------------------------------

def test_the_batch_is_on_the_development_record_and_claims_nothing_more():
    record = json.loads((ROOT / "docs" / "dev-record.json").read_text(encoding="utf-8"))
    task = next(task for task in record["tasks"] if task["id"] == "T-review-1110")
    assert task["base"] == BASE and (task["author"], task["reviewer"]) == ("claude", "codex")
    assert "PR #90" in task["purpose"] and "Davis's direct instruction" in task["purpose"]
    # A claim, a note and the author's own delivery (a commit on origin).
    # Reviewed and integrated are the reviewer's and the coordinator's to
    # record, and nothing here claims either.
    assert task["state"] in ("claimed", "delivered") and task["reviews"] == []
    delivery = task["delivery"]
    assert delivery is None or re.fullmatch(r"[0-9a-f]{40}", delivery["sha"])
    assert any("delivery SHA is recorded" in note for note in task["decisions"])
    section = CLAUDE_MD[CLAUDE_MD.index("## How development is coordinated"):
                        CLAUDE_MD.index("## Model routing")]
    for said in ("T-review-1110", "docs/DIRECTION.md", "was not edited", "awaits Codex's independent"):
        assert said in _flat(section), said
