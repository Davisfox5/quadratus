"""Cycle15 (PR73 6734704072, root's reconciliation): a headless permission
block is a typed denial, and a restricted seat is told which local readers it
already has.

cycle14 t5: grok:worker, a restricted editing seat in a source copy, opened
README.md through Fetch on a file:// URL; the CLI printed

    permission required: Fetch: file:///.../README.md was not run, as a
    headless run cannot ask for approval

and the turn came back cancelled with nothing changed. The record called it
transport and said its files were kept. These controls pin, at the provider,
the session and the record:

- only grok's exact line becomes ``PermissionBlocked`` with bounded metadata
  (tool, scheme, a target digest; never the raw path), on the invocation
  row, the task fact and the failed handoff;
- it is never retried, never handed to a fallback lead, a worker or another
  tool, in a batch or serially; refusals, caps and authentication keep their
  own types and precedence;
- the unmerged line says "it changed no files" when that is what happened;
- the restricted grok copy prompt names read_file, grep and list_dir (all in
  its allowlist), and its argv is unchanged.

Real Session and controller with scripted CLI processes; no provider runs."""
import json
import subprocess

import pytest

from quadratus import cli_providers, task_kinds
from quadratus.cli_providers import GROK_SPEC, _extract_grok_permission_block
from quadratus.delegation import safe_diagnostics
from quadratus.providers import PermissionBlocked, ProviderError, ProviderRefusal, TurnLimitReached
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script

#: cycle14 t5's saved stderr line, exactly.
TARGET = "file:///private/var/folders/_n/10qf_9vn1c5_1gccy0h9hxyr0000gn/T/quadratus-review-ibs7xarx/README.md"
LINE = f"permission required: Fetch: {TARGET} was not run, as a headless run cannot ask for approval"
HTTPS = ("permission required: web_fetch: https://example.invalid/spec was not run, "
         "as a headless run cannot ask for approval")
NARRATION = "The README already states the legacy surface. I'll replace the deferred-feature sentence"


def blocked(line=LINE, *, stop="cancelled", returncode=0, num_turns=1, error=None):
    return subprocess.CompletedProcess([], returncode, H.grok_ok(NARRATION, stop=stop, num_turns=num_turns,
                                                                 error=error), line + "\n")


# -- 1. the parser: the exact line only ----------------------------------------------------------

def test_the_exact_saved_line_is_a_bounded_block_without_the_raw_target():
    block = _extract_grok_permission_block("some log\n" + LINE + "\n")
    assert block == dict(tool="Fetch", scheme="file", headless_approval=True,
                         target_digest=block["target_digest"], lines=1)
    assert len(block["target_digest"]) == 16 and "private" not in json.dumps(block)
    assert _extract_grok_permission_block(HTTPS)["scheme"] == "https"
    bare = LINE.replace(TARGET, "README.md")
    assert _extract_grok_permission_block(bare)["scheme"] == "path"
    assert safe_diagnostics(dict(permission_block=block))["permission_block"] == block


@pytest.mark.parametrize("text", [
    LINE + ".",                                                     # trailing text
    LINE.replace("permission required", "Permission required"),     # other case
    "error: " + LINE,                                               # prefixed
    LINE.replace(", as a headless run cannot ask for approval", ""),  # no headless clause
    LINE.replace("Fetch: ", "Fetch "),                              # no tool separator
    "permission required: Fetch:  was not run, as a headless run cannot ask for approval",  # empty target
    "",
])
def test_a_near_miss_is_no_block(text):
    assert _extract_grok_permission_block(text) is None


def test_the_ledger_whitelist_drops_a_malformed_block():
    bad = dict(tool="Fetch", scheme="file", headless_approval=True, target_digest=TARGET)
    assert "permission_block" not in safe_diagnostics(dict(permission_block=bad))


# -- 2. the provider boundary --------------------------------------------------------------------

def _grok(monkeypatch, tmp_path, result):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    monkeypatch.setattr(cli_providers, "_launch", lambda argv, **kw: subprocess.CompletedProcess(
        argv, result.returncode, result.stdout, result.stderr))
    base = cli_providers.GrokCLIProvider(model="", workdir=str(tmp_path))
    return base.for_seat("", effort="low", restricted=True)


@pytest.mark.parametrize("returncode", [0, 1])
def test_a_cancelled_turn_with_the_line_raises_the_typed_denial(monkeypatch, tmp_path, returncode):
    seat = _grok(monkeypatch, tmp_path, blocked(returncode=returncode))
    with pytest.raises(PermissionBlocked) as caught:
        seat.generate("Edit README.md.")
    exc = caught.value
    assert exc.permission_block["tool"] == "Fetch" and exc.permission_block["scheme"] == "file"
    assert TARGET not in str(exc) and "private/var" not in str(exc), "the raw target stays in stderr"
    assert exc.diagnostics["permission_block"] == exc.permission_block
    assert LINE in seat.last_stderr, "the private evidence is kept as it was"


def test_without_the_line_a_cancelled_turn_stays_an_ordinary_provider_error(monkeypatch, tmp_path):
    seat = _grok(monkeypatch, tmp_path, blocked(line="something else"))
    with pytest.raises(ProviderError) as caught:
        seat.generate("Edit README.md.")
    assert not isinstance(caught.value, PermissionBlocked)


@pytest.mark.parametrize("stop, error, expected", [
    ("refusal", None, ProviderRefusal),
    ("max_turn_requests", None, TurnLimitReached),
])
def test_a_refusal_or_a_cap_keeps_its_own_type_beside_the_line(monkeypatch, tmp_path, stop, error, expected):
    seat = _grok(monkeypatch, tmp_path, blocked(stop=stop, error=error, num_turns=8))
    with pytest.raises(expected) as caught:
        seat.generate("Edit README.md.")
    assert not isinstance(caught.value, PermissionBlocked)


def test_the_restricted_argv_is_unchanged(monkeypatch, tmp_path):
    seat = _grok(monkeypatch, tmp_path, blocked())
    argv = seat._build_argv("the prompt", "the role")
    tools = argv[argv.index("--tools") + 1]
    assert tools == "read_file,grep,list_dir,web_search,web_fetch"
    assert "--always-approve" not in argv and argv[argv.index("--disallowed-tools") + 1] == "Agent"


# -- 3. the prompt: the readers the seat already has ----------------------------------------------

def test_the_restricted_grok_copy_prompt_names_only_its_own_readers(monkeypatch, tmp_path):
    from quadratus.runtime import local_read_rule
    seat = _grok(monkeypatch, tmp_path, blocked())
    rule = local_read_rule(seat)
    allowed = GROK_SPEC.restricted_args[GROK_SPEC.restricted_args.index("--tools") + 1].split(",")
    assert set(GROK_SPEC.local_read_tools) <= set(allowed)
    assert "read_file, grep or list_dir" in rule and "file:// URL" in rule
    assert "do not retry it with another tool or path" in rule
    unrestricted = cli_providers.GrokCLIProvider(model="", workdir=str(tmp_path))
    assert local_read_rule(unrestricted) == ""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    claude = cli_providers.ClaudeCLIProvider(model="haiku", workdir=str(tmp_path)).for_seat("haiku", restricted=True)
    assert local_read_rule(claude) == "", "other vendors unchanged"


# -- 4. lifecycle: batch and serial --------------------------------------------------------------

REQS = ("REQUIREMENTS:\nR1: add(a, b) returns the sum of two numbers\n"
        "R2: README documents add and states what it returns\n")


def _block(kind, path, text, covers):
    scope = json.dumps({"permitted_paths": [path], "intended_result": f"{path} written",
                        "acceptance": [f"{path} meets the task"], "max_lines": 20,
                        "proofs": H.proofs_for(covers, "tests/test_app.py" if path == "app.py" else path,
                                               evidence="reading" if path == "README.md" else None)})
    return f"KIND: {kind}\nSCOPE: {scope}\n{text}\nCOVERS: {covers}"


README = _block("docs rote", "README.md", "Document add in README.md.", "R2")
BATCH = (REQS + "PARALLEL\n" + _block("backend simple", "app.py", "Implement add in app.py.", "R1")
         + "\n---\n" + README)
CLEAN = "NO FINDINGS\nR1: MET - app.py\nR2: MET - README.md\nREVIEW: COMPLETE"


def _script(plan, line=LINE):
    replies = iter([plan])

    def lead(call, replay):
        if call.vendor == "grok":
            return blocked(line)
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'
    return Script(orchestrator=lambda c, r: next(replies, "DONE"), lead=lead, collaborator=lambda c, r: CLEAN,
                  auditor=lambda c, r: "R1: MET - app.py\nR2: MET - README.md",
                  **{"requirements-review": lambda c, r: "COMPLETE"})


def _ledger(replay):
    from pathlib import Path
    rows = [json.loads(line) for line in (Path(replay.result.run_dir) / "invocations.jsonl").read_text().splitlines()]
    return [r for r in rows if r.get("invoked")]


def _rote_to_grok_worker(monkeypatch):
    monkeypatch.setitem(task_kinds.DIFFICULTY_LADDER, "rote", "grok:worker")


@pytest.mark.requirements_ledger
def test_a_batch_child_blocked_on_a_permission_is_a_denial_with_no_retry_and_no_merge(tmp_path, monkeypatch):
    _rote_to_grok_worker(monkeypatch)
    replay = H.run(tmp_path, monkeypatch, _script(BATCH), files=FILES, max_tasks=3, record_complete=False)
    result = H.result_json(replay)
    grok = [c for c in replay.calls if c.vendor == "grok"]
    assert len(grok) == 1 and grok[0].role == "lead", [(c.role, c.vendor) for c in replay.calls]
    t5 = grok[0].task
    assert "read_file, grep or list_dir" in grok[0].prompt, "the copy prompt names the seat's readers"
    assert [c.task for c in replay.of("lead")].count(t5) == 1, "no fallback lead for the blocked child"
    assert (replay.project / "README.md").read_text() == FILES["README.md"], "nothing merged"
    from pathlib import Path

    from quadratus import checkpoint
    state = checkpoint._decode(checkpoint.load(Path(replay.result.run_dir))["state"])
    record = state["failed_records"][t5]
    assert TARGET not in json.dumps(state, default=str), "the raw target is in no carried record"
    assert record["cause"] == "denial" and record["changed"] == []
    assert record["permission_block"]["tool"] == "Fetch" and record["permission_block"]["scheme"] == "file"
    assert record["error"].endswith("it changed no files")
    line = [f for f in result["open_findings"] if f.startswith(f"Parallel task {t5} was not merged")]
    assert line and line[0].endswith("it changed no files.") and "kept in artifact" not in line[0]
    assert TARGET not in json.dumps(result), "the raw target is in no record"
    row = [r for r in _ledger(replay) if r.get("task") == t5 and r.get("role") == "lead"][-1]
    assert row["diagnostics"]["permission_block"]["tool"] == "Fetch"
    assert not any(r.get("task") == t5 for r in result.get("recoveries") or []), "no hand-back of the denial"
    assert not any(r.get("decision", "").startswith("hand_back") for r in result.get("recoveries") or [])
    assert (replay.project / "app.py").read_text() == FIXED, "the clean sibling merged as before"
    assert not replay.result.completed


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("line, scheme", [(LINE, "file"), (HTTPS, "https")], ids=["file", "https"])
def test_an_unpinned_serial_lead_blocked_on_a_permission_stops_with_no_fallback(tmp_path, monkeypatch, line, scheme):
    _rote_to_grok_worker(monkeypatch)
    replay = H.run(tmp_path, monkeypatch, _script(REQS + README, line), files=FILES, max_tasks=3,
                   record_complete=False)
    leads = replay.of("lead")
    assert [c.vendor for c in leads] == ["grok"], "one call; no fallback to the next rung"
    assert "PermissionBlocked" in (replay.result.error or ""), replay.result.error
    result = H.result_json(replay)
    (terminal,) = [r for r in result.get("recoveries") or [] if r.get("kind") == "run"]
    assert terminal["outcome_class"] == "permission" and terminal["decision"].startswith("stop"), terminal
    row = [r for r in _ledger(replay) if r.get("role") == "lead"][-1]
    assert row["diagnostics"]["permission_block"]["scheme"] == scheme
    assert (replay.project / "README.md").read_text() == FILES["README.md"]
    assert len(replay.of("orchestrator")) == 1, "no planning call after the denial"


APP = REQS.split("R2:")[0] + _block("backend simple", "app.py", "Implement add in app.py.", "R1")


@pytest.mark.requirements_ledger
def test_a_worker_blocked_on_a_permission_stops_and_is_never_reissued(tmp_path, monkeypatch):
    """A lookup errand on grok:worker hits the same headless block: the lead
    is not handed the failure to reissue on another worker or tool."""
    asked = []

    def lead(call, replay):
        asked.append(call)
        if len(asked) == 1:
            return 'WORKER {"errand":"lookup","instruction":"Read README.md and report its first line."}'
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    def worker(call, replay):
        return blocked()
    script = Script(orchestrator=lambda c, r: APP if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                    collaborator=lambda c, r: CLEAN, auditor=lambda c, r: "R1: MET - app.py",
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    script._worker = worker
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    workers = [c for c in replay.calls if c.role.startswith("worker")]
    assert len(workers) == 1 and workers[0].vendor == "grok", [(c.role, c.vendor) for c in replay.calls]
    assert len(asked) == 1, "the lead was not re-asked to work around the denial"
    assert "PermissionBlocked" in (replay.result.error or ""), replay.result.error
    (terminal,) = [r for r in H.result_json(replay).get("recoveries") or [] if r.get("kind") == "run"]
    assert terminal["outcome_class"] == "permission"


@pytest.mark.parametrize("make, expected", [
    (lambda: PermissionBlocked("blocked", permission_block=dict(tool="Fetch")), "permission"),
    (lambda: _caused(_partial(), PermissionBlocked("blocked")), "uncertain_write"),
    (lambda: _caused(_tree_changed(), PermissionBlocked("blocked")), "integrity"),
    (lambda: ProviderRefusal("declined"), "permission"),
    (lambda: ProviderError("grok did not complete the turn (stopReason 'cancelled')."), "provider_error"),
])
def test_the_terminal_record_keeps_integrity_first_and_names_the_denial(tmp_path, make, expected):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig())
    session._record_terminal_recovery(make())
    assert session.recoveries[-1]["outcome_class"] == expected


def _caused(outer, inner):
    outer.__cause__ = inner
    return outer


def _partial():
    from quadratus.session import PartialWorkStopped
    return PartialWorkStopped("could not inspect")


def _tree_changed():
    from quadratus.deptree import DependencyTreeChanged
    return DependencyTreeChanged("tree moved")
