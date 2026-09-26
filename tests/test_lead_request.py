"""A lead's closing request, however the model lays it out (GameTape run 9)."""
import json
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import CLIProvider
from quadratus.config import Settings
from quadratus.delegation import invocation
from quadratus.memory import TaskMemory
from quadratus.project import Project
from quadratus.project_run import run_project
from quadratus.runtime import Fleet
from quadratus.session import PartialWorkStopped, Session, SessionConfig, TaskSpec
from quadratus.taskmeta import MAX_REQUEST_PREFACE_LINES, lead_request, split_lead_request
from quadratus.workers import WorkerPool

OPUS = 'claude:opus'
FABLE = 'claude:fable'
LUNA = 'openai:gpt-5.6-luna'
ERRAND = {"errand": "format", "instruction": "Create a.py with the helper", "write": True}
LINE = json.dumps(ERRAND)
#: The shape of run 9's t8 reply: a short preface, WORKER alone, the object below.
RUN9 = "The write was refused, so I am handing the edit to a worker.\n\nWORKER\n" + LINE


@pytest.mark.parametrize('reply', [
    RUN9,
    "WORKER\n" + LINE,
    "WORKER   " + LINE,
    "Plan below.\nWORKER\n\n   " + LINE + "\n\n",
    "Plan below.\nWORKER\n" + json.dumps(ERRAND, indent=2),
    "Plan below.\nWORKER " + json.dumps(ERRAND, indent=2),
])
def test_a_worker_request_is_found_across_whitespace_and_newlines(reply):
    preface, request = split_lead_request(reply)
    assert request.startswith("WORKER {") and "\n" not in request
    assert json.loads(request[len("WORKER "):]) == ERRAND
    assert "WORKER" not in preface and lead_request(reply) == request


def test_a_one_line_request_is_passed_on_unchanged_and_the_preface_is_kept():
    compact = 'WORKER {"errand":"code","instruction":"add tests","write":true}'
    assert split_lead_request("I edited app.py.\nTests next:\n" + compact) == (
        "I edited app.py.\nTests next:", compact)
    assert split_lead_request(RUN9)[0] == "The write was refused, so I am handing the edit to a worker."


@pytest.mark.parametrize('reply', [
    RUN9 + '\nCHANGED: ["a.py"]',                               # a delivery and a request at once
    'CHANGED: []\nWORKER\n' + LINE,
    "Plan.\nWORKER\n{\"errand\": \"format\", \"instruction\": ",  # malformed
    "Plan.\nWORKER\n[1, 2]",                                     # not an object
    'Plan.\nWORKER\n"format"',
    "Plan.\nWORKER",                                             # verb with nothing after it
    "Plan.\nWORKER\n" + LINE + "\n" + LINE,                      # two objects
    "Plan.\nWORKER\n" + LINE + "\nThen I will review it.",       # trailing prose
    "Plan.\nWORKER\n" + LINE + " and then more",
    "WORKER\n" + LINE + "\nWORKER\n" + LINE,                     # two requests
    "FETCH: abc123\nWORKER\n" + LINE,
    "CONSULT claude:opus: is this right?\nWORKER\n" + LINE,
    "WORKER " + LINE + "\nFETCH: abc123",
    "\n".join(["line"] * (MAX_REQUEST_PREFACE_LINES + 1)) + "\nWORKER\n" + LINE,
    "Next I would ask a WORKER\nfor the docs.",                  # prose that mentions the verb
    "WORKERS\n" + LINE,
])
def test_anything_but_one_closing_request_is_a_draft(reply):
    assert split_lead_request(reply) is None and lead_request(reply) is None


def test_the_preface_bound_counts_prose_not_the_object():
    pretty = json.dumps(dict(ERRAND, needs=["patch"] * 30), indent=2)
    assert len(pretty.splitlines()) > MAX_REQUEST_PREFACE_LINES
    reply = "\n".join(["line"] * MAX_REQUEST_PREFACE_LINES) + "\nWORKER\n" + pretty
    assert lead_request(reply).startswith("WORKER {")


def test_closing_consult_lines_stay_one_request_and_other_verbs_are_unchanged():
    both = "CONSULT sol: is the schema right?\nCONSULT gemini: and the layout?"
    assert split_lead_request("Two questions.\n" + both) == ("Two questions.", both)
    assert lead_request("FETCH: abc123") == "FETCH: abc123"
    assert lead_request("Prose.\nFETCH:") is None


# -- dispatchers -------------------------------------------------------------

def _memory(store):
    return TaskMemory("t1", OPUS, store)


def test_the_drafting_loop_serves_the_run9_shape(tmp_path):
    """Drafting path: no project, the lead's reply goes straight to the loop."""
    store = ArtifactStore(tmp_path / "artifacts")
    read = {"errand": "read", "instruction": "Where are the CSV helpers?"}
    replies = iter(["Writes are refused here, so a worker reads first.\nWORKER\n" + json.dumps(read), "Done."])
    session = Session("goal", store, lambda *a, **k: next(replies), config=SessionConfig())
    served = []
    session.workers = WorkerPool(store=store, run=lambda key, instruction, **k: served.append(instruction) or "ok")
    task = _memory(store)
    with invocation("t1", "lead"):
        draft = session._draft_with_channels(OPUS, TaskSpec("t1", "do it"), task)
    assert draft == "Done."
    assert len(served) == 1 and read["instruction"] in served[0]
    assert any(t.content.startswith("[preface to a request] Writes are refused") for t in task.turns())


def _fleet(root, monkeypatch, reply, *, restricted=False):
    fleet = Fleet(Settings(backend='cli'), project=Project(root), allow_writes=True)
    view = SimpleNamespace()
    provider = SimpleNamespace(restricted=restricted, in_directory=lambda *a, **kw: view)
    monkeypatch.setattr(fleet, 'provider_for', lambda key: provider)
    monkeypatch.setattr(fleet, '_generate', lambda *a: reply)
    return fleet


@pytest.mark.parametrize('restricted', [False, True])
def test_the_editing_dispatchers_accept_the_run9_shape(tmp_path, monkeypatch, restricted):
    """Editing path: before the fix this raised 'CHANGED report does not match'."""
    (tmp_path / 'a.py').write_text('x\n')
    fleet = _fleet(tmp_path, monkeypatch, RUN9, restricted=restricted)
    try:
        assert fleet.invoke(OPUS, 'edit', allow_writes=True) == RUN9
    finally:
        fleet.close()


@pytest.mark.parametrize('reply', [RUN9 + "\nThen I will review it.", "Plan.\nWORKER\n[1, 2]"])
def test_the_editing_dispatcher_still_refuses_a_malformed_request(tmp_path, monkeypatch, reply):
    (tmp_path / 'a.py').write_text('x\n')
    fleet = _fleet(tmp_path, monkeypatch, reply)
    try:
        with pytest.raises(PartialWorkStopped, match='CHANGED report'):
            fleet.invoke(OPUS, 'edit', allow_writes=True)
    finally:
        fleet.close()


def test_a_project_run_serves_the_run9_shape_end_to_end(tmp_path, monkeypatch):
    """Fleet dispatcher, drafting loop and worker pool together, as run 9 hit them."""
    monkeypatch.setattr(CLIProvider, 'available', lambda _: True)
    scope = dict(permitted_paths=['a.py'], intended_result='Implement a',
                 acceptance=['a is correct'], max_lines=10)
    declaration = 'KIND: backend standard\nSCOPE: ' + json.dumps(scope) + '\nImplement a.'
    calls = []

    def call(self, prompt, system, history):
        calls.append(self.model)
        if self.model == 'fable':
            return declaration.replace('backend standard', 'architect complex')
        if self.model == 'opus':
            if (tmp_path / 'a.py').exists():
                raise KeyboardInterrupt('stop after the worker was served')
            return RUN9
        return 'PATCH:\n```diff\n--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+helper\n```'
    monkeypatch.setattr(CLIProvider, '_call', call)
    result = run_project('fix', tmp_path, Settings(backend='cli'), allow_writes=True, max_tasks=1)
    data = json.loads((result.run_dir / 'result.json').read_text())
    assert (tmp_path / 'a.py').read_text() == 'helper\n'
    assert calls.count('opus') == 2
    assert 'CHANGED report' not in json.dumps(data)


@pytest.mark.parametrize('answer', ["WORKER\n" + LINE, "  WORKER " + LINE, "FETCH: abc123"])
def test_a_worker_cannot_hire_in_either_layout(tmp_path, answer):
    from quadratus.workers import FanOutExceeded
    store = ArtifactStore(tmp_path / "artifacts")
    pool = WorkerPool(store=store, run=lambda *a, **k: answer)
    with pytest.raises(FanOutExceeded, match="cannot hire"):
        pool.commission(task=_memory(store), parent_key=OPUS, prompt="go", label="w")


# -- Run 11: a request run onto the end of a sentence --------------------------

#: Grok's final reply in GameTape run 11 t1, verbatim apart from the id.
RUN11 = ("I'll wire the existing preview builder into a POST endpoint and add Flask tests, "
         "starting from the clip routes, parser errors, and the client fixture.FETCH: {id}")


def test_a_request_run_onto_the_last_sentence_is_split_off():
    preface, request = split_lead_request(RUN11.format(id="2185e3cf5881"))
    assert request == "FETCH: 2185e3cf5881" and preface.endswith("the client fixture.")
    assert split_lead_request("Plan.  WORKER " + LINE)[1] == "WORKER " + LINE
    assert split_lead_request("Two questions!CONSULT sol: is the schema right?")[1].startswith("CONSULT ")


@pytest.mark.parametrize('reply', [
    "Use FETCH: abc123 to read it.",                          # prose, not after a sentence end
    "I will FETCH: abc123",
    "The ids are ready. FETCH: ids are listed above",         # inline FETCH must name one id
    "Done. `x.FETCH: abc123`",                                # inline code
    "Example:\n```\nstep one.FETCH: abc123\n```",             # code fence
    "First.FETCH: abc123. Then.FETCH: def456",                # two inline requests
    "Read it.FETCH: abc123\nCHANGED: []",                     # delivery and request at once
    "Read it.FETCH: abc123 and then more prose follows",
    "Plan.WORKER " + LINE + " then prose",                    # trailing prose after the object
    "\n".join(["line"] * MAX_REQUEST_PREFACE_LINES) + "\nlast.FETCH: abc123",
])
def test_an_inline_request_that_is_not_one_clean_closing_request_is_a_draft(reply):
    assert split_lead_request(reply) is None


def test_the_editing_dispatcher_accepts_the_run11_reply(tmp_path, monkeypatch):
    """Before the fix Fleet raised 'CHANGED report does not match' on this reply."""
    (tmp_path / 'a.py').write_text('x\n')
    reply = RUN11.format(id="2185e3cf5881")
    fleet = _fleet(tmp_path, monkeypatch, reply)
    try:
        assert fleet.invoke(OPUS, 'edit', allow_writes=True) == reply
    finally:
        fleet.close()


def test_the_drafting_loop_fetches_an_existing_worker_artifact_from_the_run11_reply(tmp_path):
    """The lead commissions a worker, then asks for the stored result with the
    request glued to its sentence, as run 11 did; the artifact is served."""
    store = ArtifactStore(tmp_path / "artifacts")
    prompts = []

    def invoke(model, prompt, system=None, allow_writes=False):
        prompts.append(prompt)
        if len(prompts) == 1:
            return 'WORKER {"errand":"read","instruction":"Where is the client fixture?"}'
        if len(prompts) == 2:
            worker_ids = [i for i in store.ids() if store.ref(i).kind.startswith("worker:")]
            assert len(worker_ids) == 1
            return RUN11.format(id=worker_ids[0])
        return "Done."

    session = Session("goal", store, invoke, config=SessionConfig())
    session.workers = WorkerPool(store=store, run=lambda *a, **k: "The fixture is in tests/conftest.py")
    task = _memory(store)
    with invocation("t1", "lead"):
        draft = session._draft_with_channels(OPUS, TaskSpec("t1", "wire the endpoint"), task)
    assert draft == "Done."
    assert "The fixture is in tests/conftest.py" in prompts[2]
    assert any(t.content.startswith("[fetched artifact ") for t in task.turns())


@pytest.mark.parametrize('reply', [
    # Codex review of 03be7e3: quoted and code examples are not requests.
    'Example: "Plan.FETCH: 2185e3cf5881"',
    "Example: 'Plan.FETCH: 2185e3cf5881'",
    'Example: ``Plan.FETCH: 2185e3cf5881``',
    'Example:\n~~~\nPlan.FETCH: 2185e3cf5881',                  # unclosed tilde fence
    'Example:\n````\nPlan.FETCH: 2185e3cf5881\n```',             # a shorter run does not close it
    'Example: `Plan.FETCH: 2185e3cf5881',                         # unclosed backtick run
    'He wrote “Plan.FETCH: 2185e3cf5881',                    # open curly quote
    'Example: "Plan.CONSULT sol: is this right?"',
    'Done.FETCH: abc123',                                         # not an artifact id
    'Done.FETCH: 2185E3CF5881',
])
def test_a_quoted_or_code_example_of_an_inline_request_is_a_draft(reply):
    assert split_lead_request(reply) is None


def test_an_inline_request_after_a_closed_fence_still_counts():
    reply = "Plan:\n~~~\nPlan.FETCH: aaaaaaaaaaaa\n~~~\nNow I read the result.FETCH: 2185e3cf5881"
    assert split_lead_request(reply)[1] == "FETCH: 2185e3cf5881"
