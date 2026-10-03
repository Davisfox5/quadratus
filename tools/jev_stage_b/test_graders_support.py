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


def test_a_provoked_400_is_excused_once_with_its_exact_response_and_nothing_else_is():
    C = _conftest()
    diag = "Failed to load resource: the server responded with a status of 400 (Bad Request)"
    one = "http://127.0.0.1:52055/api/projects/one"
    errors, http = [], []
    C.sort_console_error(diag, one, errors, http)
    C.sort_console_error("Uncaught TypeError: x is undefined", "http://127.0.0.1:52055/static/app.js", errors, http)
    assert errors == ["console: Uncaught TypeError: x is undefined"] and http[0]["status"] == 400
    expected = (("PATCH", "/api/projects/one", 400),)
    ok = [dict(method="PATCH", url=one, status=400)]
    assert C.unexpected_console_errors([], http, ok, expected) == ([], [])
    # No matching response: not excused, and the expectation is reported missing.
    assert C.unexpected_console_errors([], http, [], expected) == (["PATCH /api/projects/one -> 400"], [http[0]["text"]])
    # Codex's table (3a1eef9 review): unrelated error responses fail even when
    # the expected one was seen, including the same project's GETs and a
    # different method at the exact same URL.
    for extra in (dict(method="PATCH", url="http://127.0.0.1:52055/api/projects/boom", status=500),
                  dict(method="GET", url="http://127.0.0.1:52055/api/unrelated", status=400),
                  dict(method="GET", url="http://127.0.0.1:52055/api/projects/one/clips", status=400),
                  dict(method="GET", url=one, status=400)):
        missing, failures = C.unexpected_console_errors([], http, ok + [extra], expected)
        assert missing == [] and failures == [f"unexpected HTTP error response: {extra['method']} {extra['url']} -> {extra['status']}"], extra
    # Two diagnostics at the expected path and status with one expected response: the second fails.
    twice = []
    C.sort_console_error(diag, one, [], twice)
    C.sort_console_error(diag, one, [], twice)
    assert C.unexpected_console_errors([], twice, ok, expected) == ([], [twice[1]["text"]])
    # A path fragment is not a match: the expectation names the exact path.
    assert C.unexpected_console_errors([], http, ok, (("PATCH", "/api/projects/", 400),))[0] == ["PATCH /api/projects/ -> 400"]
    # Without an expectation every error response and diagnostic fails, as before.
    assert C.unexpected_console_errors(errors, http, ok) == (
        [], errors + ["unexpected HTTP error response: PATCH " + one + " -> 400", http[0]["text"]])
