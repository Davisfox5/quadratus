"""Cycle21 units (PR73 6751283026, reconciled by root).

A. The role packet's size refusal is typed and measured by the one builder
   dispatch uses; intake asks it before any charge; the prompt states the
   limit this run enforces; a backstop before admission undoes the dispatch
   marks.
B. A SCOPE path that differs only in letter case from an existing path is
   found from directory listings as stored, on any host.
C. A parenthetical that only labels the command ("configured unittest
   check") is not a second claim; any other word still is.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadratus import policy as P
from quadratus import repo_scan
from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.policy import PacketTooLarge, PolicyError, load_policy
from quadratus.scope import TaskScope, read_scope
from quadratus.session import Session, SessionConfig, TaskSpec, _case_collision

SAVED = (Path(__file__).parent / "lifecycle" / "cycle20_t8_scope.json").read_text()


def _project(tmp_path, files=("README.md", "tests/test_cli_workflows.py", "store.py")):
    root = tmp_path / "p"
    for name in files:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x\n")
    return root


def _scope(data):
    return read_scope("KIND: test simple\nSCOPE: " + data + "\nbody")[0]


def _session(root, **config):
    return Session("g", ArtifactStore(root.parent / "a"), lambda *a, **k: "",
                   config=SessionConfig(project=root, allow_writes=True, requirements_ledger=True,
                                        repository_policy=load_policy(root), **config))


def _spec(scope, task_id="t1"):
    return TaskSpec(task_id=task_id, description="d", kind="backend", complexity="simple", scope=scope)


# -- A: the typed, measured refusal ---------------------------------------------------------------

def test_the_saved_t8_scope_is_refused_typed_with_its_measured_parts(tmp_path):
    policy = load_policy(_project(tmp_path))
    scope = policy.scope(_scope(SAVED))
    with pytest.raises(PacketTooLarge) as info:
        policy.role_packet(scope, "lead")
    exc = info.value
    assert str(exc) == "Required role packet exceeds 12000 bytes; narrow the task", "the message is unchanged"
    assert isinstance(exc, PolicyError) and exc.limit == 12_000 and exc.needed > 12_000
    assert exc.components["scope"] == 10_825 and exc.components["proofs"] == 7_953
    assert exc.components["scope"] + exc.components["fixed"] == exc.needed


def test_a_scope_that_fits_keeps_every_mandatory_word(tmp_path):
    policy = load_policy(_project(tmp_path))
    data = json.loads(SAVED)
    data["proofs"] = data["proofs"][:2]
    scope = policy.scope(_scope(json.dumps(data)))
    packet = policy.role_packet(scope, "lead")
    assert scope.render() in packet, "the whole scope, exclusions included, is in the packet"
    for family in policy.resolve(scope.permitted_paths)["families"]:
        for check in policy.cards[family]["checklist"]:
            if check.get("id") in P._IO_CHECKS:
                continue  # narrowed to the scope's own I/O by design, and says so
            if "lead" in check.get("applies_to", ["lead", "reviewer", "verifier"]):
                assert check["because"] in packet


def test_bytes_are_utf8_exclusions_are_exempt_and_a_custom_limit_holds(tmp_path):
    policy = load_policy(_project(tmp_path))
    base = dict(permitted_paths=["store.py"], intended_result="r", acceptance=["a"], max_lines=10)
    room = policy.packet_limit - policy.packet_overhead("lead")
    fits = TaskScope(**{**base, "intended_result": "é" * 10})
    over = len(fits.render(include_forbidden=False).encode()) - len(fits.render(include_forbidden=False))
    assert over == 10, "each é counts two bytes"
    many = TaskScope(**base, forbidden_paths=tuple(f"vendor/{i:05d}/" for i in range(3000)))
    policy.role_packet(policy.scope(many), "lead")  # thousands of exclusions do not count
    policy.document.setdefault("context", {})["reference_bytes_limit"] = 4_000
    assert policy.packet_limit == 4_000
    big = TaskScope(**{**base, "intended_result": "x" * (room - 2_000)})
    with pytest.raises(PacketTooLarge) as info:
        policy.role_packet(policy.scope(big), "lead")
    assert info.value.limit == 4_000


def test_the_measured_overhead_is_the_builders_own_fixed_part(tmp_path):
    policy = load_policy(_project(tmp_path))
    scope = policy.scope(TaskScope(permitted_paths=["store.py"], intended_result="x" * 12_000, acceptance=["a"],
                                   max_lines=10))
    with pytest.raises(PacketTooLarge) as info:
        policy.role_packet(scope, "lead")
    assert info.value.components["fixed"] == policy.packet_overhead("lead") or \
        policy.resolve(["store.py"])["families"] != policy.resolve(())["families"]


def test_intake_and_dispatch_agree_at_the_boundary(tmp_path):
    root = _project(tmp_path)
    session = _session(root)
    policy = session.config.repository_policy
    room = policy.packet_limit - policy.packet_overhead("lead")
    base = TaskScope(permitted_paths=["store.py"], intended_result="", acceptance=["a"], max_lines=10)
    pad = room - len(policy.scope(base).render(include_forbidden=False).encode())
    for extra, refused in ((-40, False), (pad + 400, True)):
        scope = TaskScope(permitted_paths=["store.py"], intended_result="y" * max(1, pad + extra) if extra > 0
                          else "y" * max(1, pad + extra), acceptance=["a"], max_lines=10)
        problem = session._packet_problem(_spec(scope))
        try:
            session._prepare_dispatch(_spec(scope, task_id=f"t{extra}"))
            dispatched = True
        except PacketTooLarge:
            dispatched = False
        assert bool(problem) is refused and dispatched is (not refused), (extra, problem)


def test_another_policy_refusal_is_left_to_dispatch(tmp_path, monkeypatch):
    """Retained negative: a blocked plan or an operator path limit keeps its
    existing stop; intake reports only the typed size refusal."""
    root = _project(tmp_path)
    session = _session(root)
    policy = session.config.repository_policy
    monkeypatch.setattr(type(policy), "role_packet", lambda self, *a, **k: (_ for _ in ()).throw(PolicyError("no")))
    assert session._packet_problem(_spec(TaskScope(permitted_paths=["store.py"], acceptance=["a"]))) == ""
    real = policy.resolve

    def blocked(paths=(), **kw):
        return dict(real(paths, **kw), blocked=["store.py is denied"])
    monkeypatch.setattr(policy, "resolve", blocked)
    with pytest.raises(PolicyError) as info:
        session._prepare_dispatch(_spec(TaskScope(permitted_paths=["store.py"], acceptance=["a"])))
    assert not isinstance(info.value, PacketTooLarge)


def test_the_backstop_undoes_only_the_marks_of_a_task_that_never_began(tmp_path):
    session = _session(_project(tmp_path))
    session.config.survey = SimpleNamespace(recovery_tasks=2)
    session.survey = dict(recovery_used=1)
    session._explicit_tasks = ["one"]
    session.explicit = dict(ran=[dict(index=0, task="t0"), dict(index=1, task="t1")])
    session.unexecuted_acceptance = [dict(oid="O1", taken_over_by=[dict(task="t0"), dict(task="t1")])]
    decision = dict(charged=True)
    session._unmark_not_started(_spec(TaskScope()), decision, why="Required role packet exceeds")
    assert session.survey["recovery_used"] == 0 and decision == dict(charged=False, not_started=True)
    assert session.explicit["ran"] == [dict(index=0, task="t0")]
    assert session.unexecuted_acceptance[0]["taken_over_by"] == [dict(task="t0")]
    assert session.not_started[-1]["task"] == "t1"


def test_the_scope_request_states_this_runs_limit_and_overhead(tmp_path):
    session = _session(_project(tmp_path))
    policy = session.config.repository_policy
    text = session._scope_request()
    assert f"whose limit is {policy.packet_limit:,} bytes" in text
    assert f"already take about {policy.packet_overhead('lead'):,}" in text
    assert "well under 20,000" not in text and "sent back with its measured size" in text
    policy.document.setdefault("context", {})["reference_bytes_limit"] = 9_000
    assert "whose limit is 9,000 bytes" in session._scope_request()
    bare = Session("g", ArtifactStore(tmp_path / "b"), lambda *a, **k: "", config=SessionConfig())
    assert f"{S._FALLBACK_PACKET_BYTES:,} bytes" in bare._scope_request()


# -- B: exact path spellings ------------------------------------------------------------------------

@pytest.mark.parametrize("declared, found", [
    ("readme.md", "README.md"), ("README.md", None), ("CHANGES.md", None),
    ("Tests/test_cli_workflows.py", "tests/test_cli_workflows.py"), ("tests/Test_cli_workflows.py",
                                                                      "tests/Test_cli_workflows.py"),
    ("TESTS/", "tests"), ("tests/*.PY", None), ("Tests/*.py", "tests/*.py"), ("tests/new_test.py", None),
])
def test_case_only_collisions_are_found_from_stored_names(tmp_path, declared, found):
    root = _project(tmp_path)
    expected = found
    if declared == "tests/Test_cli_workflows.py":
        expected = "tests/test_cli_workflows.py"
    assert _case_collision(root, declared) == expected


def test_the_comparison_never_asks_the_filesystem_to_resolve_a_spelling(tmp_path, monkeypatch):
    """A case-insensitive host resolves readme.md; the listing still says
    README.md, and that is all that is read."""
    root = _project(tmp_path)
    asked, real = [], Path.exists
    monkeypatch.setattr(Path, "exists", lambda self, *a, **k: asked.append(str(self)) or real(self, *a, **k))
    found = _case_collision(root, "readme.md")
    monkeypatch.undo()
    assert found == "README.md" and not [p for p in asked if p.startswith(str(root))]


def test_links_and_ambiguity_stop_the_walk(tmp_path):
    root = _project(tmp_path)
    (root / "linked").symlink_to(root / "tests", target_is_directory=True)
    assert _case_collision(root, "linked/Test_cli_workflows.py") is None
    if not (root / "Readme.MD").exists():
        (root / "readme.md").write_text("y\n")
        assert _case_collision(root, "ReadMe.md") is None, "two spellings: ambiguous, nothing named"


def test_a_spelling_the_task_may_not_name_is_not_disclosed(tmp_path):
    root = _project(tmp_path, files=("README.md", "secrets/Key.txt", "store.py"))
    session = _session(root, project_excludes=("secrets/",))
    problem = session._path_case_problem(_spec(TaskScope(permitted_paths=["secrets/key.txt"], acceptance=["a"])))
    assert "may not name" in problem and "Key.txt" not in problem
    named = session._path_case_problem(_spec(TaskScope(permitted_paths=["readme.md"], acceptance=["a"])))
    assert "`readme.md` differs only in letter case from the existing `README.md`" in named
    assert session._path_case_problem(_spec(TaskScope(permitted_paths=["README.md", "NEW.md"],
                                                      acceptance=["a"]))) == ""


@pytest.mark.parametrize("stored", ["README.md", "readme.md", "Readme.rst"])
def test_the_scan_names_the_readme_as_stored(tmp_path, stored):
    root = tmp_path / "r"
    root.mkdir()
    (root / stored).write_text("# app\nline\n")
    (root / "app.py").write_text("x = 1\n")
    report = repo_scan.scan_repo(root)
    assert report.readme_name == stored and f"{stored} begins:" in report.summary()


# -- C: a label is not a claim ------------------------------------------------------------------------

R23 = "python3 -m unittest discover -s tests passes on the delivered source (configured unittest check)."


def _reader(tmp_path, text):
    session = Session("g", ArtifactStore(tmp_path / "c"), lambda *a, **k: "",
                      config=SessionConfig(project=tmp_path, allow_writes=True, requirements_ledger=True))
    session.memory.ledger.requirements["R23"] = text
    return session


@pytest.mark.parametrize("text, pure", [
    (R23, True),
    ("`pytest -q` passes, including every test in `tests/test_existing.py` (the required test command)", True),
    ("python3 -m unittest discover -s tests passes (configured database check).", False),
    ("pytest -q passes (configured pytest check) and refunds are atomic", False),
    ("pytest -k refunds passes (configured pytest check)", False),
    ("python3 -m unittest discover -s tests passes (configured pytest check).", False),
    (R23.replace(").", ") (configured unittest check)."), False),
])
def test_only_a_label_of_the_same_command_is_not_a_claim(tmp_path, text, pure):
    session = _reader(tmp_path, text)
    assert session._test_command_requirement("R23") is pure
    if pure:
        assert session._test_command_extra_words("R23") == [], "detection and the correction agree"


def test_r23_alone_needs_no_scenario_and_a_mixed_label_still_does(tmp_path):
    session = _reader(tmp_path, R23)
    spec = _spec(TaskScope(permitted_paths=["tests/test_x.py"], acceptance=["a"]))
    assert session._proof_problem(spec, ["R23"]) == ""
    session.memory.ledger.requirements["R23"] = R23.replace("unittest check", "database check")
    problem = session._proof_problem(spec, ["R23"])
    assert "declares no proof scenarios" in problem


def test_os_listdir_is_the_only_spelling_source(tmp_path, monkeypatch):
    root = _project(tmp_path)
    seen = []
    real = os.listdir
    monkeypatch.setattr(os, "listdir", lambda p: seen.append(str(p)) or real(p))
    assert _case_collision(root, "Tests/x.py") == "tests/x.py" and seen == [str(root)]
