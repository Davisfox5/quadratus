"""A bounded editor's patch, read deterministically and kept on rejection
(Davis, 2026-11-06, PR73 6693873055; root's baseline controls 3-5).

acceptance-routing-01 t5: Grok's retained native final held a PATCH that
matched the old whole-reply regex, the returned text was rejected, and that
text was never kept, so the mismatch is still unknown. Now one PATCH-marked
fenced diff is read whatever benign framing or line endings surround it, no
model is asked to reformat, ambiguity is refused before anything applies,
and every rejection keeps the exact reply with the parser's stage.

Through the real Grok adapter (JSON envelope), Fleet and project patch
application; only the vendor binary is a script."""
import hashlib

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.providers import ProviderError
from quadratus.runtime import Fleet, bounded_patch
from quadratus.session import Session, SessionConfig
from tests.test_project_workflow import project_env  # noqa: F401 -- fixture

DIFF = "--- a/add.py\n+++ b/add.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return 0\n+    return a + b\n"
ONE = "PATCH:\n```diff\n" + DIFF + "```"


@pytest.mark.parametrize("reply", [
    ONE,
    "I read add.py first.\n\n" + ONE + "\n\nThat fixes the sum.",   # root control 3: benign framing
    ONE.replace("\n", "\r\n"),                                      # root control 4: CRLF
    "﻿" + ONE,
    "**PATCH:**\n~~~diff\n" + DIFF + "~~~",
    "PATCH:\n```\n" + DIFF + "```",
], ids=["plain", "framed", "crlf", "bom", "bold-tilde", "bare-fence"])
def test_one_marked_diff_is_read_whatever_benign_framing_surrounds_it(reply):
    diff, stage = bounded_patch(reply)
    assert stage == "ok" and diff == DIFF


@pytest.mark.parametrize("reply, stage", [
    ("Here is the change:\n```diff\n" + DIFF + "```", "no-marker"),
    (ONE + "\n\nPATCH:\n```diff\n" + DIFF.replace("a + b", "b + a") + "```", "multiple-markers"),  # control 5
    (ONE + "\n\nOr instead:\n```diff\n" + DIFF.replace("a + b", "b + a") + "```", "multiple-diffs"),
    ("NO CHANGES: nothing to do\n" + ONE, "conflicting-no-changes"),
    ("PATCH: see below\nI changed add.", "no-fence"),
    ("PATCH:\n```diff\n" + DIFF, "unterminated-fence"),
    ("PATCH:\n```diff\n\n```", "empty-diff"),
    ("PATCH:\n```diff\nreturn a + b\n```", "not-a-diff"),
])
def test_ambiguous_or_incomplete_replies_are_refused_with_their_stage(reply, stage):
    assert bounded_patch(reply) == (None, stage)


def _invoke(project, settings, monkeypatch, reply):
    monkeypatch.setenv("FAKE_MODE", "reply")
    monkeypatch.setenv("FAKE_REPLY", reply)
    fleet = Fleet(settings, project=project, allow_writes=True)
    try:
        return fleet.invoke("grok:worker", "Implement the addition patch", allow_writes=True)
    finally:
        fleet.close()


def test_a_framed_crlf_patch_applies_through_the_grok_adapter(project_env, monkeypatch):  # noqa: F811
    project, settings, _trace, _caller = project_env
    reply = _invoke(project, settings, monkeypatch, ("Reading add.py.\n\n" + ONE + "\n\nDone.").replace("\n", "\r\n"))
    assert "Patch applied" in reply
    assert "return a + b" in (project.root / "add.py").read_text()


@pytest.mark.parametrize("reply, stage", [
    (ONE + "\n\nPATCH:\n```diff\n" + DIFF.replace("a + b", "b + a") + "```", "multiple-markers"),
    ("PATCH:\n```diff\n--- a/add.py\n+++ b/add.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return 9\n"
     "+    return a + b\n```", "apply"),
    ("I think add should return the sum.", "no-marker"),
], ids=["two-patches", "does-not-apply", "prose-only"])
def test_a_rejection_keeps_the_exact_reply_and_changes_nothing(project_env, monkeypatch, reply, stage):  # noqa: F811
    project, settings, _trace, _caller = project_env
    before = (project.root / "add.py").read_text()
    with pytest.raises(Exception) as caught:
        _invoke(project, settings, monkeypatch, reply)
    exc = caught.value
    assert exc.provider_response == reply, "the exact returned text, not a summary of it"
    assert exc.parse_stage == stage and exc.response_chars == len(reply)
    assert exc.response_sha256 == hashlib.sha256(reply.encode()).hexdigest() and exc.response_truncated is False
    assert (project.root / "add.py").read_text() == before, "nothing applied"


def test_the_session_keeps_a_rejected_reply_with_its_labelled_metadata(tmp_path):
    """The runtime exception reaches the session's call seam: the reply
    exactly as returned and its metadata go to a private artifact, and the
    metadata without the text goes on the run record (root review of
    b49a0cb, R6)."""
    import json

    from quadratus.runtime import _attach_response
    reply = "Reading.\n" + ONE + "\nPATCH:\n```diff\n" + DIFF + "```"

    def invoke(*args, **kwargs):
        raise _attach_response(ProviderError("Bounded editor returned no PATCH (more than one PATCH: marker)."),
                               reply, "multiple-markers", normalized=reply.replace("Reading.", "Read."))
    store = ArtifactStore(tmp_path / "artifacts")
    session = Session("goal", store, invoke, config=SessionConfig())
    with pytest.raises(ProviderError) as caught:
        session._invoke_model_call("grok:worker", "edit", allow_writes=False)
    kept = json.loads(store.get(caught.value.response_artifact))
    assert kept["text"] == reply and kept["text_kind"] == "raw returned text"
    assert kept["chars"] == len(reply) and kept["sha256"] == hashlib.sha256(reply.encode()).hexdigest()
    assert kept["stage"] == "multiple-markers" and kept["normalized"] is True and kept["truncated"] is False
    (meta,) = session.rejected_responses
    assert "text" not in meta and meta["artifact"] == caught.value.response_artifact
    assert meta["chars"] == len(reply) and meta["model"] == "grok:worker"


def test_a_reply_over_the_bound_is_a_labelled_prefix_with_the_whole_replys_digest():
    from quadratus.runtime import RESPONSE_KEEP_CHARS, _attach_response, response_record
    reply = "PATCH:\n" + "x" * (RESPONSE_KEEP_CHARS + 50)
    record = response_record(_attach_response(ProviderError("no"), reply, "no-fence"))
    assert record["text"] == reply[:RESPONSE_KEEP_CHARS] and record["kept_chars"] == RESPONSE_KEEP_CHARS
    assert record["text_kind"] == "retained prefix of the raw returned text" and record["truncated"] is True
    assert record["chars"] == len(reply) and record["sha256"] == hashlib.sha256(reply.encode()).hexdigest()


def test_a_failed_retention_is_visible_on_the_record(tmp_path):
    from quadratus.runtime import _attach_response

    class Refusing(ArtifactStore):
        def put(self, content, *, kind, author):
            if kind == "rejected-response":
                raise OSError("disk full")
            return super().put(content, kind=kind, author=author)

    def invoke(*args, **kwargs):
        raise _attach_response(ProviderError("rejected"), ONE + ONE, "multiple-markers")
    session = Session("goal", Refusing(tmp_path / "artifacts"), invoke, config=SessionConfig())
    with pytest.raises(ProviderError):
        session._invoke_model_call("grok:worker", "edit", allow_writes=False)
    (meta,) = session.rejected_responses
    assert meta["artifact"] is None and meta["retention_error"].startswith("OSError: disk full")


def test_the_worker_seam_keeps_the_same_labelled_record(tmp_path):
    import json

    from quadratus.runtime import _attach_response
    from quadratus.workers import WorkerPool
    from tests.test_reliability_repair import OPUS, _memory
    store = ArtifactStore(tmp_path / "artifacts")
    reply = ONE + "\n" + ONE

    def run(model, prompt, allow_writes=False):
        raise _attach_response(ProviderError("Bounded editor returned no PATCH."), reply, "multiple-markers")

    def invoke(model, prompt, system=None, allow_writes=False):
        if "Name the single next task" in prompt:
            return "DONE"
        if "The task is finished" in prompt:
            return "SUMMARY: closed incomplete"
        if "Worker errand" in prompt and "failed" in prompt:
            return "Closing incomplete."
        return 'WORKER {"errand":"draft","instruction":"write the design"}'
    session = Session("goal", store, invoke, config=SessionConfig())
    session.workers = WorkerPool(store=store, run=run)
    from quadratus.session import TaskSpec
    session._draft_with_channels(OPUS, TaskSpec("t1", "design it"), _memory(store))
    kept = [json.loads(store.get(i)) for i in store.ids()
            if (store.ref(i).kind or "").startswith("worker-stopped:")]
    assert kept and kept[0]["text"] == reply and kept[0]["stage"] == "multiple-markers"
    assert kept[0]["chars"] == len(reply) and kept[0]["text_kind"] == "raw returned text"
