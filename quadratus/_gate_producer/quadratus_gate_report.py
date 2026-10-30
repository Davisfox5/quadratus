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

For each ordinary class of a collected item (``classes``) it attests the
class's default membership (``_class_members``: the supported boundary is
stated there; inherited and dynamically attached plain functions included,
a custom metaclass or descriptor held), so a whole-class claim is checked
against what the runner would collect by default rather than against the
command's syntax, a configured pattern or a static read of the file. Read
through built-in descriptors and exact type checks only. Bounded; past the
count or byte bound flagged truncated and attesting nothing, the core report
written as it always was.
"""

import hashlib
import json
import os

PRODUCER = "quadratus-pytest/7"
MAX_FAILURES = 200
MAX_PASSED = 3000
MAX_CLASSES = 1000
MAX_CLASS_MEMBERS = 1000
#: The whole serialized report, attestation included, may not pass this;
#: over it the attestation is dropped and flagged and the core report is
#: written as it would be without the field (root's review of 55e07d4).
MAX_REPORT_BYTES_WITH_CLASSES = 900_000

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


#: The built-in ``type`` descriptors, invoked directly: a class's dictionary,
#: MRO and qualified name read this way never pass through a metaclass's
#: ``__getattribute__``, ``__dir__`` or any other project code (root's review
#: of e916a0a: ``kind.__mro__`` and ``vars(base)`` ran a metaclass and turned
#: a passing test into a failure).
_TYPE_DICT = type.__dict__["__dict__"]
_TYPE_MRO = type.__dict__["__mro__"]
_TYPE_QUALNAME = type.__dict__["__qualname__"]
#: Attribute kinds whose class-level access is understood without running
#: anything: a plain function is a test method; a built-in ``staticmethod``
#: or ``classmethod`` of one is too; a ``property`` or plain data is not
#: (class access yields the object itself, which is not callable).
_PLAIN_DATA = (int, float, str, bytes, bool, type(None))


def _class_dict(kind):
    return _TYPE_DICT.__get__(kind, type)


def _class_members(kind):
    """The test methods the runner collects under a class by the default
    rule, or None when the class is outside the supported boundary (then
    nothing is attested for it; never a smaller set).

    Supported: an ordinary class, whose metaclass is exactly ``type`` (so
    every class on its MRO is ordinary too), each of whose ``test*``
    attributes along the MRO (the first definition in MRO order decides,
    as attribute lookup does) is a plain function, a built-in
    ``staticmethod``/``classmethod`` wrapping one, a ``property`` or plain
    data. That is the set ``unittest.TestLoader.getTestCaseNames`` and
    pytest's default prefix collect for such a class, inherited and
    dynamically attached plain functions included. Everything is read
    through the built-in ``type`` descriptors and exact type checks: no
    attribute access on the class, no descriptor invoked, no project code
    run. Unsupported, held as a whole: a custom metaclass; a ``test*``
    attribute of any other kind (a custom descriptor, a nested class, a
    partial, a bound or built-in callable), whose class-level resolution
    could change membership and cannot be known without running it
    (root's review of e916a0a: a descriptor that raised once ``load_tests``
    had hidden the inherited failing test was skipped as "not callable" and
    the class cleared). No general emulation of attribute lookup."""
    import types
    try:
        if type(kind) is not type:
            return None
        members, seen = [], set()
        for base in _TYPE_MRO.__get__(kind, type):
            if base is object:
                continue
            if type(base) is not type:
                return None
            for name, value in list(_class_dict(base).items()):
                if type(name) is not str or not name.startswith("test") or name in seen:
                    continue
                seen.add(name)
                kind_of = type(value)
                if kind_of is types.FunctionType:
                    members.append(name[:300])
                elif kind_of in (staticmethod, classmethod) and type(value.__func__) is types.FunctionType:
                    members.append(name[:300])
                elif kind_of is property or kind_of in _PLAIN_DATA:
                    continue
                else:
                    return None
        return sorted(members)
    except Exception:  # noqa: BLE001 -- unreadable attests nothing, never a smaller set
        return None


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
        # Optional and contained: a class whose identity or membership
        # cannot be read is not attested, past MAX_CLASSES the list is
        # flagged truncated, and any failure of the whole pass leaves an
        # empty, flagged list. Nothing here may raise into the session.
        try:
            seen = set()
            for item in session.items:
                kind = getattr(item, "cls", None)
                nodeid = getattr(item, "nodeid", None)
                if not isinstance(kind, type) or id(kind) in seen or not isinstance(nodeid, str) or "::" not in nodeid:
                    continue
                seen.add(id(kind))
                # The class id as pytest's own node ids spell it: the item's
                # node id without its last segment (its method and parameter).
                class_id = nodeid.rsplit("::", 1)[0]
                if len(class_id) > 300:
                    continue
                members = _class_members(kind)
                if not members or len(members) > MAX_CLASS_MEMBERS:
                    continue
                if len(self.classes) >= MAX_CLASSES:
                    self.classes_truncated = True
                    break
                self.classes.append(dict(id=class_id, members=members))
        except Exception:  # noqa: BLE001 -- the attestation is optional; the run is not
            self.classes, self.classes_truncated = [], True

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
        body = json.dumps(data)
        if self.classes and len(body.encode("utf-8")) > MAX_REPORT_BYTES_WITH_CLASSES:
            # The optional field may not cost the core report: dropped and
            # flagged, the rest written exactly as without it.
            data["classes"], data["classes_truncated"] = [], True
            body = json.dumps(data)
        with open(self.path, "w", encoding="utf-8") as out:
            out.write(body)
