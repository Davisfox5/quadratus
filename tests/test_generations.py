"""Immutable record generations and the manifest rules (blueprint section 8).

Pure validation only: no file is written here, and the durable publisher
that will call these rules is not part of this batch.
"""

import pytest

from quadratus import generations as G

D1, D2, D3 = "1" * 64, "2" * 64, "3" * 64


def _gen(number, records, parent=None, **over):
    return G.Generation(number=number, schema=G.SCHEMA_VERSION, records=records, parent=parent, **over)


def test_record_paths_are_content_addressed_and_validated():
    assert G.record_path("findings", D1) == "records/findings-1111111111111111.json"
    with pytest.raises(ValueError):
        G.record_path("Findings", D1)
    with pytest.raises(ValueError):
        G.record_path("findings", "abc")


def test_first_publication_writes_everything_and_names_no_parent():
    pub = G.plan_publication(None, expected=None, records={"findings": D1, "plan": D2})
    assert pub.generation.number == 1 and pub.generation.parent is None
    assert set(pub.write) == {G.record_path("findings", D1), G.record_path("plan", D2)} and pub.keep == ()


def test_unchanged_records_are_kept_and_changed_ones_get_new_paths():
    g1 = _gen(1, {"findings": D1, "plan": D2})
    pub = G.plan_publication(g1, expected=1, records={"findings": D1, "plan": D3})
    assert pub.generation.number == 2 and pub.generation.parent == 1
    assert pub.keep == (G.record_path("findings", D1),) and pub.write == (G.record_path("plan", D3),)


def test_stale_expectation_is_a_conflict_not_a_merge():
    g1 = _gen(1, {"findings": D1})
    with pytest.raises(G.GenerationConflict):
        G.plan_publication(g1, expected=None, records={"findings": D2})
    with pytest.raises(G.GenerationConflict):
        G.plan_publication(g1, expected=2, records={"findings": D2})
    with pytest.raises(G.GenerationConflict):
        G.plan_publication(None, expected=1, records={"findings": D2})


def test_a_committed_path_is_never_given_other_bytes():
    g1 = _gen(1, {"findings": D1})
    colliding = "1" * 16 + "f" * 48  # same published name, different bytes
    with pytest.raises(G.ImmutableRecordViolation):
        G.plan_publication(g1, expected=1, records={"findings": colliding})
    g2 = _gen(2, {"findings": D2}, parent=1)
    with pytest.raises(G.ImmutableRecordViolation):
        G.plan_publication(g2, expected=2, records={"findings": colliding}, history=[g1])


def test_generation_rows_are_consecutive_and_schema_bound():
    with pytest.raises(ValueError):
        _gen(2, {})
    with pytest.raises(ValueError):
        _gen(3, {}, parent=1)
    with pytest.raises(ValueError):
        G.Generation(number=1, schema="other/9", records={})
    with pytest.raises(ValueError):
        _gen(1, {}, reserved_unfinished=-1)
    g = _gen(1, {"plan": D1})
    assert g.clean and g.paths == {"plan": G.record_path("plan", D1)} and g.to_dict()["digest"] == g.digest


def test_no_fallback_past_started_effects_or_unfinished_spend():
    g1 = _gen(1, {"plan": D1})
    g2 = _gen(2, {"plan": D2}, parent=1, unsettled=("O-7",), reserved_unfinished=200_000)
    allowed, why = G.fallback_allowed(g2, g1)
    assert not allowed and "O-7" in why and "200,000" in why
    assert G.fallback_allowed(g2, g2) == (True, "same generation")
    assert not G.fallback_allowed(g1, g2)[0]
    clean2 = _gen(2, {"plan": D2}, parent=1)
    assert not G.fallback_allowed(clean2, g1)[0]
    pub = G.plan_publication(g2, expected=2, records={"plan": D2}, history=[g1], unsettled=["O-7"],
                             reserved_unfinished=200_000)
    assert not pub.generation.clean and pub.write == ()
