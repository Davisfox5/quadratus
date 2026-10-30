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

For each class of a test the run holds (``classes``), it attests that
class's default membership (``_class_members``), so a whole-class claim can
be checked against what the runner would collect rather than against the
command's syntax or a static read of the file. The list is bounded; past
the bound it is flagged truncated and attests nothing.
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
    holds, or None when its file cannot be placed there or its identity
    cannot be read; never raises into the run."""
    try:
        source = inspect.getsourcefile(kind)
        relative = os.path.relpath(os.path.realpath(source), os.path.realpath(os.getcwd())) if source else None
        if not relative or relative.startswith(os.pardir) or os.path.isabs(relative):
            return None
        qualname = kind.__qualname__
        if not isinstance(qualname, str) or not qualname:
            return None
        return f"{relative.replace(os.sep, '/')}::{'::'.join(qualname.split('.'))}"
    except Exception:  # noqa: BLE001 -- an identity that cannot be read attests nothing
        return None


def _case_id(test):
    prefix = _class_id(type(test))
    if prefix is None:
        return test.id()[:300]
    return f"{prefix}::{getattr(test, '_testMethodName', '')}"[:300]


def _class_members(kind):
    """The test methods a runner collects under a class by the default
    rule, or None when the class cannot be read whole (then nothing is
    attested for it). Read statically: the names in the ``__dict__`` of
    each class on the MRO (``object`` aside) that start with ``test`` and
    resolve through ``inspect.getattr_static`` to a callable that is not
    itself a class, a ``staticmethod``/``classmethod`` unwrapped. That is
    the set ``unittest.TestLoader.getTestCaseNames`` and pytest's default
    prefix collect, inherited and dynamically attached members included,
    without calling the class's ``__dir__`` or any descriptor: optional
    reporting must not run project code or change the run it observes
    (root's review of 55e07d4: a metaclass ``__dir__`` that raised on its
    second call aborted a passing run). A metaclass that customises
    ``__dir__`` or ``__mro__`` is not modelled: any exception here is
    "cannot read", never a smaller membership. What a ``load_tests`` hook,
    a configured pattern, a filter or a collection hook then left out is
    visible as a member the run did not report passed."""
    import inspect
    try:
        names = set()
        for base in kind.__mro__:
            if base is object:
                continue
            names.update(k for k in vars(base) if isinstance(k, str) and k.startswith("test"))
        members = []
        for name in names:
            attribute = inspect.getattr_static(kind, name)
            if isinstance(attribute, (staticmethod, classmethod)):
                attribute = attribute.__func__
            if callable(attribute) and not isinstance(attribute, type):
                members.append(name[:300])
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

    def attest(self, suite):
        """One membership record per class of a test the suite holds, in
        first-seen order. Optional and contained: a class whose file cannot
        be placed under the working directory, whose identity or membership
        cannot be read, or whose membership is over the bound is not
        attested; past MAX_CLASSES the list is flagged truncated; and any
        failure of the whole pass leaves an empty, flagged list. Nothing
        here may raise into, or alter, the run it observes."""
        try:
            seen = set()
            for leaf in _leaves(suite):
                if _is_failed_import(leaf) or not isinstance(leaf, unittest.TestCase):
                    continue
                kind = type(leaf)
                if kind in seen:
                    continue
                seen.add(kind)
                class_id = _class_id(kind)
                if class_id is None or len(class_id) > 300:
                    continue
                members = _class_members(kind)
                if not members or len(members) > MAX_CLASS_MEMBERS:
                    continue
                if len(self.classes) >= MAX_CLASSES:
                    self.classes_truncated = True
                    return
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
        _RECORDER.collected = sum(1 for leaf in _leaves(test) if not _is_failed_import(leaf))
        _RECORDER.attest(test)
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
