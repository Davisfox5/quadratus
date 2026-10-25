"""Effort per call is decided from the harness's own task facts
(quadratus.effort): deterministic, explicit, on the vendor's ladder, with
bounded seats fixed, judging seats floored, and ultra never selected."""
import pytest

from quadratus import effort as E


def level(**kw):
    kw.setdefault("vendor", "openai")
    return E.select(**kw).level


def test_every_decision_carries_a_level_reasons_and_the_policy_version():
    decision = E.select(role="lead", vendor="claude", complexity="standard", kind="backend")
    assert decision.level == "medium" and decision.reasons and decision.policy == "effort/1"
    assert decision.to_dict()["level"] == "medium" and decision.wanted is None and not decision.seat_fixed


@pytest.mark.parametrize("role, expected", [
    ("orchestrator", "high"), ("decide", "low"), ("requirements-review", "medium"), ("consultant", "medium"),
    ("interview", "low"), ("auditor", "high"), ("verifier", "high"), ("capture-redeclare", "low"),
    ("fixture-supply", "low"), ("worker", "low"),
])
def test_control_plane_and_judging_roles_have_fixed_levels(role, expected):
    assert level(role=role, complexity="simple") == expected


@pytest.mark.parametrize("complexity, kind, expected", [
    ("rote", "general", "low"), ("simple", "backend", "medium"), ("standard", "backend", "medium"),
    ("complex", "backend", "high"), ("simple", "security", "high"), ("standard", "concurrency", "high"),
    ("standard", "perf", "high"), ("standard", "architect", "high"),
])
def test_the_lead_follows_difficulty_and_the_hard_kinds(complexity, kind, expected):
    assert level(role="lead", complexity=complexity, kind=kind) == expected


def test_security_work_class_is_high_whatever_the_kind_says():
    assert level(role="lead", complexity="simple", kind="general", work_class="security") == "high"
    assert level(role="lead", complexity="simple", security=True) == "high"


def test_reviews_start_at_medium_and_rise_with_difficulty_never_below():
    assert level(role="collaborator", complexity="simple", kind="backend") == "medium"
    assert level(role="collaborator", complexity="complex", kind="backend") == "high"
    assert level(role="collaborator", complexity="simple", kind="security") == "high"
    assert level(role="collaborator", complexity="simple", kind="review") == "high"
    assert level(role="design-review", complexity="rote") == "medium", "a rote task does not buy a cheap review"
    assert level(role="recheck", complexity="simple", cycle=1) == "medium"
    assert level(role="recheck", complexity="simple", cycle=2) == "high"
    assert level(role="recheck", complexity="simple", cycle=1, impact="data-loss") == "high"


def test_a_second_fix_cycle_raises_the_editor_one_step_and_the_draft_never_moves():
    assert level(role="revision", complexity="simple", cycle=1) == "medium"
    assert level(role="revision", complexity="simple", cycle=2) == "high"
    assert level(role="gate-fix", complexity="rote", cycle=2) == "medium"
    assert level(role="lead", complexity="simple", cycle=3) == "medium", "the draft is never a fix cycle"
    decision = E.select(role="revision", vendor="claude", complexity="simple", cycle=2)
    assert any("fix cycle 2" in r for r in decision.reasons)


def test_the_elevated_rule_needs_complexity_risk_and_a_repeated_cycle_and_a_ladder_above_high():
    assert level(role="revision", complexity="complex", security=True, cycle=2) == "xhigh"
    assert level(role="recheck", complexity="complex", impact="integrity", cycle=2) == "xhigh"
    assert level(role="revision", complexity="complex", security=True, cycle=1) == "high", "one cycle is not evidence"
    assert level(role="revision", complexity="standard", security=True, cycle=2) == "high", "standard work is not complex"
    assert level(role="revision", complexity="complex", kind="backend", cycle=2) == "high", "no risk, no elevation"
    # grok's ladder tops at xhigh; claude's at max; a per-model ladder without a level above high stays high.
    assert level(role="revision", vendor="grok", complexity="complex", security=True, cycle=2) == "xhigh"
    assert level(role="revision", vendor="claude", complexity="complex", security=True, cycle=2) == "max"
    bounded = E.select(role="revision", vendor="openai", complexity="complex", security=True, cycle=2,
                       supported=("low", "medium", "high"))
    assert bounded.level == "high" and any("no level above high" in r for r in bounded.reasons)


def test_ultra_is_never_selected_and_never_validates():
    decision = E.select(role="revision", vendor="openai", complexity="complex", security=True, cycle=4,
                        supported=("low", "medium", "high", "xhigh", "max", "ultra"))
    assert decision.level == "xhigh"
    assert "ultra" not in E.ladder_for("openai", ("low", "ultra")) and E.validate("ultra", "openai", ("ultra",)).startswith("ultra is never sent")


def test_a_bounded_seat_keeps_the_effort_its_roster_row_fixes():
    worker = E.select(role="worker", vendor="grok", restricted=True, seat_effort="low", complexity="complex", security=True)
    assert worker.level == "low" and worker.seat_fixed and "bounded seat" in worker.reasons[0]
    expert = E.select(role="worker", vendor="grok", restricted=True, seat_effort="high")
    assert expert.level == "high" and expert.seat_fixed
    terra = E.select(role="worker", vendor="openai", restricted=True, seat_effort="high", supported=("low", "medium"))
    assert terra.level == "high" and any("refused, not lowered" in r for r in terra.reasons)
    assert E.validate(terra.level, "openai", ("low", "medium")) == "high is not on the openai ladder (low, medium)"


def test_validation_names_the_ladder_and_an_empty_level_is_refused():
    assert E.validate("high", "claude") == ""
    assert E.validate("xhigh", "claude") == "xhigh is not on the claude ladder (low, medium, high, max)"
    assert E.validate("max", "grok").startswith("max is not on the grok ladder")
    assert E.validate("", "openai") == "no effort level selected"
    assert E.validate("high", "openai", ("low", "medium")) == "high is not on the openai ladder (low, medium)"
    assert E.ladder_for("openai", ("high", "low", "medium")) == ("low", "medium", "high")
    assert E.ladder_for("openai", ()) == E.VENDOR_LADDERS["openai"]


def test_a_base_level_the_ladder_lacks_is_never_lowered_only_refused():
    """The floors are floors: a review stays at medium, an audit at high,
    and a model whose ladder lacks that level is refused by name."""
    decision = E.select(role="auditor", vendor="openai", supported=("low", "medium"))
    assert decision.level == "high" and decision.wanted is None
    assert E.validate(decision.level, "openai", ("low", "medium")) == "high is not on the openai ladder (low, medium)"


def test_an_unknown_role_gets_the_ordinary_level_never_the_cli_default():
    decision = E.select(role="something-new", vendor="claude")
    assert decision.level == "medium" and "never the CLI's own default" in decision.reasons[0]
