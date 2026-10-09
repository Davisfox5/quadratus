"""f5-empty-state: an empty-state panel on the project list."""
from conftest import no_console_errors
from support import run_baseline_regressions


def _panel_visible(page):
    return page.evaluate("""() => {
        const el = document.getElementById('projects-empty');
        if (!el) return 'missing';
        if (el.hidden || getComputedStyle(el).display === 'none') return 'hidden';
        return 'visible';
    }""")


def test_R1_with_no_projects_the_panel_shows_and_its_button_focuses_the_name_field(live_factory, page_factory):
    live = live_factory([])
    page = page_factory()
    page.goto(live.url + "/")
    page.wait_for_selector("#projects-empty", state="attached", timeout=5000)
    page.wait_for_function("() => { const e = document.getElementById('projects-empty'); return e && !e.hidden; }", timeout=5000)
    container = page.evaluate("() => !!document.querySelector('#projects-container #projects-empty')")
    assert container, "#projects-empty must live inside #projects-container"
    text = page.evaluate("() => document.getElementById('projects-empty').innerText.trim()")
    assert text and "\n" not in text.replace("\r", "").strip().split("\n")[0], text
    assert len(text.split("\n")) <= 3, text  # one explanation line plus the button
    assert page.evaluate("() => !!document.querySelector('#projects-container p')") is False or \
        "No projects yet. Create one above." not in page.evaluate("() => document.getElementById('projects-container').innerText"), \
        "the old inline 'No projects yet' paragraph must be gone"
    page.click("#btn-projects-empty-create")
    assert page.evaluate("() => document.activeElement && document.activeElement.id") == "new-project-name"
    no_console_errors(page)


def test_R2_creating_the_first_project_hides_the_panel_without_a_reload(live_factory, page_factory):
    live = live_factory([])
    page = page_factory()
    page.goto(live.url + "/")
    page.wait_for_function("() => { const e = document.getElementById('projects-empty'); return e && !e.hidden; }", timeout=5000)
    page.evaluate("() => { window.__graderMarker = 'kept'; }")
    page.fill("#new-project-name", "First one")
    page.click("#btn-create-project")
    page.wait_for_selector(".project-card", timeout=5000)
    page.wait_for_function("() => { const e = document.getElementById('projects-empty'); return !e || e.hidden || getComputedStyle(e).display === 'none'; }", timeout=5000)
    assert page.evaluate("() => window.__graderMarker") == "kept", "the page reloaded"
    assert len(live.api("/api/projects")) == 1
    no_console_errors(page)


def test_R3_deleting_the_last_project_shows_the_panel_again(live_factory, page_factory):
    live = live_factory([{"name": "Only one"}])
    page = page_factory()
    page.on("dialog", lambda d: d.accept())
    page.goto(live.url + "/")
    page.wait_for_selector(".project-card", timeout=5000)
    assert _panel_visible(page) in ("hidden", "missing")
    page.evaluate("() => { window.__graderMarker = 'kept'; }")
    page.click(".project-card .delete-btn")
    page.wait_for_function("() => { const e = document.getElementById('projects-empty'); return e && !e.hidden && getComputedStyle(e).display !== 'none'; }", timeout=5000)
    assert page.evaluate("() => document.querySelectorAll('.project-card').length") == 0
    assert page.evaluate("() => window.__graderMarker") == "kept", "the page reloaded"
    no_console_errors(page)


def test_R4_baseline_tests_still_pass(project_root, tmp_path):
    failures = run_baseline_regressions(project_root, tmp_path)
    assert failures == [], "\n\n".join(failures)
