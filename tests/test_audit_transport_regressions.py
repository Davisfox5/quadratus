"""Reproductions for transport and project-boundary audit findings."""

import difflib
import json
import os
from types import SimpleNamespace

import pytest

from quadratus.cli_providers import _extract_codex_result
from quadratus.decisions import Decision, JevDecider
from quadratus.project import Project
from quadratus.providers import ClaudeProvider, GrokProvider, OpenAIProvider, ProviderError
from quadratus.run_budget import RunBudget, RunLimits


@pytest.mark.parametrize("error", [
    {"message": "connection lost"},
    {},
    None,
])
def test_codex_failed_turn_cannot_deliver_earlier_narration(error):
    stream = "\n".join(json.dumps(event) for event in [
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": "I am inspecting the project."}},
        {"type": "turn.failed", "error": error},
    ])

    with pytest.raises(ProviderError, match="turn failed"):
        _extract_codex_result(stream)


def test_codex_auth_failure_after_narration_keeps_vendor_sign_in_marker():
    stream = "\n".join(json.dumps(event) for event in [
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": "I will check the files."}},
        {"type": "turn.failed", "error": {"message": "401 Unauthorized"}},
    ])

    with pytest.raises(ProviderError) as caught:
        _extract_codex_result(stream)
    assert caught.value.auth_invalid


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO support is required")
def test_source_snapshot_skips_a_fifo_before_opening_it(tmp_path):
    (tmp_path / "source.py").write_text("value = 1\n")
    os.mkfifo(tmp_path / "events.py")
    project = Project(tmp_path)

    # Assert before snapshotting: the unfixed implementation must fail this
    # test immediately instead of opening the FIFO and hanging pytest.
    assert [path.name for path in project.files()] == ["source.py"]
    with project.snapshot() as snapshot:
        assert (snapshot / "source.py").read_text() == "value = 1\n"
        assert not (snapshot / "events.py").exists()


def _patch(relative):
    return f"--- a/{relative}\n+++ b/{relative}\n@@ -1 +1 @@\n-old\n+new\n"


@pytest.mark.parametrize("relative", [
    "source dir/.quadratus/state.txt",
    "source dir/.env",
    "excluded dir/state.txt",
])
def test_patch_validates_complete_paths_with_spaces(tmp_path, relative):
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text("old\n")
    project = Project(tmp_path, exclude=[tmp_path / "excluded dir"])

    with pytest.raises(ProviderError, match="outside editable project source"):
        project.apply_patch(_patch(relative))
    assert target.read_text() == "old\n"


def test_patch_can_still_edit_allowed_paths_with_spaces(tmp_path):
    target = tmp_path / "source dir/example file.py"
    target.parent.mkdir()
    target.write_text("old\n")

    Project(tmp_path).apply_patch(_patch("source dir/example file.py"))

    assert target.read_text() == "new\n"


def test_patch_payload_resembling_file_headers_is_applied_to_its_real_file(tmp_path):
    target = tmp_path / "notes.txt"
    old, new = "-- old.txt\n", "++ second\n"
    target.write_text(old)
    patch = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                         fromfile="a/notes.txt", tofile="b/notes.txt"))

    Project(tmp_path).apply_patch(patch)

    assert target.read_text() == new


@pytest.mark.parametrize("provider_type", [ClaudeProvider, OpenAIProvider, GrokProvider])
def test_api_request_uses_the_current_run_budget_timeout(monkeypatch, provider_type):
    seen = []
    response = SimpleNamespace(
        model="fixture-model",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                              prompt_tokens=1, completion_tokens=1),
        content=[SimpleNamespace(type="text", text="answer")],
        output_text="answer",
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
    )

    def create(**kwargs):
        seen.append(kwargs)
        return response

    endpoint = SimpleNamespace(create=create)
    client = SimpleNamespace(messages=endpoint, responses=endpoint,
                             chat=SimpleNamespace(completions=endpoint))
    monkeypatch.setattr(provider_type, "_build_client", lambda self: client)
    provider = provider_type("fixture-model", "fixture", timeout=120)
    provider.run_budget = RunBudget(RunLimits(wall_seconds=5), clock=lambda: 0)

    assert provider.generate("bounded request") == "answer"

    assert seen[0]["timeout"] == 5
    assert provider.timeout == 120


@pytest.mark.parametrize("configured,remaining,expected", [
    (10, 1, 1),
    (2, 20, 2),
    (7, None, 7),
    (3, "no budget", 3),
    (None, 5, 5),
])
def test_jev_request_uses_the_shorter_budget_or_configured_timeout(configured, remaining, expected):
    httpx2 = pytest.importorskip("httpx2")
    pytest.importorskip("typesafe_sdk")
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx2.Response(200, json={
            "model": "fixture", "usage": {"input_tokens": 1, "output_tokens": 0},
            "answers": {"task_kind": {"type": "choice", "choice": "backend",
                                      "confidence": 1, "probabilities": {"backend": 1}}},
        })

    if remaining == "no budget":
        budget = None
    elif remaining is None:
        budget = SimpleNamespace(reserve=lambda **kwargs: (1, None),
                                 finish=lambda *args, **kwargs: None)
    else:
        budget = RunBudget(RunLimits(wall_seconds=remaining), clock=lambda: 0)
    decider = JevDecider(api_key="offline-fixture", model="jev-latest", timeout=configured,
                         base_url="https://example.test", budget=budget,
                         transport=httpx2.MockTransport(handler))

    verdict = decider.decide(Decision(id="task.kind", question="Kind?", answers=("backend",)))

    assert verdict.answer == "backend"
    assert seen == [{key: expected for key in ("connect", "read", "write", "pool")}]
    assert decider.timeout == configured
