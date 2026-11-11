"""Harness-owned pytest report producer (quadratus phase 3, #25).

Loaded into a check's own pytest only when the check declares
``--quadratus-report={report}``. The harness copies this file into a
directory it owns for that one invocation, under a module name derived from
the invocation's nonce, and names that module in ``PYTEST_PLUGINS``: no
project module can already carry the name, so import order cannot swap in
another producer. The report states this module's own file and source
digest, which the harness compares with the copy it placed. It imports
nothing from quadratus, so it runs under whatever interpreter the project
uses.

It records facts pytest already has and never interprets prose: the session
exit status, collection errors, per-phase counts, for each failed phase the
exception's module-qualified type, and the node id of each case that passed
(a quiet run prints no case lines, so the report is what names them). The
report carries the nonce the harness set for this one invocation, so a stale
or foreign file is refused.
"""

import hashlib
import json
import os

PRODUCER = "quadratus-pytest/5"
MAX_FAILURES = 200
#: Passed node ids are bounded in number and in the bytes they add to the
#: report, which the harness reads only up to a fixed size; past either the
#: list stops and ``passed_truncated`` says so.
MAX_PASSED = 20000
MAX_PASSED_BYTES = 600000

import pytest  # noqa: E402 -- after the constants a reader of this file wants first


def pytest_addoption(parser):
    parser.addoption("--quadratus-report", action="store", default=None,
                     help="quadratus: write the gate attribution report here")


def pytest_configure(config):
    path = config.getoption("--quadratus-report")
    if path:
        config.pluginmanager.register(_Recorder(path), "quadratus-gate-recorder")


def _test_code(item):
    """The code object of the test function itself, unwrapped through
    decorators; None when the item has no plain function behind it."""
    import inspect
    function = getattr(item, "function", None)
    try:
        function = inspect.unwrap(getattr(function, "__func__", function))
    except Exception:  # noqa: BLE001 -- unknown stays unknown
        return None
    return getattr(function, "__code__", None)


def _from_body(item, excinfo):
    """Whether the exception's traceback passes through the test function's
    own frame: the body ran and the failure came from it or from code it
    called. A hook that fails before or around the body (pytest_runtest_call,
    pytest_pyfunc_call) never has that frame, though pytest reports it in the
    call phase (Codex attribution assessment on 4a273a3: a call-hook
    AssertionError was admitted as a product failure with the body unrun)."""
    code = _test_code(item)
    if excinfo is None or code is None:
        return False
    tb = getattr(excinfo, "tb", None)
    while tb is not None:
        if tb.tb_frame.f_code is code:
            return True
        tb = tb.tb_next
    return False


def _case_key(name):
    """A case's identity as the harness keys it (``integration.case_key``):
    the node id, or for one over 300 characters a readable prefix plus the
    SHA256 of the whole, so two long ids never share a key."""
    if len(name) <= 300:
        return name
    return name[:200] + "\u2026sha256:" + hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()


def _type_name(excinfo):
    if excinfo is None:
        return None
    kind = excinfo.type
    return f"{kind.__module__}.{kind.__qualname__}"


class _Recorder:
    def __init__(self, path):
        self.path = path
        self.counts = dict(passed=0, failed=0, errors=0, skipped=0)
        self.failures, self.truncated = [], False
        self.passed, self.passed_bytes, self.passed_truncated = [], 0, False
        #: Expected failures, which pytest reports as skips: counted apart so
        #: the harness reads the same executed total as the runner's summary.
        self.xfailed = 0
        self.collected, self.collect_errors = 0, 0

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_errors += 1

    def pytest_collection_finish(self, session):
        self.collected = len(session.items)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        report.quadratus_exc = _type_name(call.excinfo)
        report.quadratus_body = call.when == "call" and _from_body(item, call.excinfo)
        # Identity, not a name: pytest's own outcome exceptions report their
        # module as "builtins", so a name can be spoofed; the type cannot.
        # And provenance, not the phase: only an assertion raised through the
        # test function's own frame is a test assertion.
        report.quadratus_assertion = (call.excinfo is not None and call.excinfo.type is AssertionError
                                      and report.quadratus_body)

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            if report.passed:
                self.counts["passed"] += 1
                self._pass(report.nodeid)
            elif report.failed:
                self.counts["failed"] += 1
                self._fail(report)
            elif report.skipped:
                self.counts["skipped"] += 1
                self.xfailed += hasattr(report, "wasxfail")
        elif report.failed:
            self.counts["errors"] += 1
            self._fail(report)
        elif report.skipped and report.when == "setup":
            self.counts["skipped"] += 1
            self.xfailed += hasattr(report, "wasxfail")

    def _pass(self, nodeid):
        key = _case_key(nodeid)
        size = len(json.dumps(key)) + 2
        if (self.passed_truncated or len(self.passed) >= MAX_PASSED
                or self.passed_bytes + size > MAX_PASSED_BYTES):
            self.passed_truncated = True
            return
        self.passed.append(key)
        self.passed_bytes += size

    def _fail(self, report):
        if len(self.failures) >= MAX_FAILURES:
            self.truncated = True
            return
        self.failures.append(dict(nodeid=report.nodeid[:300], when=report.when,
                                  exc_type=getattr(report, "quadratus_exc", None),
                                  assertion=getattr(report, "quadratus_assertion", False) is True,
                                  body=getattr(report, "quadratus_body", False) is True))

    def pytest_sessionfinish(self, session, exitstatus):
        with open(__file__, "rb") as source:
            digest = hashlib.sha256(source.read()).hexdigest()
        data = dict(producer=PRODUCER, nonce=os.environ.get("QUADRATUS_GATE_NONCE", ""),
                    module_file=os.path.realpath(__file__), module_sha256=digest,
                    exitstatus=int(exitstatus), collected=self.collected,
                    collect_errors=self.collect_errors, counts=self.counts,
                    failures=self.failures, truncated=self.truncated,
                    passed_cases=self.passed, passed_truncated=self.passed_truncated,
                    xfailed=self.xfailed)
        with open(self.path, "w", encoding="utf-8") as out:
            json.dump(data, out)
