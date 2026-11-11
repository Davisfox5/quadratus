"""Repairs to the session engine from the final review of the 1110 batch.

Each test fails without its fix. Offline: every model call is answered by the
scripted invoke below, never a vendor CLI.

1. Undeclared design work is decided from the changed file's own type. A
   folder name alone (views, pages, static) no longer ends a finished task,
   and a test or test-support file is never design work.
2. An operator list's claims are read by the reader the task's own record
   uses, so a requirement a listed task claims is always audited.
3. "The same task twice in a row" compares the words together with the
   scope's paths, and a batch running one task at a time is never compared
   with itself.
4. Paths are compared through ``_scope_path``: a dot-folder keeps its dot.
5. A contest is no way around the routed lead's refusal (already repaired
   before this change; pinned here from the finding's own repro).
"""
import ast
import inspect
import json
import threading
from pathlib import Path

import pytest

from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.providers import ProviderRefusal
from quadratus.scope import TaskScope
from quadratus.session import RunStalled, Session, SessionConfig, TaskSpec
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import covered_met

OPUS = "claude:opus"
CALC = "def add(a, b):\n    return a + b\n"


def _task(label="backend standard", paths=("calc.py",), text="Add sub(a, b) to calc.py", extra_lines=()):
    scope = json.dumps({"permitted_paths": list(paths), "intended_result": "the change is made",
                        "acceptance": ["it works"], "max_lines": 40})
    return "\n".join([f"KIND: {label}", f"SCOPE: {scope}", *extra_lines, text])


class Script:
    """A scripted orchestrator, lead, reviewers and auditor. ``act`` is the
    lead: ``act(model, prompt, root)`` writes its files and returns its reply."""

    def __init__(self, plan, act, *, audit=None):
        self.plan, self.act, self.audit = list(plan), act, audit
        self.calls, self.prompts, self.lock = [], [], threading.RLock()

    @staticmethod
    def kind(prompt):
        for marker, kind in (("Name the single next task", "orchestrator"), ("The task is finished", "closeout"),
                             ("## Draft A", "judge"), ("You are an independent auditor", "audit"),
                             ("Compare the list with the goal", "reqreview"), ("You are leading", "lead")):
            if marker in prompt:
                return kind
        return "review"

    def invoke_for(self, root):
        root = Path(root).resolve()

        def invoke(model, prompt, system=None, allow_writes=False):
            kind = self.kind(prompt)
            with self.lock:
                self.calls.append((model, kind))
                self.prompts.append((kind, prompt))
            if kind == "orchestrator":
                return self.plan.pop(0) if self.plan else "DONE"
            if kind == "closeout":
                return "SUMMARY: done\nDECISIONS: none recorded\nDEAD ENDS: none"
            if kind == "judge":
                return "x\nWINNER: A"
            if kind == "audit":
                return self.audit(prompt)
            if kind == "reqreview":
                return "COMPLETE"
            if kind == "lead":
                return self.act(model, prompt, root)
            # A review of work that covers requirements owes a MET line for each.
            return ("NO FINDINGS\n" + covered_met(prompt)).strip()
        return invoke

    def of(self, kind):
        return [call for call in self.calls if call[1] == kind]


def _session(tmp_path, script, *, files=None, **config):
    project = (tmp_path / "project").resolve()
    for name, text in (files or {"calc.py": CALC}).items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text)
    config.setdefault("requirements_ledger", False)
    conf = SessionConfig(project=project, allow_writes=True,
                         fork=lambda root: (script.invoke_for(root), (lambda: None)), **config)
    return Session("Do the goal", ArtifactStore(tmp_path / "artifacts"), script.invoke_for(project),
                   config=conf), project


def _writes(files):
    """A lead that writes ``files`` (path -> text) and declares them."""
    def act(model, prompt, root):
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        return "Did it.\nCHANGED: " + json.dumps(sorted(files))
    return act


# -- 1. undeclared design work is the changed file's own type -------------------------------------

@pytest.mark.parametrize("path", [
    "app/views/user.py",            # Django: a folder named views
    "pages/api/users.ts",           # Next.js: an API route under pages
    "app/static/app.js",
    "components/registry.py",
    "tests/pages/login.py",         # a page object
    "tests/components/button.test.tsx",
    "tests/fixtures/page.html",     # an HTML fixture is test support
    "src/Button.test.tsx",
    "web/__tests__/form.jsx",
])
def test_a_folder_name_or_a_test_file_is_not_a_rendered_change(path):
    assert not S._rendered_file(path), path


@pytest.mark.parametrize("path", ["app/templates/index.html", "static/app.css", "src/App.tsx", "web/a.htm",
                                  "ui/Main.vue", "ui/Row.svelte", "theme/site.scss", "INDEX.HTML"])
def test_a_file_of_a_rendered_type_still_counts_wherever_it_sits(path):
    assert S._rendered_file(path), path


def test_a_backend_task_that_writes_python_under_a_views_folder_closes_and_the_run_completes(tmp_path):
    """The finding's repro: scoped to app/, the lead writes app/views/user.py.
    The folder name read as rendered work and the finished task ended the run
    as DesignUnverified, after the lead, review and close-out were paid for."""
    script = Script([_task(paths=("app/",), text="Add show(request) to the user views module"), "DONE"],
                    _writes({"app/views/user.py": "def show(request):\n    return {'id': 1}\n"}))
    session, project = _session(tmp_path, script, files={"app/__init__.py": "", "app/views/__init__.py": ""})
    session.run(max_tasks=3)
    assert (project / "app" / "views" / "user.py").exists()
    assert session.completed, session.stop_reason
    assert not session.design_checks and not session.open_findings


def test_a_test_fixture_page_written_beside_the_code_is_not_design_work(tmp_path):
    script = Script([_task(paths=("app/", "tests/"), text="Add the parser and its fixture"), "DONE"],
                    _writes({"app/parse.py": "def parse(text):\n    return text\n",
                             "tests/fixtures/sample.html": "<p>sample</p>\n"}))
    session, _ = _session(tmp_path, script, files={"app/__init__.py": ""})
    session.run(max_tasks=3)
    assert session.completed, session.stop_reason
    assert not session.design_checks and not session.open_findings


def test_a_template_under_a_parent_scope_is_still_held_and_only_the_rendered_file_is_named(tmp_path):
    """The rule is narrower, not gone: the stylesheet is named, the Python
    file beside it in the same views folder is not."""
    script = Script([_task(paths=("app/",), text="Add the user view and its stylesheet"), "DONE"],
                    _writes({"app/views/user.py": "def show(request):\n    return {}\n",
                             "app/views/user.css": "p { color: red }\n"}))
    session, _ = _session(tmp_path, script, files={"app/__init__.py": ""})
    session.run(max_tasks=3)
    assert not session.completed
    (record,) = session.design_checks
    assert record["rendered"] == ["app/views/user.css"] and record["verified"] is False
    assert session.stop_reason.startswith("DesignUnverified: task t1 is design work without clean rendered evidence")


def test_the_declared_scope_rule_is_unchanged():
    """Only the after-the-fact rule narrowed. A scope that names a UI folder
    is still a statement of intent, and ``_UI_PATH`` matches what it did."""
    import re
    before = re.compile(r"(?i)(^|/)(templates|static|components|pages|views|styles?)/"
                        r"|\.(html?|css|scss|sass|less|jsx|tsx|vue|svelte)$")
    for path in ("app/views/user.py", "views/", "static/a", "Styles/x", "a.HTML", "a.htm", "a.tsx", "x.svelte",
                 "app.py", "docs/readme.md", "src/util.js", "viewsx/a.py", "a.htmlx", "a.xhtml", "pages"):
        assert bool(S._UI_PATH.search(path)) == bool(before.search(path)), path
    assert S._renders("app/views/") and S._renders("static")
    assert S.is_design_task(TaskSpec("t1", "x", scope=TaskScope(["app/views/"])))
    assert not S.is_design_task(TaskSpec("t1", "x", scope=TaskScope(["app/"])))


def test_the_orchestrator_is_told_exactly_which_changed_files_count(tmp_path):
    """Prompt parity: the sentence names every type the rule reads and no
    other, says a test file is left out, and no longer lists the folder words
    (template, page, component) the rule does not go by."""
    rule = S._UNDECLARED_RENDER_RULE
    for suffix in S._RENDERED_SUFFIXES:
        assert f".{suffix}" in rule and S._rendered_file(f"app/x.{suffix}"), suffix
    import re
    assert sorted(re.findall(r"\.([a-z]+)\b", rule.split(")")[0])) == sorted(S._RENDERED_SUFFIXES)
    assert "; not a test file or under a test folder)" in rule
    assert not S._rendered_file("tests/x.html") and not S._rendered_file("src/x.test.jsx")
    assert "template, page, component" not in rule
    assert "names that file or its UI folder in SCOPE" in rule
    assert rule in S._KIND_REQUEST and rule in S._DECIDER_LABELS_REQUEST


def test_a_run_whose_decider_labels_every_task_is_told_the_rule_too(tmp_path):
    """It reads SCOPE and the changed files, never the label, so this run's
    orchestrator is held to it; it was told nothing."""
    seen = []
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"),
                      lambda model, prompt, system=None: seen.append(prompt) or "DONE",
                      config=SessionConfig(decider=object(), decider_labels="all"))
    session.next_task()
    assert "Do not begin your reply with a KIND line" in seen[-1]
    assert S._UNDECLARED_RENDER_RULE in seen[-1]


# -- 2. one reading of COVERS for the task's record and the run's claims --------------------------

def _auditor(verdicts, shown):
    def audit(prompt):
        block = prompt.split("## Requirements", 1)[1].split("\n\n", 2)[1]
        ids = [line.split(":")[0] for line in block.splitlines() if line.startswith("R")]
        shown.append(ids)
        # The task's SCOPE acceptance string is a row the audit judges on its
        # own line; a row left unjudged keeps its requirements not met.
        rows = [f"{oid}: SUFFICIENT calc.py shows it" for oid in H.obligation_ids(prompt, modality="legacy")]
        return "\n".join([f"{rid}: {verdicts.get(rid, 'MET - calc.py')}" for rid in ids] + rows)
    return audit


LISTED = ("REQUIREMENTS:\nR1: calc.py has sub\nR2: sub handles negative numbers\n\n"
          + _task(extra_lines=("COVERS: R1", "COVERS: R2")))
_SUB = _writes({"calc.py": CALC + "\n\ndef sub(a, b):\n    return a - b\n"})


def test_a_listed_task_with_two_covers_lines_has_both_requirements_audited(tmp_path):
    shown = []
    session, _ = _session(tmp_path, Script([], _SUB, audit=_auditor({}, shown)), requirements_ledger=True)
    session.run(max_tasks=3, tasks=[LISTED])
    assert session.task_outcomes[0].covers == ["R1", "R2"]
    assert session.explicit["requirements_claimed"] == ["R1", "R2"]
    assert session.explicit["requirements_unclaimed"] == []
    assert shown == [["R1", "R2"]], "the auditor was not shown every claimed requirement"
    assert dict(session.memory.ledger.requirement_status) == {"R1": "met (audited)", "R2": "met (audited)"}
    assert session.completed, session.stop_reason


def test_a_requirement_claimed_on_the_second_covers_line_can_fail_the_listed_run(tmp_path):
    """The false success: R2 was claimed by the task's own record, left out of
    the run's claims, never judged, and the run completed."""
    shown = []
    audit = _auditor({"R2": "NOT MET - sub is never called with a negative number"}, shown)
    session, _ = _session(tmp_path, Script([], _SUB, audit=audit), requirements_ledger=True)
    session.run(max_tasks=3, tasks=[LISTED])
    assert shown and "R2" in shown[0]
    assert not session.completed, "completed with a claimed requirement the audit found not met"
    assert session.memory.ledger.requirement_status["R2"].startswith("NOT MET")


def test_the_claims_and_the_task_record_come_from_one_reader():
    text = "Do it\nCOVERS: R2, R1\nmore\n  COVERS: R1 and R4\n"
    assert S._covers_ids(text) == ["R2", "R1", "R4"]
    ids, spec = S._read_covers(TaskSpec("t1", text))
    assert ids == S._covers_ids(text) and "COVERS" not in spec.description
    assert S._covers_ids("no line here") == [] and S._covers_ids(None) == []
    assert S._read_covers(TaskSpec("t1", "COVERS: nothing listed\nDo it"))[0] == []
    assert "_covers_ids(text)" in inspect.getsource(Session.run)
    assert "_COVERS.search(text)" not in inspect.getsource(Session.run)


# -- 3. the same task twice in a row --------------------------------------------------------------

TEXT = "Add a module docstring that says what the module is for"
MODULES = {name: "x = 1\n" for name in ("a.py", "b.py", "c.py")}
SHORT = {"reserve_shortfall": lambda calls: f"the budget holds fewer than {calls} reserves"}


def _docstring(model, prompt, root):
    name = prompt.split("You may change only these paths: ", 1)[1].split(". A change", 1)[0].strip()
    (root / name).write_text('"""Module."""\n' + (root / name).read_text())
    return f'Added it.\nCHANGED: ["{name}"]'


def _one(name, text=TEXT):
    return _task("docs simple", paths=(name,), text=text)


def _batch(*blocks):
    return "PARALLEL\n" + "\n---\n".join(blocks)


def _run(tmp_path, plan, **config):
    script = Script(plan, _docstring)
    session, _ = _session(tmp_path, script, files=MODULES, max_parallel_tasks=3, **config)
    return session, script


def test_a_batch_whose_blocks_share_their_words_runs_through_one_task_at_a_time(tmp_path):
    """The finding's repro. The budget could not hold three reserves, the
    harness replayed the one reply a task at a time, and its second block read
    as the orchestrator naming the same task twice."""
    session, script = _run(tmp_path, [_batch(_one("a.py"), _one("b.py"), _one("c.py")), "DONE"], **SHORT)
    session.run(max_tasks=6)
    assert [h.task_id for h in session.history] == ["t1", "t2", "t3"]
    assert session.completed, session.stop_reason
    assert len(script.of("orchestrator")) == 2, "the queue spends no orchestrator call"
    assert not session.parallel_batches, "it ran one at a time"


def test_the_same_words_for_another_file_are_not_the_same_task(tmp_path):
    session, _ = _run(tmp_path, [_one("a.py"), _one("b.py"), "DONE"])
    session.run(max_tasks=6)
    assert [h.task_id for h in session.history] == ["t1", "t2"] and session.completed


def test_a_batch_block_worded_like_last_rounds_task_for_another_file_runs(tmp_path):
    session, _ = _run(tmp_path, [_one("a.py"), _batch(_one("b.py"), _one("c.py")), "DONE"])
    session.run(max_tasks=6)
    assert [h.task_id for h in session.history] == ["t1", "t2", "t3"] and session.completed
    assert len(session.parallel_batches) == 1


def test_the_same_words_for_the_same_file_still_stall(tmp_path):
    session, script = _run(tmp_path, [_one("a.py"), _one("./a.py"), "DONE"])
    with pytest.raises(RunStalled, match="named the same task twice in a row"):
        session.run(max_tasks=6)
    assert [h.task_id for h in session.history] == ["t1"] and len(script.of("lead")) == 1


def test_naming_any_task_of_a_batch_that_ran_one_at_a_time_again_stalls(tmp_path):
    """Once the queue is through, the whole batch is what was named last, as
    it is for a batch run at once: its first task named again is a repeat.
    Only the last task run was remembered."""
    plan = [_batch(_one("a.py", "Document a"), _one("b.py", "Document b")), _one("a.py", "Document a"), "DONE"]
    session, script = _run(tmp_path, plan, **SHORT)
    with pytest.raises(RunStalled, match="named the same task twice in a row"):
        session.run(max_tasks=6)
    assert [h.task_id for h in session.history] == ["t1", "t2"] and len(script.of("lead")) == 2


def test_a_queued_task_that_repeats_the_round_before_its_batch_stalls_as_it_does_at_once(tmp_path):
    """Each block is compared with what was named before the batch. Run at
    once that was already so; one at a time the second block was compared
    only with the first, and the repeat ran again."""
    plan = [_one("b.py", "Document b"), _batch(_one("a.py", "Document a"), _one("b.py", "Document b")), "DONE"]
    at_once, _ = _run(tmp_path / "at-once", list(plan))
    with pytest.raises(RunStalled, match="named the same task twice in a row"):
        at_once.run(max_tasks=6)
    queued, script = _run(tmp_path / "queued", list(plan), **SHORT)
    with pytest.raises(RunStalled, match="named the same task twice in a row"):
        queued.run(max_tasks=6)
    assert [h.task_id for h in queued.history] == ["t1", "t2"], "the repeat of t1 was dispatched again"


def test_the_repeat_key_is_the_words_with_the_scope_paths():
    def key(text, *paths):
        return S._repeat_key(TaskSpec("t1", text, scope=TaskScope(list(paths)) if paths else None))
    assert key("Do it", "a.py") != key("Do it", "b.py")
    assert key("Do it", "a.py") == key("Do it", "./a.py")
    assert key("Do it", "a.py", "b.py") == key("Do it", "b.py", "a.py")
    assert key("Do it", ".env") != key("Do it", "env")
    assert key("Do it") == ("Do it", ())


# -- 4. a dot-folder keeps its dot when paths are compared ----------------------------------------

@pytest.mark.parametrize("path", ["static/app.css", ".storybook/preview-head.html", ".vitepress/theme/style.css"])
def test_renders_of_a_file_the_merge_fix_rewrote_are_withdrawn_under_a_dot_folder_too(tmp_path, path):
    session, project = _session(tmp_path, Script([], _SUB), files={path: "a {}\n"})
    spec = TaskSpec(task_id="t1", description="style it", scope=TaskScope(permitted_paths=[path]))
    before = session._capture_source()
    (project / path).write_text("a { color: red }\n")           # what a merge fix call left
    session._gate_fixes_used = 1
    design = dict(task="t1", verified=True, screenshots=["x.png"])
    session._renders_after_merge_gate([(spec, None, [design])], before)
    assert design["verified"] is False, "an approved render of a page the merge fix rewrote still stood"
    (finding,) = session.open_findings
    assert path in finding and "after its renders were approved" in finding


def test_another_tasks_file_under_a_dot_folder_does_not_withdraw_these_renders(tmp_path):
    """The other half of the same comparison: "storybook/x.html" is not
    ".storybook/x.html", so a change to one is not a change to the other."""
    files = {".storybook/x.html": "<p>a</p>\n", "storybook/x.html": "<p>b</p>\n"}
    session, project = _session(tmp_path, Script([], _SUB), files=files)
    spec = TaskSpec(task_id="t1", description="x", scope=TaskScope(permitted_paths=["storybook/x.html"]))
    before = session._capture_source()
    (project / ".storybook" / "x.html").write_text("<p>changed</p>\n")
    session._gate_fixes_used = 1
    design = dict(task="t1", verified=True, screenshots=["x.png"])
    session._renders_after_merge_gate([(spec, None, [design])], before)
    assert design["verified"] is True and not session.open_findings


def test_a_cited_path_under_a_dot_folder_is_looked_up_as_written(tmp_path):
    files = {".storybook/main.js": "x\n", "app.py": "x\n", "src/util.py": "x\n", "env.example": "x\n"}
    session, _ = _session(tmp_path, Script([], _SUB), files=files)
    assert session._resolve_citations("see .storybook/main.js") == [".storybook/main.js"]
    # ".env.example" is not in the project; "env.example" is another file.
    assert session._resolve_citations("configured in .env.example") == []
    assert session._resolve_citations("./app.py and /src/util.py and .//src//util.py") == [
        "app.py", "src/util.py", "src/util.py"]
    assert session._resolve_citations("../app.py or src/../app.py") == [], "a path that climbs is no citation"
    assert S._cited_path("") == "" and S._cited_path("./") == "" and S._cited_path(".github/ci.yml") == ".github/ci.yml"


def test_no_path_in_the_session_engine_is_stripped_of_dot_and_slash_characters():
    """``"x".strip("./")`` strips characters, not a prefix. Read from the
    syntax tree, so the docstrings that explain the fault do not count."""
    found = []
    for node in ast.walk(ast.parse(inspect.getsource(S))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("strip", "lstrip") and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                and {".", "/"} <= set(node.args[0].value)):
            found.append(node.lineno)
    assert not found, f"session.py strips './' as characters on line(s) {found}; use _scope_path"


# -- 5. a contest is no way around the routed lead's refusal --------------------------------------

@pytest.mark.parametrize("compete", ["off", "complex"])
def test_a_routed_leads_refusal_stops_the_run_with_or_without_a_contest(tmp_path, compete):
    """The finding's repro, already repaired when this change began
    (``_never_replaced``): with contests on the challenger's draft was merged
    and the refusal recorded as recovered."""
    def act(model, prompt, root):
        if model == OPUS:
            raise ProviderRefusal("declined by the vendor's safety policy")
        return _SUB(model, prompt, root)

    session, _ = _session(tmp_path, Script([_task("backend complex"), "DONE"], act), compete=compete)
    with pytest.raises(ProviderRefusal, match="declined by the vendor's safety policy"):
        session.run(max_tasks=3)
    assert not session.completed and not session.history
    refusals = [f.recovered for o in session.task_outcomes for f in o.facts if f.kind == "refusal"]
    assert refusals == [False]
