"""f2-project-search: client-side search over the project list."""
import urllib.parse

from conftest import no_console_errors
from support import run_baseline_regressions

SEED = [{"name": "Alpha Cup"}, {"name": "Beta Bowl"}, {"name": "alpha reserve"}, {"name": "Gamma Night"}]


def _visible_names(page):
    return page.evaluate("""() => Array.from(document.querySelectorAll('.project-card'))
        .filter(c => !c.hidden && getComputedStyle(c).display !== 'none')
        .map(c => c.querySelector('.name').textContent.trim()).sort()""")


def _open(live, page):
    page.goto(live.url + "/")
    page.wait_for_selector(".project-card", timeout=5000)
    page.wait_for_function("() => document.querySelectorAll('.project-card').length === 4", timeout=5000)


def _requests_counter(page):
    counter = {"api": 0}
    page.on("request", lambda req: counter.__setitem__("api", counter["api"] + 1) if "/api/" in req.url else None)
    return counter


def test_R1_typing_filters_cards_by_name_without_a_request_or_reload(live_factory, page_factory):
    live = live_factory(SEED)
    page = page_factory()
    _open(live, page)
    assert page.evaluate("() => { const s = document.getElementById('project-search'); const c = document.getElementById('projects-container'); "
                         "return !!s && !!c && !!(s.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING) && !!s.closest('#project-list-screen'); }"), \
        "#project-search must sit inside #project-list-screen before #projects-container"
    counter = _requests_counter(page)
    page.evaluate("() => { window.__graderMarker = 'kept'; }")
    page.fill("#project-search", "ALPHA")
    page.wait_for_function("() => Array.from(document.querySelectorAll('.project-card')).filter(c => !c.hidden && getComputedStyle(c).display !== 'none').length === 2", timeout=5000)
    assert _visible_names(page) == ["Alpha Cup", "alpha reserve"]
    page.fill("#project-search", "bowl")
    page.wait_for_function("() => Array.from(document.querySelectorAll('.project-card')).filter(c => !c.hidden && getComputedStyle(c).display !== 'none').length === 1", timeout=5000)
    assert _visible_names(page) == ["Beta Bowl"]
    assert counter["api"] == 0, "filtering must not call the server"
    assert page.evaluate("() => window.__graderMarker") == "kept", "the page reloaded"
    no_console_errors(page)


def test_R2_count_and_clear_control(live_factory, page_factory):
    live = live_factory(SEED)
    page = page_factory()
    _open(live, page)
    assert page.evaluate("() => (document.getElementById('project-search-count') || {textContent: 'missing'}).textContent.trim()") == ""
    page.fill("#project-search", "alpha")
    page.wait_for_function("() => document.getElementById('project-search-count').textContent.trim() === '2 of 4 projects'", timeout=5000)
    page.click("#btn-project-search-clear")
    page.wait_for_function("() => document.getElementById('project-search').value === '' && "
                           "Array.from(document.querySelectorAll('.project-card')).filter(c => !c.hidden && getComputedStyle(c).display !== 'none').length === 4", timeout=5000)
    assert page.evaluate("() => document.getElementById('project-search-count').textContent.trim()") == ""
    no_console_errors(page)


def test_R3_the_query_lives_in_the_url_hash_and_is_restored_on_load(live_factory, page_factory):
    live = live_factory(SEED)
    page = page_factory()
    _open(live, page)
    page.fill("#project-search", "gamma night")
    page.wait_for_function("() => location.hash === '#q=' + encodeURIComponent('gamma night')", timeout=5000)
    page.fill("#project-search", "")
    page.wait_for_function("() => location.hash === '' || location.hash === '#' || location.hash === '#q='", timeout=5000)
    assert page.evaluate("() => location.hash in {'': 1, '#': 1}"), page.evaluate("() => location.hash")
    fresh = page_factory()
    fresh.goto(live.url + "/#q=" + urllib.parse.quote("beta"))
    fresh.wait_for_selector(".project-card", timeout=5000)
    fresh.wait_for_function("() => document.getElementById('project-search').value === 'beta' && "
                            "Array.from(document.querySelectorAll('.project-card')).filter(c => !c.hidden && getComputedStyle(c).display !== 'none').length === 1", timeout=5000)
    assert _visible_names(fresh) == ["Beta Bowl"]
    no_console_errors(page)
    no_console_errors(fresh)


def test_R4_no_match_shows_a_one_line_message(live_factory, page_factory):
    live = live_factory(SEED)
    page = page_factory()
    _open(live, page)
    hidden_js = "() => { const e = document.getElementById('project-search-empty'); return !e || e.hidden || getComputedStyle(e).display === 'none'; }"
    assert page.evaluate(hidden_js), "the empty message must be hidden before any query"
    page.fill("#project-search", "zzz-no-such-project")
    page.wait_for_function("() => { const e = document.getElementById('project-search-empty'); return e && !e.hidden && getComputedStyle(e).display !== 'none'; }", timeout=5000)
    text = page.evaluate("() => document.getElementById('project-search-empty').innerText.trim()")
    assert text and "\n" not in text, text
    assert _visible_names(page) == []
    page.fill("#project-search", "alpha")
    page.wait_for_function(hidden_js, timeout=5000)
    no_console_errors(page)


def test_R5_baseline_tests_still_pass_and_the_list_logs_no_console_errors(project_root, tmp_path, live_factory, page_factory):
    live = live_factory(SEED)
    page = page_factory()
    _open(live, page)
    page.fill("#project-search", "a")
    page.wait_for_timeout(500)
    no_console_errors(page)
    failures = run_baseline_regressions(project_root, tmp_path)
    assert failures == [], "\n\n".join(failures)
