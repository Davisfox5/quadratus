"""The replay pack runs in CI: controller determinism, not live reliability.

Every case variant is one parametrised test, so a regression names the case
and the variant that caught it. The structural tests pin the pack's own
rules: twenty cases, a source line per case, and a good and a bad variant
wherever a grader's reply is judged. The discrimination test proves the bad
variants are not decorative: a graded case's good expectations must fail
against its bad script, or the pair is not telling the two apart.
"""

import pytest

from . import runner

CASES = runner.load_cases()
PAIRS = [(case, variant) for case in CASES for variant in runner.variants_of(case)]


@pytest.mark.parametrize("case,variant", PAIRS,
                         ids=[f"{c['id']}[{v}]" for c, v in PAIRS])
def test_case(case, variant, tmp_path):
    result = runner.run_case(case, variant, tmp_path)
    assert result.passed, "\n".join(result.failures)


def test_pack_is_structurally_sound():
    assert runner.pack_problems(CASES) == []
    assert len(CASES) == 20


def test_every_case_cites_its_incident():
    for case in CASES:
        assert case["source"].startswith("Report"), case["id"]
        assert case["shape"] in {"happy-path", "fake-done", "blocked-path", "noisy-tool", "scope-trap",
                                 "permission-trap", "compaction", "idempotence", "resume", "cost-cap"}, case["id"]


def test_graded_cases_tell_good_from_bad(tmp_path):
    graded = [c for c in CASES if c.get("grader")]
    assert graded, "the pack has graded cases"
    for case in graded:
        good = runner.resolve_variant(case, "good")
        bad = runner.resolve_variant(case, "bad")
        crossed = dict(bad)
        crossed["expect"] = good["expect"]
        out = runner.execute(crossed, tmp_path / case["id"])
        assert runner.check(good["expect"], out), f"{case['id']}: the bad script satisfies the good expectations"


def test_report_carries_the_label():
    text = runner.render([runner.CaseResult("x", "base", "happy-path", True, [])], [])
    assert runner.LABEL in text
    assert "live reliability not measured" in text


def test_an_unscripted_reply_fails_the_case_rather_than_guessing(tmp_path):
    case = {"id": "gap", "shape": "happy-path", "source": "Report", "drive": {"kind": "next_task"},
            "session": {"project": False}, "script": [], "expect": {"outcome": "completed"}}
    result = runner.run_case(case, "base", tmp_path)
    assert not result.passed
    assert "ReplayGap" in result.failures[0]
