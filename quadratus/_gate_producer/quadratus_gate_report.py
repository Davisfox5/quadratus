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
the exception's module-qualified type. A subtest (``unittest`` ``subTest`` or
the ``subtests`` fixture, which pytest 9 reports through the same hook as
its item) is part of its item, never an item: it is tallied apart, and its
failure makes its item fail, with the item's one failure record an
assertion only when every failure behind it is; with no failure, a skipped
subtest makes its item skipped (R21: seven passing subtests
were counted as seven more passed items than were collected). The report carries the nonce the
harness set for this one invocation, so a stale or foreign file is refused.

It also names each item it counted passed, by pytest's own node id, so a
quiet run still shows exactly which cases executed and passed (Davis,
2026-10-16; root review of 5ca254c). A subtest is never one of them: its
item is, and only when the item counted passed.

For each class of a collected item (``classes``) it attests the class's
default membership (``_class_members``, read from the live class object:
every ``test*`` callable, inherited and dynamically attached ones included),
so a whole-class claim is checked against what the runner would collect by
default rather than against the command's syntax, a configured pattern or a
static read of the file. Bounded; past the bound flagged truncated and
attesting nothing.
"""

import hashlib
import json
import os

PRODUCER = "quadratus-pytest/7"
MAX_FAILURES = 200
MAX_PASSED = 3000
MAX_CLASSES = 1000
MAX_CLASS_MEMBERS = 1000

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


def _subtest_types():
    """The report classes pytest and the pytest-subtests plugin use for a
    subtest; by type, never by name."""
    found = []
    for module, name in (("_pytest.subtests", "SubtestReport"), ("pytest_subtests", "SubTestReport")):
        try:
            found.append(getattr(__import__(module, fromlist=[name]), name))
        except (ImportError, AttributeError):
            continue
    return tuple(found)


def _type_name(excinfo):
    if excinfo is None:
        return None
    kind = excinfo.type
    return f"{kind.__module__}.{kind.__qualname__}"


def _class_members(kind):
    """The test methods a runner collects under a class by the default
    rule, read from the live class object: every attribute whose name
    starts with ``test`` and resolves to a callable that is not itself a
    class, inherited and dynamically attached ones included (the same set
    ``unittest.TestLoader.getTestCaseNames`` reads, without name patterns;
    the same prefix pytest's default ``python_functions`` reads). What a
    ``load_tests`` hook, a configured pattern, a filter or a collection hook
    then left out is visible as a member the run did not report passed
    (class-claim correction, 2026-10-30: root's load_tests and
    python_functions counterexamples)."""
    names = set()
    for name in dir(kind):
        if not name.startswith("test"):
            continue
        try:
            attribute = getattr(kind, name)
        except Exception:  # noqa: BLE001 -- an attribute that will not resolve is not a test
            continue
        if callable(attribute) and not isinstance(attribute, type):
            names.add(name[:300])
    return sorted(names)


class _Recorder:
    def __init__(self, path):
        self.path = path
        self.counts = dict(passed=0, failed=0, errors=0, skipped=0)
        self.failures, self.truncated = [], False
        self.collected, self.collect_errors = 0, 0
        self.subtest_types = _subtest_types()
        self.subtests = dict(passed=0, failed=0, skipped=0)
        self.pending = {}
        self.passed_cases, self.passed_truncated = [], False
        self.classes, self.classes_truncated = [], False

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_errors += 1

    def pytest_collection_finish(self, session):
        self.collected = len(session.items)
        seen = set()
        for item in session.items:
            kind = getattr(item, "cls", None)
            if not isinstance(kind, type) or kind in seen or "::" not in item.nodeid:
                continue
            seen.add(kind)
            # The class id as pytest's own node ids spell it: the item's
            # node id without its last segment (its method and parameter).
            class_id = item.nodeid.rsplit("::", 1)[0]
            if len(class_id) > 300:
                continue
            members = _class_members(kind)
            if not members or len(members) > MAX_CLASS_MEMBERS:
                continue
            if len(self.classes) >= MAX_CLASSES:
                self.classes_truncated = True
                break
            self.classes.append(dict(id=class_id, members=members))

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
        if self.subtest_types and isinstance(report, self.subtest_types):
            kind = "failed" if report.failed else "skipped" if report.skipped else "passed"
            self.subtests[kind] += 1
            if kind != "passed":
                self.pending.setdefault(report.nodeid, []).append(report)
            return
        if report.when == "call":
            held = self.pending.pop(report.nodeid, [])
            failed = [r for r in held if r.failed]
            if report.failed or failed:
                self.counts["failed"] += 1
                # pytest may already have turned a parent with failed
                # subtests into a failure with no exception of its own; that
                # outcome is its subtests', not a cause beside them.
                own = report.failed and (getattr(report, "quadratus_exc", None) is not None or not failed)
                self._fail(report, ([report] if own else []) + failed)
            elif report.skipped or held:
                # A skipped subtest is a case that did not run: its item
                # counts skipped, never passed (root review of f0b4827).
                self.counts["skipped"] += 1
            elif report.passed:
                self.counts["passed"] += 1
                if len(self.passed_cases) >= MAX_PASSED:
                    self.passed_truncated = True
                else:
                    self.passed_cases.append(report.nodeid[:300])
        elif report.failed:
            self.counts["errors"] += 1
            self._fail(report)
        elif report.skipped and report.when == "setup":
            self.counts["skipped"] += 1

    def _fail(self, report, causes=None):
        """One record per failed phase; ``causes`` are the item's own failed
        call report and its failed subtests, all of which must be body
        assertions for the record to be one."""
        if len(self.failures) >= MAX_FAILURES:
            self.truncated = True
            return
        causes = causes or [report]
        other = next((getattr(c, "quadratus_exc", None) for c in causes
                      if getattr(c, "quadratus_assertion", False) is not True), None)
        self.failures.append(dict(nodeid=report.nodeid[:300], when=report.when,
                                  exc_type=other or getattr(causes[0], "quadratus_exc", None),
                                  assertion=all(getattr(c, "quadratus_assertion", False) is True for c in causes),
                                  body=all(getattr(c, "quadratus_body", False) is True for c in causes)))

    def pytest_sessionfinish(self, session, exitstatus):
        with open(__file__, "rb") as source:
            digest = hashlib.sha256(source.read()).hexdigest()
        data = dict(producer=PRODUCER, nonce=os.environ.get("QUADRATUS_GATE_NONCE", ""),
                    module_file=os.path.realpath(__file__), module_sha256=digest,
                    exitstatus=int(exitstatus), collected=self.collected,
                    collect_errors=self.collect_errors, counts=self.counts,
                    failures=self.failures, truncated=self.truncated, subtests=self.subtests,
                    passed_cases=self.passed_cases, passed_truncated=self.passed_truncated,
                    classes=self.classes, classes_truncated=self.classes_truncated)
        with open(self.path, "w", encoding="utf-8") as out:
            json.dump(data, out)
