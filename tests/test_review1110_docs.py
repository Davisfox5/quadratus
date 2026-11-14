"""What the repository says about itself, held to what the code does.

Review of 2026-11-10 (design.md sections 4 and 7, gui_records.md items 13,
14, 23 and 24, transport.md item 19). Every defect here was a statement that
had drifted from the code: a comment, the README, CLAUDE.md, the example
environment file, the CI workflow or the acceptance image recipe. Each test
reads the code fact first and then the text that describes it, so the next
drift fails here and not in a reader's head.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from quadratus import decisions, registry, task_kinds, workers
from quadratus.cli_providers import CLI_SPECS
from quadratus.config import DEFAULT_CLI_MODELS, Settings

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
CLAUDE_MD = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")


def _source(name: str) -> str:
    return (ROOT / "quadratus" / name).read_text(encoding="utf-8")


def _entry(text: str, title: str) -> str:
    """One ``- **Title...`` entry of CLAUDE.md, up to the next entry."""
    start = text.index(title)
    end = text.find("\n- ", start)
    return " ".join(text[start: end if end != -1 else len(text)].split())


def _flat(text: str) -> str:
    return " ".join(text.split())


# -- decisions.py: the seed answers and the reference beside them ------------

def test_the_kind_seed_names_only_real_kinds():
    seed = decisions.DECISIONS["task.kind"]
    assert set(seed["answers"]) <= set(task_kinds.ROUTING), "a seed answer that is not a routing kind"
    assert "test" in seed["answers"] and "testing" not in seed["answers"]


def test_the_kind_seed_note_points_at_something_that_exists():
    note = decisions.DECISIONS["task.kind"]["note"]
    named = re.findall(r"task_kinds\.(\w+)", note)
    assert named, "the note says where the live answers come from"
    for name in named:
        assert hasattr(task_kinds, name), f"task_kinds.{name} does not exist"
    assert set(task_kinds.ROUTING) == set(getattr(task_kinds, named[0]))


# -- registry.py -------------------------------------------------------------

_COUNT_WORDS = {"nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14}


def test_the_roster_size_a_comment_names_is_the_roster_size():
    named = re.findall(r"\b(\w+)-model roster", _source("registry.py"))
    assert named, "the mode roster comment still states a size"
    for word in named:
        assert _COUNT_WORDS.get(word.lower()) == len(registry.ROSTER), (word, len(registry.ROSTER))


def test_the_orchestrator_comment_names_the_fallback_the_chain_has():
    assert registry.ORCHESTRATOR_CHAIN == ["claude:fable", "openai:astra"]
    source = _source("registry.py")
    assert "Opus 5 backs it up" not in source
    block = source[source.index("#: Ordered preference for the orchestrator seat."):
                   source.index("ORCHESTRATOR_CHAIN: List[str]")]
    assert "GPT-6 Astra alone" in _flat(block.replace("#:", ""))


def test_registry_names_the_probe_by_the_flag_the_cli_has():
    assert '"--probe"' in _source("cli.py")
    assert not re.search(r"quadratus probe\b", _source("registry.py"))


def test_registry_comments_do_not_call_called_aliases_unverified():
    source = _source("registry.py")
    assert "aliases unverified" not in source
    # Every xAI row sends no model flag, so there is no alias to verify.
    assert all(m.vendor_default for m in registry.ROSTER if m.provider == "grok")
    assert "no alias is sent" in _flat(source.replace("#", ""))


def test_registry_says_which_tables_nothing_reads():
    """REFUSAL_CHAIN and CONTROL_PLANE have no caller; the comments say so.
    When one gains a caller this fails, and the comment is what to update."""
    import ast

    for table in ("REFUSAL_CHAIN", "CONTROL_PLANE"):
        readers = []
        for path in (ROOT / "quadratus").glob("*.py"):
            if path.name == "registry.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                imported = isinstance(node, ast.ImportFrom) and any(a.name == table for a in node.names)
                if imported or (isinstance(node, ast.Attribute) and node.attr == table):
                    readers.append(path.name)
        assert readers == [], (table, readers)
    source = _source("registry.py")
    assert "Nothing reads this chain yet" in source
    assert "nothing reads this table" in source


# -- task_kinds.py -----------------------------------------------------------

def test_an_unpinned_kind_rides_the_ladder_and_the_module_says_so():
    # The rotated default is passed in and loses to the rung whenever a rung fits.
    assert task_kinds.route("general", default="claude:opus", difficulty="simple") == \
        task_kinds.DIFFICULTY_LADDER["simple"]
    assert task_kinds.route("backend", default="grok:default", difficulty="complex") == \
        task_kinds.DIFFICULTY_LADDER["complex"]
    doc = _flat(task_kinds.__doc__)
    assert "The lead rotates as normal" not in doc
    assert "rides the difficulty ladder" in doc
    for kind, policy in task_kinds.ROUTING.items():
        assert "rotat" not in policy.evidence.lower(), kind
        assert "scoreboard" not in policy.evidence.lower(), kind


def test_the_docs_row_counts_the_orchestrator_seats_correctly():
    anthropic = [k for k in registry.ORCHESTRATOR_CHAIN if k.startswith("claude:")]
    assert len(registry.ORCHESTRATOR_CHAIN) == 2 and len(anthropic) == 1
    assert "two of the three orchestrator seats" not in _flat(task_kinds.ROUTING["docs"].evidence)


# -- workers.py, memory.py, usage.py, repo_scan.py ---------------------------

def test_the_worker_module_names_the_lookup_worker_the_tree_has():
    assert workers.WORKER_TREE["lookup"] == "grok:worker"
    doc = _flat(workers.__doc__)
    assert "live lookup to Grok Fast" not in doc
    assert "live lookup to the Grok worker call" in doc
    assert "only Haiku is verified" not in _source("workers.py")


def test_the_memory_module_describes_the_fallback_seat():
    import quadratus.memory as memory

    doc = _flat(memory.__doc__)
    assert len(registry.ORCHESTRATOR_CHAIN) == 2
    assert "halts the run rather than being substituted for" not in doc
    assert "GPT-6 Astra" in doc and "only when both are gone" in doc


def test_the_usage_module_says_every_cli_reports_usage():
    import quadratus.usage as usage

    assert all(spec.extract_usage is not None for spec in CLI_SPECS.values())
    doc = _flat(usage.__doc__)
    assert "All three do today" in doc and "codex" in doc and "Grok" in doc
    # One model line, three seats: the sheet prices them alike and says why.
    grok = {usage.PRICES[f"grok:{seat}"] for seat in ("default", "worker", "expert")}
    assert len(grok) == 1
    assert "share its rate" in _flat(_source("usage.py").replace("#", ""))


def test_the_interview_is_described_as_not_built():
    import quadratus.repo_scan as repo_scan

    doc = _flat(repo_scan.__doc__)
    assert "when it is built" in doc and "not implemented yet" in doc
    assert "which belongs in the interview, not the loop" not in _source("session.py")
    assert "the interview is not built yet" in _source("session.py")


# -- .env.example ------------------------------------------------------------

def test_copying_the_example_sets_nothing():
    """The file promises that an empty .env works. A live placeholder key
    line broke that: the copied file set three keys to junk."""
    from dotenv import dotenv_values

    assert dict(dotenv_values(ROOT / ".env.example")) == {}


def test_the_example_shows_the_cli_model_defaults_the_code_has():
    shown = dict(re.findall(r"^# ((?:CLAUDE|OPENAI|GROK)_CLI_MODEL_(?:HIGH|LOW))=(.*)$", ENV_EXAMPLE, re.MULTILINE))
    expected = {f"{vendor.upper()}_CLI_MODEL_{tier.upper()}": value
                for vendor, tiers in DEFAULT_CLI_MODELS.items() for tier, value in tiers.items()}
    assert shown == expected
    assert "grok-4.5" not in ENV_EXAMPLE


def test_the_example_does_not_promise_a_low_tier_nothing_requests():
    """Every caller takes the default tier. When one asks for "low" this
    fails, and the example's paragraph is what to bring back."""
    import ast

    tiers = []
    for path in (ROOT / "quadratus").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "model_for"):
                tiers.append((path.name, len(node.args), [k.arg for k in node.keywords]))
    assert tiers and all(args == 1 and not keywords for _, args, keywords in tiers), tiers
    flat = _flat(ENV_EXAMPLE.replace("#", ""))
    assert 'The "low" tier is used by the control plane' not in flat
    assert 'Nothing requests the "low" tier yet' in flat


def test_the_example_does_not_deny_the_signed_in_checks():
    assert all(spec.verified for spec in CLI_SPECS.values())
    assert "no signed-in call has been made" not in _flat(ENV_EXAMPLE.replace("#", ""))


# -- CLAUDE.md agrees with itself and with the code --------------------------

def test_claude_md_names_the_grok_seats_the_code_uses():
    rote = task_kinds.DIFFICULTY_LADDER["rote"]
    lookup = workers.WORKER_TREE["lookup"]
    bump = workers.WORKER_ESCALATION[lookup]
    assert (rote, lookup, bump) == ("grok:worker", "grok:worker", "grok:expert")
    assert registry.resolve("grok:grok-4.5") is None, "Grok 4.5 is not a roster row"
    ladder = _entry(CLAUDE_MD, "- **Difficulty is the primary routing axis")
    assert f"`{rote}`" in ladder and "rote → Grok 4.5." not in ladder
    tree = _entry(CLAUDE_MD, "- **Workers are picked by errand")
    assert f"lookup → `{lookup}`" in tree and f"`{lookup}`→`{bump}`" in tree
    tools = _entry(CLAUDE_MD, "- **Tool requests flow worker")
    assert f"`{bump}`" in tools and "escalates to the vendor default" not in tools
    catalogue = _entry(CLAUDE_MD, "- **A vendor's catalogue is not what your transport can invoke")
    assert "xAI's seats are now" not in catalogue and f"`{bump}`" in catalogue
    # The correction changes the tense, not the record of why 4.5 held the rung.
    assert "not documented as one" in catalogue
    assert "load-spreading argument rather than by price" in catalogue


def test_claude_md_says_once_how_many_cli_specs_are_verified():
    assert all(spec.verified for spec in CLI_SPECS.values())
    flat = _flat(CLAUDE_MD)
    assert "All three CLI specs are now probe-verified" in flat
    assert "are still written from documentation" not in flat
    assert "Only the Claude CLI has been verified" not in flat


def test_playwright_is_described_as_the_dependency_it_is():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert any(dep.startswith("playwright") for dep in project["dependencies"])
    assert project["optional-dependencies"]["browser"] == []
    for text in (CLAUDE_MD, README):
        assert "optional `playwright` extra" not in _flat(text)


# -- README ------------------------------------------------------------------

def test_the_readme_lists_every_worker_errand():
    sentence = _flat(README)
    listed = re.search(r"picked by errand type: ([a-z /]+)\)", sentence)
    assert listed, "the worker-bee sentence lists the errand types"
    assert {name.strip() for name in listed.group(1).split("/")} == set(workers.WORKER_TREE)


def test_the_readme_states_the_reviewer_rule_the_code_applies():
    from quadratus.session import Complexity

    counts = {level: Complexity.collaborator_count(level, 2) for level in ("rote", "simple", "standard", "complex")}
    assert counts == {"rote": 0, "simple": 0, "standard": 1, "complex": 2}
    rule = _flat(README)
    assert "never one reviewer (the two pinned reviewers" not in rule
    # The lead is a member itself, so a complex task draws the others, not all.
    assert "A complex task draws every other brain-trust member" in rule
    assert "a standard task one reviewer, and simple and rote tasks none" in rule
    assert "a `review` task always draws its counterpart reviewer" in rule


def test_the_readme_describes_a_changed_disagreement_as_recorded():
    from quadratus.runtime import changed_report

    # A disagreement is a returned report, never an exception.
    assert changed_report('Done.\nCHANGED: ["a.py"]', ["b.py"])["status"] == "undeclared"
    flat = _flat(README)
    assert "A mismatch stops the run" not in flat
    assert "does not stop the run" in flat and "unverified fact" in flat


def test_the_readme_says_sharing_is_refused_on_every_backend(monkeypatch):
    from quadratus.gui import resolve_share

    monkeypatch.setenv("QUADRATUS_ALLOW_SHARE", "1")
    for backend in ("cli", "api"):
        assert resolve_share(Settings(backend=backend, backend_overrides={})) is False
    flat = _flat(README)
    assert "Sharing is disabled while a CLI backend is active" not in flat
    assert "Public sharing is refused on every backend" in flat
    # The sentence that lost its verb.
    assert "as before. Conversation memory, file uploads" not in flat


def test_the_readme_states_how_far_the_monitor_page_reaches(tmp_path):
    """The served monitor page reads the run folder of whatever project its
    address names, with no sign-in. The README's section on what leaves the
    machine says so for as long as that is true, and must stop saying so when
    the server starts refusing other folders."""
    import http.client
    import threading
    from urllib.parse import urlencode

    from quadratus.monitor_server import MonitorServer

    launch, other = tmp_path / "launch", tmp_path / "other"
    launch.mkdir()
    other.mkdir()
    server = MonitorServer(0, project=str(launch))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
        connection.request("GET", "/status.json?" + urlencode({"project": str(other)}))
        response = connection.getresponse()
        body = response.read().decode("utf-8", "replace")
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
    try:
        shown = json.loads(body)["status"]["project"]
    except (ValueError, KeyError, TypeError):
        shown = None
    reaches_any_folder = response.status == 200 and isinstance(shown, str) and Path(shown) == other
    said = "any project folder a viewer types into it" in _flat(README)
    assert said == reaches_any_folder, (
        "the README's monitor paragraph and monitor_server disagree about whether a viewer can "
        f"read another project's status (README says so: {said}; the server does it: {reaches_any_folder}). "
        "Rewrite the README sentence that starts 'It has no sign-in' to say what the server does now.")


def test_the_readme_layout_names_what_each_engine_runs():
    assert "(current entry)" not in README
    assert "session engine (in progress)" not in README
    assert 'orchestrator.py     # Four-phase collaboration pipeline (a bare "<goal>")' in README
    assert "# --- session engine (what --project and --session run) ---" in README


#: The options cli.py defined when this was written. Options added later are
#: documented by the change that adds them; this list only guards these.
_OPTIONS_AT_REVIEW = (
    "--allow-writes", "--branch", "--capture-profile", "--check", "--claude-model", "--clone",
    "--decider", "--decider-labels", "--direct-tier", "--extra-check", "--forbid", "--grok-model",
    "--history", "--max-tasks", "--mode", "--monitor", "--neutral-preferences", "--openai-model",
    "--path", "--plan-gate", "--policy-preview", "--probe", "--probe-all", "--project",
    "--readiness", "--repair-test", "--repo", "--rounds", "--rulings", "--security-verdict-json",
    "--series", "--serve", "--session", "--show-stages", "--state-dir", "--status",
    "--survey-recovery", "--tasks", "--test-changes", "--verbose",
)


def _defined_options() -> set:
    found = set()
    for name in ("cli.py", "gui.py"):
        found |= set(re.findall(r"""["'](--[a-z][a-z-]+)["']""", _source(name)))
    return found


def test_every_option_the_cli_had_is_in_the_readme():
    defined = _defined_options()
    named = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", README))
    for option in _OPTIONS_AT_REVIEW:
        assert option in defined, f"{option} left the CLI; take it out of the README and this list"
        assert option in named, f"{option} exists and the README does not mention it"


#: Vendor CLI flags the README names only in its list of probes nobody has
#: made yet. They are another program's options, so no parser here has them.
_VENDOR_FLAGS_IN_PROBES = {
    "--strict-mcp-config", "--setting-sources", "--output-format", "--skip-git-repo-check",
    "--sandbox", "--system-prompt-file", "--mcp-config",
}
_PROBE_HEADING = "### Not yet probed against the real CLIs"


def test_the_readme_names_no_option_the_commands_lack():
    # --cask is brew's and --help is argparse's own.
    foreign = {"--cask", "--help"}
    # The two record readers have small parsers of their own.
    for tool in ("compare_runs.py", "scoreboard.py"):
        source = (ROOT / "tools" / tool).read_text(encoding="utf-8")
        foreign |= set(re.findall(r"""["'](--[a-z][a-z-]+)["']""", source))
    start = README.index(_PROBE_HEADING)
    end = README.index("\n## ", start)
    probes, rest = README[start:end], README[:start] + README[end:]
    option = r"(?<![\w-])--[a-z][a-z-]+"
    assert set(re.findall(option, rest)) - _defined_options() - foreign == set()
    # A vendor's flag belongs in the probe list and nowhere else.
    assert set(re.findall(option, probes)) - _defined_options() - foreign <= _VENDOR_FLAGS_IN_PROBES


# -- CI ----------------------------------------------------------------------

def _workflow() -> dict:
    import yaml

    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_ci_reads_only_and_runs_each_change_once():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    triggers = workflow[True] if True in workflow else workflow["on"]  # YAML reads a bare `on` as true
    assert triggers["push"] == {"branches": ["main"]}
    assert "pull_request" in triggers


def test_ci_actions_are_pinned_to_commits():
    uses = [step["uses"] for job in _workflow()["jobs"].values() for step in job["steps"] if "uses" in step]
    assert uses
    for action in uses:
        # An action may live in a folder of its repository (actions/cache/restore).
        assert re.fullmatch(r"[\w.-]+/[\w.-]+(?:/[\w.-]+)*@[0-9a-f]{40}", action), action


def test_ci_runs_the_tool_tests_and_checks_the_installed_wheel():
    jobs = _workflow()["jobs"]
    commands = "\n".join(step.get("run", "") for job in jobs.values() for step in job["steps"])
    for path in ("tools/jev_stage_b/test_pairs.py", "tools/jev_stage_b/test_graders_support.py",
                 "tools/jev_stage_b/test_collector_browser.py"):
        assert (ROOT / path).is_file() and path in commands
    wheel = "\n".join(step.get("run", "") for step in jobs["wheel"]["steps"])
    assert "build --wheel" in wheel and "-m venv" in wheel
    assert "quadratus --help" in wheel and "tools/wheel_smoke.py" in wheel
    assert (ROOT / "tools" / "wheel_smoke.py").is_file()


def test_the_readme_names_the_python_versions_ci_runs():
    versions = _workflow()["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    assert versions == ["3.11", "3.12", "3.13"]
    named = re.search(r"CI runs ruff and the test suite on Python ([0-9., and]+?) \(", _flat(README))
    assert named, "the README says which Python versions CI runs"
    assert re.findall(r"\d\.\d+", named.group(1)) == versions


# -- the installed-wheel check itself ---------------------------------------

sys.path.insert(0, str(ROOT))
from tools import wheel_smoke  # noqa: E402


@pytest.fixture(scope="module")
def installed_wheel(tmp_path_factory):
    """The wheel, built offline from a copy and installed outside the tree."""
    work = tmp_path_factory.mktemp("wheel")
    source = work / "source"
    source.mkdir()
    shutil.copytree(ROOT / "quadratus", source / "quadratus", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "README.md", "multi_model_workflow.py", "chat_gui.py"):
        shutil.copyfile(ROOT / name, source / name)
    wheels = work / "wheels"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index",
                    "--wheel-dir", str(wheels), str(source)], check=True, capture_output=True)
    target = work / "installed"
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "--target", str(target),
                    str(next(wheels.glob("quadratus*.whl")))], check=True, capture_output=True)
    shutil.rmtree(source)
    return target


def _smoke(target: Path, cwd: Path):
    done = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "wheel_smoke.py"), "--source", str(ROOT),
                           "--site", str(target), "--commands", "skip"],
                          capture_output=True, text=True, cwd=cwd)
    return done.returncode, json.loads(done.stdout)


def test_the_wheel_holds_the_package_data_pyproject_lists(installed_wheel, tmp_path):
    code, report = _smoke(installed_wheel, tmp_path)
    assert code == 0 and report["problems"] == []
    expected, problems = wheel_smoke.expected_package_data(ROOT)
    assert problems == [] and report["package_data_files"] == len(expected["quadratus"]) > 0
    assert Path(report["installed"]["quadratus"]) == (installed_wheel / "quadratus").resolve()
    assert sorted(report["entry_points"]) == ["quadratus", "quadratus-gui"]


def test_a_wheel_missing_a_data_file_fails_the_check(installed_wheel, tmp_path):
    expected, _ = wheel_smoke.expected_package_data(ROOT)
    victim = installed_wheel / "quadratus" / expected["quadratus"][0]
    kept = victim.read_bytes()
    try:
        victim.unlink()
        code, report = _smoke(installed_wheel, tmp_path)
        assert code == 1
        assert any(expected["quadratus"][0] in problem for problem in report["problems"])
        victim.write_bytes(kept + b"\n# changed after the build\n")
        code, report = _smoke(installed_wheel, tmp_path)
        assert code == 1 and any("differs from the checkout" in problem for problem in report["problems"])
    finally:
        victim.write_bytes(kept)


def test_the_checkout_never_counts_as_the_installed_package():
    import quadratus

    # Whatever folder this process imported quadratus from plays the checkout.
    checkout = Path(quadratus.__file__).resolve().parents[1]
    found, problems = wheel_smoke.installed_packages(checkout, ["quadratus"])
    assert found == {} and "imported from the checkout" in problems[0]


def test_a_package_data_pattern_that_matches_nothing_is_reported(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "kept.json").write_text("{}")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.setuptools.package-data]\npkg = ["*.json", "missing/*.md"]\n')
    expected, problems = wheel_smoke.expected_package_data(tmp_path)
    assert expected == {"pkg": ["kept.json"]}
    assert problems == ["package-data pattern pkg:missing/*.md matches no file in the checkout"]


def test_the_gui_help_reading_accepts_usage_or_a_clean_refusal_only():
    read = wheel_smoke.classify_gui_help
    assert read(0, "usage: quadratus-gui [-h] [--port N]", "") == wheel_smoke.GUI_HELP
    assert read(2, "", "Port 7860 on 127.0.0.1 is already in use.") == wheel_smoke.GUI_NO_HELP
    assert read(0, "", "") is None            # exited 0 and said nothing
    assert read(1, "usage: x", "Traceback") is None
    assert read(2, "", "error: unrecognized arguments") is None
    assert read(None, "", "") is None         # still running at the timeout


def test_the_gui_help_probe_never_lets_the_gui_serve():
    """Run against the real module. Today it has no --help and must exit on
    the busy port; once it parses --help it must print usage. Either way it
    returns, and nothing is left listening."""
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    outcome, detail = wheel_smoke.probe_gui_help([sys.executable, "-m", "quadratus.gui"], env=env, timeout=300)
    assert outcome in (wheel_smoke.GUI_HELP, wheel_smoke.GUI_NO_HELP), detail


def test_a_required_gui_help_fails_when_the_command_has_none(tmp_path):
    fake = tmp_path / "quadratus-gui"
    fake.write_text("#!/bin/sh\necho 'Port 1 on 127.0.0.1 is already in use.' >&2\nexit 2\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    results, problems = wheel_smoke.check_commands(tmp_path, ["quadratus-gui"], gui_help="required", cwd=str(tmp_path))
    assert results == {"quadratus-gui": wheel_smoke.GUI_NO_HELP} and len(problems) == 1
    results, problems = wheel_smoke.check_commands(tmp_path, ["quadratus-gui"], gui_help="if-supported",
                                                   cwd=str(tmp_path))
    assert problems == []
    results, problems = wheel_smoke.check_commands(tmp_path, ["quadratus"], gui_help="required", cwd=str(tmp_path))
    assert problems == [f"{tmp_path / 'quadratus'} is not installed"]


# -- the acceptance image recipe ---------------------------------------------

DOCKERFILE = ROOT / "tools" / "acceptance" / "Dockerfile"
CONSTRAINTS = ROOT / "tools" / "acceptance" / "constraints.txt"


def _instructions() -> list:
    """(keyword, text) per Dockerfile instruction, comments dropped."""
    out = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        out.append((keyword, rest))
    return out


def _grok_steps() -> list:
    return [text for keyword, text in _instructions() if keyword == "RUN" and "GROK_SHA256" in text]


def _run_step(step: str, tmp_path: Path, digest):
    """One RUN line as docker would run it, with curl and the paths stubbed."""
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    curl = stubs / "curl"
    # The stub writes fixed bytes to the -o path and never touches a network.
    curl.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do if [ "$1" = "-o" ]; then out="$2"; fi; shift; done\n'
                    'printf "grok build bytes" > "$out"\n')
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    step = step.replace("/tmp/grok-download", str(tmp_path / "grok-download"))
    step = step.replace("/usr/local/bin/grok", str(tmp_path / "bin-grok"))
    env = {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}
    if digest is not None:
        env["GROK_SHA256"] = digest
    return subprocess.run(["sh", "-c", step], capture_output=True, text=True, env=env)


def test_the_grok_digest_is_a_build_argument_with_no_default():
    args = [text for keyword, text in _instructions() if keyword == "ARG"]
    assert args == ["GROK_SHA256"], "declared once, with no default value"
    order = [keyword for keyword, text in _instructions() if "GROK_SHA256" in text]
    assert order[0] == "ARG" and len(_grok_steps()) == 2
    # The guard is the first RUN, so the build stops before the slow layers.
    assert next(text for keyword, text in _instructions() if keyword == "RUN") == _grok_steps()[0]


@pytest.mark.parametrize("digest", [None, "", "abc123", "Z" * 64, "A" * 64])
def test_the_build_stops_without_a_usable_digest(tmp_path, digest):
    guard = _grok_steps()[0]
    done = _run_step(guard, tmp_path, digest)
    assert done.returncode != 0
    assert "--build-arg GROK_SHA256" in done.stderr


# The image is Linux, where sha256sum always exists; a development machine
# without it cannot run the step, which says nothing about the recipe.
@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs sha256sum, as the image has")
def test_a_download_that_does_not_match_is_never_installed(tmp_path):
    import hashlib

    guard, download = _grok_steps()
    good = hashlib.sha256(b"grok build bytes").hexdigest()
    wrong = hashlib.sha256(b"another file").hexdigest()

    assert _run_step(guard, tmp_path, wrong).returncode == 0   # well formed, so the guard lets it by
    refused = _run_step(download, tmp_path, wrong)
    assert refused.returncode != 0
    assert not (tmp_path / "bin-grok").exists()

    assert _run_step(guard, tmp_path, good).returncode == 0
    accepted = _run_step(download, tmp_path, good)
    assert accepted.returncode == 0, accepted.stderr
    installed = tmp_path / "bin-grok"
    assert installed.read_bytes() == b"grok build bytes" and os.access(installed, os.X_OK)
    assert not (tmp_path / "grok-download").exists()


def test_every_pip_install_in_the_image_is_pinned_to_the_record():
    record = json.loads((ROOT / "docs" / "collaboration" / "2026-09-blind-acceptance" / "evidence"
                         / "application-preflight.json").read_text(encoding="utf-8"))
    pins = {}
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, _, version = line.partition("==")
            pins[name.lower()] = version
    for line in record["installed_python"]:
        name, _, version = line.partition("==")
        assert pins.get(name.lower()) == version, line
    # The one pin the freeze predates, from the harness-canary record.
    assert pins["jsonschema"] == "4.26.0"
    assert "jsonschema==4.26.0" in (ROOT / "docs" / "harness-canary" / "README.md").read_text(encoding="utf-8")
    recorded = {line.partition("==")[0].lower() for line in record["installed_python"]}
    assert set(pins) == recorded | {"jsonschema"}

    installs = [text for keyword, text in _instructions() if keyword == "RUN" and text.startswith("pip install")]
    assert installs
    assert ("COPY", "constraints.txt /tmp/constraints.txt") in _instructions()
    for text in installs:
        words = text.split()
        assert "-c" in words and words[words.index("-c") + 1] == "/tmp/constraints.txt"
        packages = [w for w in words[2:] if not w.startswith("-") and w != "/tmp/constraints.txt"]
        assert packages
        for requirement in packages:
            name, sep, version = requirement.partition("==")
            assert sep and pins[name.lower()] == version, requirement
