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


def test_the_session_keeps_a_rejected_reply_as_a_private_artifact(tmp_path):
    """The runtime exception reaches the session's call seam, which keeps
    the reply in the run's artifacts whatever store the receipt has."""
    from quadratus.runtime import _attach_response
    reply = "Reading.\n" + ONE + "\nPATCH:\n```diff\n" + DIFF + "```"

    def invoke(*args, **kwargs):
        raise _attach_response(ProviderError("Bounded editor returned no PATCH (more than one PATCH: marker)."),
                               reply, "multiple-markers")
    store = ArtifactStore(tmp_path / "artifacts")
    session = Session("goal", store, invoke, config=SessionConfig())
    with pytest.raises(ProviderError) as caught:
        session._invoke_model_call("grok:worker", "edit", allow_writes=False)
    artifact = caught.value.response_artifact
    assert store.get(artifact) == reply
