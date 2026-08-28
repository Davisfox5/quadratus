"""The GUI treats the brand assets as optional decoration, not a dependency."""

from multi_llm.gui import brand_asset, inline_mark


def test_a_shipped_asset_resolves_to_a_real_file():
    mark = brand_asset("mark-large.svg")
    assert mark is not None
    assert mark.read_text(encoding="utf-8").lstrip().startswith("<svg")


def test_the_favicon_is_present_for_launch():
    assert brand_asset("favicon.svg") is not None


def test_a_missing_asset_is_none_rather_than_an_error():
    assert brand_asset("no-such-mark.svg") is None


def test_each_size_band_gets_the_drawing_made_for_it():
    # The small mark inverts -- an ink tile rather than a paper one -- so the
    # tile fill is enough to tell the three drawings apart.
    assert 'width="120" height="120"' in inline_mark(120)
    assert 'width="52" height="52"' in inline_mark(52)
    assert 'width="16" height="16"' in inline_mark(16)
    assert 'rx="20" fill="#191711"' in inline_mark(16)
    assert 'rx="20" fill="#F1E4CF"' in inline_mark(52)
    assert 'rx="20" fill="#F1E4CF"' in inline_mark(120)


def test_the_large_drawing_is_not_stretched_into_the_medium_band():
    # mark-large carries a hairline keyline the medium drawing drops; if the
    # header ever reaches for the wrong file this catches it.
    assert "stroke-opacity=\"0.35\"" in inline_mark(120)
    assert "stroke-opacity=\"0.35\"" not in inline_mark(52)
