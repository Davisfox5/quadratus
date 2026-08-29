"""Tests for browser evidence. Real Chromium when available, skipped cleanly
when not -- the module is an optional dependency by design."""

from __future__ import annotations

import pytest

from quadratus.browser import PageEvidence, render_page

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def clean_page(tmp_path_factory):
    root = tmp_path_factory.mktemp("pages")
    page = root / "clean.html"
    page.write_text(
        "<!doctype html><html><head><title>Clean Page</title></head>"
        "<body><h1>hello</h1></body></html>",
        encoding="utf-8",
    )
    return page


@pytest.fixture(scope="module")
def broken_page(tmp_path_factory):
    root = tmp_path_factory.mktemp("pages")
    page = root / "broken.html"
    # Throws after load -- the failure a compile step cannot see.
    page.write_text(
        "<!doctype html><html><head><title>Broken Page</title></head>"
        "<body><script>setTimeout(function(){ throw new Error('late boom'); }, 50);"
        "</script></body></html>",
        encoding="utf-8",
    )
    return page


def test_a_clean_page_reports_clean(clean_page, tmp_path):
    got = render_page(str(clean_page), out_dir=tmp_path, wait_ms=300)
    assert got.clean
    assert got.title == "Clean Page"
    assert got.screenshot_path.endswith(".png")


def test_a_late_console_error_is_caught(broken_page, tmp_path):
    got = render_page(str(broken_page), out_dir=tmp_path, wait_ms=500)
    assert not got.clean
    assert any("late boom" in e for e in got.console_errors)


def test_evidence_is_persisted_beside_the_screenshot(clean_page, tmp_path):
    render_page(str(clean_page), out_dir=tmp_path, wait_ms=300)
    assert (tmp_path / "evidence.json").exists()
    assert (tmp_path / "page.png").exists()


def test_render_carries_facts_not_judgement(clean_page, tmp_path):
    got = render_page(str(clean_page), out_dir=tmp_path, wait_ms=300)
    block = got.render()
    assert "no console errors" in block
    assert "screenshot" in block


def test_missing_playwright_is_a_clear_error(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("nope")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    from quadratus.browser import PlaywrightMissing
    with pytest.raises(PlaywrightMissing, match="pip install playwright"):
        render_page("x.html", out_dir="/tmp/unused")


def test_evidence_is_frozen():
    import dataclasses
    ev = PageEvidence(url="u", title="t", screenshot_path="s")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.clean = False
