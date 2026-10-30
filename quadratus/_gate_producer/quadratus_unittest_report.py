"""Harness-owned unittest report producer (R21, Step 2).

Run as ``python -m <this module, under a nonce name> <the check's own
unittest arguments>`` only when a check that is ``python -m unittest ...``
declares ``--quadratus-report={report}``. The harness copies this file into a
directory it owns for that one invocation and puts only that directory on
PYTHONPATH, behind the working directory, so no project module can take its
name. It does what ``python -m unittest`` does: the same program name, the
same ``unittest.main(module=None)`` reading the same arguments, the same text
runner and output, and the same exit, which is unittest's own. The one
addition is a result class that records, per test, facts unittest already
has, and the report written once unittest has decided its exit.

It records no prose. A test counts once: failed when its body, its setUp or
tearDown, or any of its subtests failed or errored, or it succeeded
unexpectedly; otherwise skipped when it or any of its subtests was skipped,
or it was an expected failure; otherwise passed.
Its one failure record is an assertion only when every exception behind it
is exactly ``AssertionError`` and its traceback passes through the test
method's own frame. Class and module fixture errors are setup or teardown
errors; import and discovery failures are collection errors. It imports
nothing from quadratus and runs under the check's own interpreter (3.9+).

Each test counted passed is also named, as ``file::Class::method`` with the
file relative to the working directory (pytest's node id form), so a quiet
run still shows exactly which cases executed and passed; a test whose file
cannot be placed under the working directory keeps unittest's dotted id,
which names no file. A subtest is never one of them (root review of 5ca254c).

For each ordinary class of a test the run holds (``classes``), it attests
that class's default membership (``_class_members``: the supported boundary
is stated there), so a whole-class claim can be checked against what the
runner would collect rather than against the command's syntax or a static
read of the file. Everything optional is read through built-in descriptors
and exact type checks, never by attribute access on project classes; a
class outside the boundary attests nothing. The list is bounded; past the
bound, or past the report's byte bound, it is flagged truncated and attests
nothing, and the core report is written as it always was.
"""

import hashlib
import inspect
import json
import os
import sys
import unittest

PRODUCER = "quadratus-unittest/3"
MAX_FAILURES = 200
MAX_PASSED = 3000
MAX_CLASSES = 1000
MAX_CLASS_MEMBERS = 1000
#: The whole serialized report, attestation included, may not pass this;
#: over it the attestation is dropped and flagged and the core report is
#: written as it would be without the field (root's review of 55e07d4:
#: 20 classes of 1000 long-named members, within both count bounds, put
#: the report over the harness's 1 MiB read bound and erased a valid
#: 20-pass result). Below the harness's bound with room for it.
MAX_REPORT_BYTES_WITH_CLASSES = 900_000
__unittest = True  # unittest hides this module's frames from its tracebacks, as its own


def _test_code(test):
    method = getattr(test, getattr(test, "_testMethodName", ""), None)
    function = getattr(method, "__func__", method)
    try:
        function = inspect.unwrap(function)
    except Exception:  # noqa: BLE001 -- unknown stays unknown
        return None
    return getattr(function, "__code__", None)


def _from_body(test, err):
    code = _test_code(test)
    tb = err[2] if err else None
    while code is not None and tb is not None:
        if tb.tb_frame.f_code is code:
            return True
        tb = tb.tb_next
    return False


def _type_name(kind):
    return None if kind is None else f"{kind.__module__}.{kind.__qualname__}"


def _class_id(kind):
    """``file::Qualified::Class`` for a test class the working directory
    holds, or None when it cannot be placed or read; never raises into the
    run and never runs project code: the module name from the class
    dictionary and the qualified name through the built-in ``type``
    descriptors, the module from ``sys.modules`` only when it is a plain
    module object, its ``__file__`` a ``.py`` path under the working
    directory. A class this cannot place keeps unittest's dotted id."""
    import types
    try:
        module_name = _class_dict(kind).get("__module__")
        qualname = _TYPE_QUALNAME.__get__(kind, type)
        if type(module_name) is not str or type(qualname) is not str or not qualname:
            return None
        module = sys.modules.get(module_name)
        if type(module) is not types.ModuleType:
            return None
        source = module.__dict__.get("__file__")
        if type(source) is not str or not source.endswith(".py"):
            return None
        relative = os.path.relpath(os.path.realpath(source), os.path.realpath(os.getcwd()))
        if relative.startswith(os.pardir) or os.path.isabs(relative):
            return None
        return f"{relative.replace(os.sep, '/')}::{'::'.join(qualname.split('.'))}"
    except Exception:  # noqa: BLE001 -- an identity that cannot be read attests nothing
        return None


def _case_id(test):
    prefix = _class_id(type(test))
    if prefix is None:
        return test.id()[:300]
    return f"{prefix}::{getattr(test, '_testMethodName', '')}"[:300]


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


def _is_failed_import(test):
    from unittest import loader
    return isinstance(test, getattr(loader, "_FailedTest", ()))


def _leaves(test):
    if isinstance(test, unittest.TestSuite):
        for child in test:
            yield from _leaves(child)
    else:
        yield test


class _Recorder:
    def __init__(self):
        self.counts = dict(passed=0, failed=0, errors=0, skipped=0)
        self.failures, self.truncated = [], False
        self.collected, self.collect_errors, self.subtests = 0, 0, dict(passed=0, failed=0, skipped=0)
        self.ran, self.tests = False, {}
        self.passed_cases, self.passed_truncated = [], False
        self.classes, self.classes_truncated = [], False

    def survey(self, suite):
        """One pass over the suite, as the collected count always made:
        the count of unittest's own leaves (failed imports aside) and,
        beside it, the optional attestation, one membership record per
        supported class in first-seen order. Contained: a class that cannot
        be placed, read or is outside ``_class_members``' boundary is not
        attested; past MAX_CLASSES the list is flagged truncated; any
        failure of the attestation leaves an empty flagged list and never
        touches the count. Classes are told apart by identity, never by
        hashing them."""
        seen = set()
        for leaf in _leaves(suite):
            if _is_failed_import(leaf):
                continue
            self.collected += 1
            if self.classes_truncated:
                continue
            try:
                kind = type(leaf)
                if not issubclass(kind, unittest.TestCase) or id(kind) in seen:
                    continue
                seen.add(id(kind))
                class_id = _class_id(kind)
                if class_id is None or len(class_id) > 300:
                    continue
                members = _class_members(kind)
                if not members or len(members) > MAX_CLASS_MEMBERS:
                    continue
                if len(self.classes) >= MAX_CLASSES:
                    self.classes_truncated = True
                    continue
                self.classes.append(dict(id=class_id, members=members))
            except Exception:  # noqa: BLE001 -- the attestation is optional; the run is not
                self.classes, self.classes_truncated = [], True

    def _state(self, test):
        return self.tests.setdefault(id(test), dict(test=test, errors=[], unexpected=False, skipped=False))

    def _fail(self, nodeid, when, exc_type, assertion, body):
        if len(self.failures) >= MAX_FAILURES:
            self.truncated = True
            return
        self.failures.append(dict(nodeid=nodeid[:300], when=when, exc_type=exc_type,
                                  assertion=assertion, body=body))

    def error(self, test, err):
        if _is_failed_import(test):
            self.collect_errors += 1
        elif isinstance(test, unittest.TestCase):
            self._state(test)["errors"].append(err)
        else:  # a class or module fixture (unittest's _ErrorHolder)
            description = str(test)
            when = "teardown" if description.startswith("tearDown") else "setup"
            self.counts["errors"] += 1
            self._fail(description, when, _type_name(err[0]), False, False)

    def subtest(self, test, err):
        if err is None:
            self.subtests["passed"] += 1
            return
        self.subtests["failed"] += 1
        self._state(test)["errors"].append(err)

    def skip(self, test):
        if isinstance(test, getattr(unittest.case, "_SubTest", ())):
            self.subtests["skipped"] += 1
            # A skipped subcase did not run: with no failure its test is
            # skipped, never passed (root review of f0b4827).
            self._state(test.test_case)["skipped"] = True
        elif isinstance(test, unittest.TestCase):
            self._state(test)["skipped"] = True

    def stop(self, test):
        state = self.tests.pop(id(test), None)
        if state is None or _is_failed_import(test):
            return
        if state["errors"] or state["unexpected"]:
            self.counts["failed"] += 1
            causes = [(err[0], _from_body(test, err)) for err in state["errors"]]
            assertion = (not state["unexpected"] and all(k is AssertionError and body for k, body in causes))
            other = next((k for k, _ in causes if k is not AssertionError), None)
            first = causes[0][0] if causes else None
            self._fail(test.id(), "call", _type_name(other or first), assertion,
                       bool(causes) and all(body for _, body in causes))
        elif state["skipped"]:
            self.counts["skipped"] += 1
        else:
            self.counts["passed"] += 1
            if len(self.passed_cases) >= MAX_PASSED:
                self.passed_truncated = True
            else:
                self.passed_cases.append(_case_id(test))

    def write(self, path, exitstatus):
        with open(__file__, "rb") as source:
            digest = hashlib.sha256(source.read()).hexdigest()
        data = dict(producer=PRODUCER, nonce=os.environ.get("QUADRATUS_GATE_NONCE", ""),
                    module_file=os.path.realpath(__file__), module_sha256=digest, exitstatus=exitstatus,
                    collected=self.collected, collect_errors=self.collect_errors, counts=self.counts,
                    failures=self.failures, truncated=self.truncated, subtests=self.subtests,
                    passed_cases=self.passed_cases, passed_truncated=self.passed_truncated,
                    classes=self.classes, classes_truncated=self.classes_truncated)
        body = json.dumps(data)
        if self.classes and len(body.encode("utf-8")) > MAX_REPORT_BYTES_WITH_CLASSES:
            # The optional field may not cost the core report: dropped and
            # flagged, the rest written exactly as without it.
            data["classes"], data["classes_truncated"] = [], True
            body = json.dumps(data)
        with open(path, "w", encoding="utf-8") as out:
            out.write(body)


_RECORDER = _Recorder()


class _Result(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        _RECORDER._state(test)

    def stopTest(self, test):
        super().stopTest(test)
        _RECORDER.stop(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        _RECORDER.error(test, err)

    def addError(self, test, err):
        super().addError(test, err)
        _RECORDER.error(test, err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        _RECORDER.skip(test)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        _RECORDER._state(test)["skipped"] = True

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        _RECORDER._state(test)["unexpected"] = True

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        _RECORDER.subtest(test, err)


class _Runner(unittest.TextTestRunner):
    resultclass = _Result

    def run(self, test):
        _RECORDER.survey(test)
        _RECORDER.ran = True
        return super().run(test)


def main():
    path = os.environ.get("QUADRATUS_GATE_REPORT")
    sys.argv[0] = "python -m unittest"
    try:
        unittest.main(module=None, testRunner=_Runner)
    except SystemExit as exc:
        code = exc.code
        if path and _RECORDER.ran and (code is None or isinstance(code, (bool, int))):
            # Only the exit unittest itself chose; a usage error ran nothing
            # and leaves no report, which the harness reads as missing.
            _RECORDER.write(path, int(code or 0))
        raise


if __name__ == "__main__":
    main()
