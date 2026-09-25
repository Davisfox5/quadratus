"""A lead's turn limit and the partial-work handback (Codex review on #25).

Offline throughout: fake CLI envelopes for the extractors and argv, and a fake
``CLIProvider._call`` driving the real project runner against a temporary
writable project for the handback.
"""

import json
from pathlib import Path

import pytest

from quadratus.cli_providers import (
    ClaudeCLIProvider,
    CLIProvider,
    CodexCLIProvider,
    GrokCLIProvider,
    _extract_claude_result,
    _extract_grok_result,
)
from quadratus.config import Settings
from quadratus.delegation import invocation
from quadratus.project_run import run_project
from quadratus.providers import ProviderError, TurnLimitReached
from quadratus.runtime import Fleet


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/unused/cli")
    for vendor in ("OPENAI", "CLAUDE", "GROK"):
        monkeypatch.delenv(f"QUADRATUS_CLI_ARGS_{vendor}", raising=False)
    monkeypatch.delenv("QUADRATUS_NATIVE_DELEGATION", raising=False)


def _pair(argv, flag):
    return argv[argv.index(flag) + 1]


# -- the capped envelope is an outcome, not an error -----------------------------


def test_grok_max_turns_with_text_is_turn_limited_and_keeps_the_narration():
    envelope = {"stopReason": "max_turns", "text": "I added the header; next the rows",
                "num_turns": 16}
    with pytest.raises(TurnLimitReached) as caught:
        _extract_grok_result(json.dumps(envelope))
    assert caught.value.partial_text == "I added the header; next the rows"
    assert caught.value.turns == 16
    assert isinstance(caught.value, ProviderError), "never retried: ProviderError passes through"


def test_grok_max_turns_with_no_text_is_still_turn_limited():
    envelope = {"stopReason": "max_turns", "text": "",
                "modelUsage": {"grok-4.7-build": {"modelCalls": 16}}}
    with pytest.raises(TurnLimitReached) as caught:
        _extract_grok_result(json.dumps(envelope))
    assert caught.value.partial_text is None
    assert caught.value.turns == 16


def test_other_grok_stops_are_unchanged():
    with pytest.raises(ProviderError) as caught:
        _extract_grok_result(json.dumps({"stopReason": "cancelled", "text": "I'll create it"}))
    assert not isinstance(caught.value, TurnLimitReached)


def test_claude_error_max_turns_is_turn_limited():
    envelope = {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 16}
    with pytest.raises(TurnLimitReached) as caught:
        _extract_claude_result(json.dumps(envelope))
    assert caught.value.turns == 16 and caught.value.partial_text is None


# -- the flag -------------------------------------------------------------------


@pytest.mark.parametrize("make", [lambda: ClaudeCLIProvider(model="opus", allow_writes=True),
                                  lambda: GrokCLIProvider(model="", allow_writes=True)])
def test_a_view_with_a_turn_limit_sends_it(make):
    view = make()
    assert "--max-turns" not in view._build_argv("p", ""), "off unless set"
    view.max_turns = 16
    assert _pair(view._build_argv("p", ""), "--max-turns") == "16"


def test_codex_has_no_turn_flag_and_is_unchanged():
    plain = CodexCLIProvider("gpt-5.6-sol")._build_argv("p", "")
    capped = CodexCLIProvider("gpt-5.6-sol")
    capped.max_turns = 16
    assert capped._build_argv("p", "") == plain


def test_a_summary_call_keeps_its_own_single_turn(tmp_path):
    (tmp_path / "empty").mkdir()
    view = GrokCLIProvider(model="", workdir=str(tmp_path / "empty"), timeout=60,
                           max_retries=1).for_seat("", effort="low", restricted=True)
    view.summary_only = True
    view.max_turns = 16
    argv = view._build_argv("p", "")
    assert argv.count("--max-turns") == 1 and _pair(argv, "--max-turns") == "1"


# -- the Fleet applies it to leads only, per call -------------------------------


@pytest.mark.parametrize("project", [True, False])
def test_the_fleet_limits_lead_calls_only(monkeypatch, tmp_path, project):
    views = []
    monkeypatch.setattr(Fleet, "_generate",
                        lambda self, key, provider, prompt, system, **kw: views.append(provider) or "ok")
    (tmp_path / "a.py").write_text("x = 1\n")
    fleet = Fleet(Settings(backend="cli", lead_max_turns=16), project=tmp_path if project else None)
    for role in ("lead", "verifier", "orchestrator"):
        with invocation("t1", role):
            fleet.invoke("grok:default", "p")
    lead, verifier, orchestrator = views
    assert lead.max_turns == 16
    assert verifier.max_turns is None and orchestrator.max_turns is None
    assert fleet.provider_for("grok:default").max_turns is None, "the shared provider is never marked"


def test_no_setting_means_no_limit(monkeypatch, tmp_path):
    views = []
    monkeypatch.setattr(Fleet, "_generate",
                        lambda self, key, provider, prompt, system, **kw: views.append(provider) or "ok")
    fleet = Fleet(Settings(backend="cli"), project=None)
    with invocation("t1", "lead"):
        fleet.invoke("grok:default", "p")
    assert views[0].max_turns is None


# -- the handback, end to end through run_project -------------------------------


def _scope(path="a.md"):
    return "SCOPE: " + json.dumps({"permitted_paths": [path], "intended_result": "Heading reads Hello",
                                    "acceptance": ["Heading reads Hello"], "max_lines": 10})


def _run(tmp_path, monkeypatch, leads, *, tasks, max_tasks=5):
    """Drive run_project with scripted orchestrator replies and lead behaviours."""
    monkeypatch.setattr(CLIProvider, "available", lambda _: True)
    seen = {"orchestrator": [], "lead_max_turns": [], "leads": 0}
    plan = list(tasks)

    def call(self, prompt, system, history):
        self.last_usage = {"input_tokens": 700, "output_tokens": 20, "cached_input_tokens": 500}
        self.last_diagnostics = {"model_calls": 16, "cached_input_tokens": 500}
        if "Name the single next task" in prompt:
            seen["orchestrator"].append(prompt)
            return plan.pop(0) if plan else "DONE"
        if "The task cap for this run has been reached" in prompt:
            return "DONE"
        if "You are leading" in prompt:
            seen["lead_max_turns"].append(self.max_turns)
            behaviour = leads[seen["leads"]]
            seen["leads"] += 1
            return behaviour(self)
        if "The task is finished" in prompt:
            return "SUMMARY: heading corrected\nREASONING: inspected"
        pytest.fail(f"Unexpected model call: {prompt[:120]}")

    monkeypatch.setattr(CLIProvider, "_call", call)
    result = run_project("Correct heading", tmp_path, Settings(backend="cli", lead_max_turns=16),
                         allow_writes=True, max_tasks=max_tasks)
    rows = [json.loads(line) for line in (result.run_dir / "invocations.jsonl").read_text().splitlines()]
    data = json.loads((result.run_dir / "result.json").read_text())
    return result, rows, data, seen


def _capped_after_writing(text, path="a.md"):
    def lead(self):
        Path(self.workdir, path).write_text("partial\n")
        raise TurnLimitReached("grok stopped at its turn limit before finishing", partial_text=text, turns=16)
    return lead


def _capped_with_nothing(self):
    raise TurnLimitReached("grok stopped at its turn limit before finishing", partial_text=None, turns=16)


def _finishes(self):
    Path(self.workdir, "a.md").write_text("# Hello\n")
    return 'Heading corrected\nCHANGED: ["a.md"]'


TASK_1 = "KIND: docs simple\n" + _scope() + "\nCorrect the heading in a.md."
TASK_2 = "KIND: docs simple\n" + _scope() + "\nFinish the heading in a.md.\nCONTINUES: t1"
UNRELATED = "KIND: docs simple\n" + _scope() + "\nFix a typo in a.md."


def test_a_capped_lead_hands_its_partial_edit_back_and_the_run_completes(tmp_path, monkeypatch):
    (tmp_path / "mine.txt").write_text("user work\n")
    result, rows, data, seen = _run(
        tmp_path, monkeypatch,
        [_capped_after_writing("I set the heading line; the body is next"), _finishes],
        tasks=[TASK_1, TASK_2])
    assert result.completed and not result.error
    assert data["tasks"] == 2 and data["turn_limited_tasks"] == ["t1"]
    assert (tmp_path / "a.md").read_text() == "# Hello\n"
    assert (tmp_path / "mine.txt").read_text() == "user work\n"
    assert seen["lead_max_turns"] == [16, 16], "the limit reached the CLI view on every lead"
    # The orchestrator's next decision saw the unfinished task and the kept edit.
    assert "STOPPED AT THE LEAD TURN LIMIT" in seen["orchestrator"][1]
    assert "a.md" in seen["orchestrator"][1]
    assert "narration and not a result" in seen["orchestrator"][1]
    leads = [r for r in rows if r["invoked"] and r["role"] == "lead"]
    assert [r["outcome"] for r in leads] == ["TurnLimitReached", "ok"], "capped once, never retried"
    capped = leads[0]
    assert capped["turn_limited"] is True and capped["max_turns"] == 16 and capped["model_turns"] == 16
    assert (capped["input_tokens"], capped["cached_input_tokens"], capped["fresh_input_tokens"],
            capped["output_tokens"]) == (700, 500, 200, 20), "usage of the capped call is recorded"
    assert not [r for r in rows if r["invoked"] and r["role"] == "closeout" and r["task"] == "t1"], \
        "no close-out model call for an unfinished task"


def test_a_cap_with_no_answer_and_no_edit_is_handed_back_too(tmp_path, monkeypatch):
    result, rows, data, seen = _run(tmp_path, monkeypatch, [_capped_with_nothing, _finishes],
                                    tasks=[TASK_1, TASK_2])
    assert result.completed and data["turn_limited_tasks"] == ["t1"]
    assert "Changed, unreviewed and ungated: no files" in seen["orchestrator"][1]
    assert "The lead returned no answer text" in seen["orchestrator"][1]


def test_two_caps_in_a_row_stop_the_run_instead_of_looping(tmp_path, monkeypatch):
    result, rows, data, seen = _run(
        tmp_path, monkeypatch,
        [_capped_after_writing("first"), _capped_after_writing("second"), _finishes],
        tasks=[TASK_1, TASK_2, "KIND: docs simple\n" + _scope() + "\nTry the heading again."])
    assert not result.completed and not result.error
    assert data["turn_limited_tasks"] == ["t1", "t2"]
    assert seen["leads"] == 2, "no third lead after two caps in a row"
    assert (tmp_path / "a.md").read_text() == "partial\n", "the kept work stays in place"


def test_a_capped_lead_that_wrote_outside_its_scope_stops_with_the_work_preserved(tmp_path, monkeypatch):
    result, rows, data, seen = _run(tmp_path, monkeypatch, [_capped_after_writing("oops", path="b.md")],
                                    tasks=[TASK_1])
    assert not result.completed and "PartialWorkStopped" in (result.error or "")
    assert (tmp_path / "b.md").read_text() == "partial\n"
    assert data["turn_limited_tasks"] == []


def test_an_unrelated_clean_task_does_not_resolve_a_capped_one(tmp_path, monkeypatch):
    """Codex review of #25: any clean task used to clear the partial flag."""
    result, rows, data, seen = _run(
        tmp_path, monkeypatch, [_capped_after_writing("half done"), _finishes],
        tasks=[TASK_1, UNRELATED])
    assert not result.completed and not result.error
    assert data["turn_limited_tasks"] == ["t1"]
    assert "CONTINUES: t1" in seen["orchestrator"][1], "the orchestrator is told how to link the follow-up"
