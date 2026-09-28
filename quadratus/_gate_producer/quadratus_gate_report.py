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
exit status, collection errors, per-phase counts, and for each failed phase
the exception's module-qualified type. The report carries the nonce the
harness set for this one invocation, so a stale or foreign file is refused.
"""

import hashlib
import json
import os

PRODUCER = "quadratus-pytest/3"
MAX_FAILURES = 200

import pytest  # noqa: E402 -- after the constants a reader of this file wants first


def pytest_addoption(parser):
    parser.addoption("--quadratus-report", action="store", default=None,
                     help="quadratus: write the gate attribution report here")


def pytest_configure(config):
    path = config.getoption("--quadratus-report")
    if path:
        config.pluginmanager.register(_Recorder(path), "quadratus-gate-recorder")


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
        # Identity, not a name: pytest's own outcome exceptions report their
        # module as "builtins", so a name can be spoofed; the type cannot.
        report.quadratus_assertion = call.excinfo is not None and call.excinfo.type is AssertionError

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            if report.passed:
                self.counts["passed"] += 1
            elif report.failed:
                self.counts["failed"] += 1
                self._fail(report)
            elif report.skipped:
                self.counts["skipped"] += 1
        elif report.failed:
            self.counts["errors"] += 1
            self._fail(report)
        elif report.skipped and report.when == "setup":
            self.counts["skipped"] += 1

    def _fail(self, report):
        if len(self.failures) >= MAX_FAILURES:
            self.truncated = True
            return
        self.failures.append(dict(nodeid=report.nodeid[:300], when=report.when,
                                  exc_type=getattr(report, "quadratus_exc", None),
                                  assertion=getattr(report, "quadratus_assertion", False) is True))

    def pytest_sessionfinish(self, session, exitstatus):
        with open(__file__, "rb") as source:
            digest = hashlib.sha256(source.read()).hexdigest()
        data = dict(producer=PRODUCER, nonce=os.environ.get("QUADRATUS_GATE_NONCE", ""),
                    module_file=os.path.realpath(__file__), module_sha256=digest,
                    exitstatus=int(exitstatus), collected=self.collected,
                    collect_errors=self.collect_errors, counts=self.counts,
                    failures=self.failures, truncated=self.truncated)
        with open(self.path, "w", encoding="utf-8") as out:
            json.dump(data, out)
