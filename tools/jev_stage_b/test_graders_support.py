"""The graders' shared console collector, offline: a deliberately provoked
HTTP error is excused only with the response as evidence (Codex review of
e3deaed, P1 #3). No browser."""
import importlib.util
import sys
from pathlib import Path

GRADERS = Path(__file__).parent / "graders"


def _conftest():
    sys.path.insert(0, str(GRADERS))
    spec = importlib.util.spec_from_file_location("stage_b_grader_conftest", GRADERS / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_provoked_400_is_excused_only_with_its_response_and_nothing_else_is():
    C = _conftest()
    errors, http = [], []
    C.sort_console_error("Failed to load resource: the server responded with a status of 400 (Bad Request)",
                         "http://127.0.0.1:52055/api/projects/abc", errors, http)
    C.sort_console_error("Uncaught TypeError: x is undefined", "http://127.0.0.1:52055/static/app.js", errors, http)
    assert errors == ["console: Uncaught TypeError: x is undefined"] and http[0]["status"] == 400
    responses = [dict(method="PATCH", url="http://127.0.0.1:52055/api/projects/abc", status=400)]
    missing, failures = C.unexpected_console_errors([], http, responses, expected_http=(("PATCH", "/api/projects/", 400),))
    assert missing == [] and failures == []
    # The same diagnostic with no matching response is not excused: no evidence, no excuse.
    missing, failures = C.unexpected_console_errors([], http, [], expected_http=(("PATCH", "/api/projects/", 400),))
    assert missing == ["PATCH /api/projects/ -> 400"] and failures == [http[0]["text"]]
    # A different status or URL is never excused by the expectation.
    other = [dict(text="console: Failed to load resource: the server responded with a status of 500 (Internal Server Error)",
                  url="http://127.0.0.1:52055/api/projects/abc", status=500)]
    missing, failures = C.unexpected_console_errors([], other, responses, expected_http=(("PATCH", "/api/projects/", 400),))
    assert failures == [other[0]["text"]]
    # Without an expectation every HTTP error diagnostic fails the page, as before.
    assert C.unexpected_console_errors(errors, http, responses) == ([], errors + [http[0]["text"]])
