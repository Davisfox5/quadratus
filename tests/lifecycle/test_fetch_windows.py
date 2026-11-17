"""Exact retrieval behind the index (root on 6c1b922): a FETCH returns the
original in exact windows; a window that ends before the artifact's end
names the next request, and each window is one fetch. Real controller
below scripted vendor CLIs; the planner's own FETCH replies drive it."""
import pytest

from quadratus.session import _FETCH_CHAR_CAP
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, DECL_T2, FILES, Script

PLAN = "REQUIREMENTS:\nR1: add returns the sum\nR2: README documents add\n" + DECL_T1 + "\nCOVERS: R1"


def _run(tmp_path, monkeypatch, detail):
    seen = dict(prompts=[], ref=None, original=None, declared=False)
    default = Script()

    def lead(call, replay):
        return default._lead(call, replay) + ("\n" + detail if call.task == "t1" else "")

    def planner(call, replay):
        if len(replay.of("orchestrator")) == 1:
            return PLAN
        if seen["declared"]:
            return "DONE"
        if seen["ref"] is None:
            candidates = [p for p in replay._texts() if "draft" in replay._kinds(p) and detail in p.read_text()]
            assert len(candidates) == 1
            seen["ref"], seen["original"] = candidates[0].stem, candidates[0].read_text()
        seen["prompts"].append(call.prompt)
        if "## Fetched artifacts" not in call.prompt:
            return "FETCH: " + seen["ref"]
        # Only the last window decides: an earlier window's "remain" line
        # stays in the prompt, since fetched material accumulates.
        last = call.prompt.rsplit("[artifact " + seen["ref"], 1)[1]
        if "characters remain" in last:
            offset = last.rsplit("FROM ", 1)[1].split("'", 1)[0]
            return f"FETCH: {seen['ref']} FROM {offset}"
        seen["declared"] = True
        return DECL_T2 + "\nCOVERS: R2"
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=planner, lead=lead,
                                                **{"requirements-review": lambda c, r: "COMPLETE"}),
                   files=FILES, max_tasks=3, record_complete=False)
    return replay, seen


@pytest.mark.requirements_ledger
def test_an_original_under_the_window_comes_back_whole_in_one_fetch(tmp_path, monkeypatch):
    detail = "LONG_JSON_DETAIL_" + ("evidence-value-" * 1800)
    assert 25_000 < len(detail) < _FETCH_CHAR_CAP
    replay, seen = _run(tmp_path, monkeypatch, detail)
    assert seen["ref"] is not None, replay.result.error
    assert detail not in seen["prompts"][0], "the index carries the id, not the payload"
    assert seen["original"] in seen["prompts"][1], "FETCH returns the exact original"
    assert "whole]" in seen["prompts"][1] and "characters remain" not in seen["prompts"][1]
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]
    assert detail not in replay.of("lead")[1].prompt


@pytest.mark.requirements_ledger
def test_an_original_over_the_window_is_reconstructed_across_exact_windows(tmp_path, monkeypatch):
    detail = "LONG_JSON_DETAIL_" + "".join(f"<row {i:05d} evidence-value>" for i in range(2600))
    assert len(detail) > _FETCH_CHAR_CAP + 1_000
    replay, seen = _run(tmp_path, monkeypatch, detail)
    assert seen["ref"] is not None, replay.result.error
    first, second = seen["prompts"][1], seen["prompts"][2]
    body = seen["original"]
    assert body[:_FETCH_CHAR_CAP] in first and "[...truncated]" not in first
    assert f"'FETCH: {seen['ref']} FROM {_FETCH_CHAR_CAP}'" in first
    assert body[_FETCH_CHAR_CAP:] in second, "the second window is the exact remainder"
    start = second.index(body[_FETCH_CHAR_CAP:_FETCH_CHAR_CAP + 40])
    assert body[:40] not in second[start - 200:start], "windows do not repeat what was already shown"
    whole = first.split("## Fetched artifacts", 1)[1]
    recon = whole.split("]\n", 1)[1].split("\n[", 1)[0] + second.split("## Fetched artifacts", 1)[1].rsplit("]\n", 1)[1]
    assert recon.startswith(body), "the windows reconstruct the original exactly"
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]
    fetches = [c for c in replay.of("orchestrator") if c.prompt.count("## Fetched artifacts")]
    assert len(fetches) == 2, "each window was one fetch"
