"""Cycle19 (PR73 6745520095, reconciled by root): a definition or an asserted
signature is a signature; an invocation or example is not.

Run 20261109T155552Z (cycle18 terminal): the planner declared
``def main(argv=None)`` and, in test guidance, said a helper runs
``main(argv)``. ``declared_signatures`` collected every backticked
``name(args)`` for a ``def``-declared name, read the call as a second
signature, refused the batch and then its one correction, and the run
stalled before the CLI or README task ran.

Now a signature is a ``def name(args)`` form (description, intended result
or acceptance) or a backticked ``name(args)`` directly after the anchored
assertion grammar (``scope._ASSERTED``). Everything else is an example and
never collected, so its call validity stays the task's behaviour to test.
Real assertions keep exact normalised comparison.
"""
import json

import pytest

from quadratus import scope as SC
from quadratus import session as S
from quadratus.scope import declared_signatures, read_scope

SCOPE = dict(permitted_paths=["cli.py", "tests/test_cli.py"],
             intended_result="`def main(argv=None):` dispatches the subcommands",
             acceptance=["cli.py defines `def main(argv=None):`", "`python -m pytest -q` passes"],
             max_lines=80)

# Root's reproduction and the two saved planner shapes (a5bbfcd08f11 and its
# correction 4a749a472288), abridged to the sentences that tripped the lint.
ROOT = "Keep `def main(argv=None):`. Tests invoke `main(argv)` with a list."
PLANNER = ("Add subcommands to `def main(argv=None)` in cli.py. Add a small helper that runs `main(argv)` and "
           "returns `(code, stdout_text, stderr_text)`.")
CORRECTION = ("Final signature: `def main(argv=None):`, quoted in intended_result and acceptance. Tests use an "
              "in-process helper that calls `main(argv)` with a list and captures stdout and stderr.")


def _declaration(body, **overrides):
    return "KIND: backend simple\nSCOPE: " + json.dumps(dict(SCOPE, **overrides)) + "\n" + body


def _refused(body, **overrides):
    with pytest.raises(ValueError, match="more than one signature") as info:
        read_scope(_declaration(body, **overrides))
    return str(info.value)


# -- invocations and examples are not signatures -------------------------------------------------

@pytest.mark.parametrize("body", [ROOT, PLANNER, CORRECTION], ids=["root", "planner", "correction"])
def test_a_definition_with_an_invocation_example_is_one_signature(body):
    scope, text = read_scope(_declaration(body))
    assert declared_signatures(text, [scope.intended_result], scope.acceptance) == {"main": ["argv=None"]}


@pytest.mark.parametrize("call", ['main(["deps", "add", "1", "2"])', "main(argv=['x'])", "main()",
                                  "main(*args)", "main(argv, env)", "main(1, 2, 3)", "main(nope=1)"])
def test_list_keyword_empty_and_non_binding_calls_are_examples(call):
    body = f"Keep `def main(argv=None):`. Tests call `{call}`."
    assert declared_signatures(body, [], []) == {"main": ["argv=None"]}
    read_scope(_declaration(body))


def test_an_intentionally_wrong_call_in_a_negative_test_is_not_a_signature_in_either_place():
    body = "Keep `def f(a, b):`. Assert calling `f(1)` raises TypeError, and `f(1, 2, 3)` too."
    acceptance = ["`def f(a, b):` is in cli.py", "calling `f(a=1, c=2)` raises TypeError"]
    assert declared_signatures(body, ["`def f(a, b):`"], acceptance) == {"f": ["a, b"]}
    read_scope(_declaration(body, intended_result="`def f(a, b):` adds", acceptance=acceptance))


@pytest.mark.parametrize("call", ["store.add(item)", "Store.add(store, item)", "cli.main(argv)"])
def test_a_qualified_invocation_is_never_collected(call):
    body = f"Add `def add(self, item):` to Store and `def main(argv=None):`. Tests call `{call}`."
    assert declared_signatures(body, [], []) == {"add": ["self, item"], "main": ["argv=None"]}


def test_a_bare_call_of_a_method_creates_no_signature_and_no_binding_rule_is_invented():
    body = "Add `def add(self, item):` to Store. Tests call `add(item)` through the store."
    assert declared_signatures(body, [], []) == {"add": ["self, item"]}


@pytest.mark.parametrize("mention", ["`main(...)`", "`main(…)`", "`main(<argv list>)`",
                                     "signature `main(<argv list>)`"])
def test_elisions_and_placeholders_state_no_signature(mention):
    body = f"Keep `def main(argv=None):`. Run {mention} from the helper."
    assert declared_signatures(body, [], []) == {"main": ["argv=None"]}


def test_incidental_calls_and_column_headings_stay_undeclared():
    body = "Keep `def main(argv=None):`. Parse `Start (s)` with `float()`; `len(row)` must match."
    assert declared_signatures(body, [], ["a missing `End (s)` header is refused"]) == {"main": ["argv=None"]}


# -- definitions and asserted signatures are still compared exactly ------------------------------

def test_two_explicit_definitions_still_conflict():
    message = _refused("Define `def main(argv=None):`. Define `def main(argv):` instead.")
    assert "main(argv=None); main(argv)" in message


def test_a_definition_in_acceptance_that_differs_is_a_conflict():
    message = _refused("Keep `def main(argv=None):`.", acceptance=["cli.py has `def main(args=None):`"])
    assert "main(args=None)" in message


@pytest.mark.parametrize("assertion", [
    "The signature is `main(args=None)`.",       # a rename that binds the same calls
    "Give the function signature `main(argv=[])`.",  # a changed default
    "cli.py defines `main(argv)`.",
    "Actually make it `main(argv, env=None)`.",
    "It is declared as `main(argv=None, *, env=None)`.",
    "Signature: `main(argv: list = None)`.",
])
def test_an_asserted_signature_is_compared_as_written(assertion):
    message = _refused("Keep `def main(argv=None):`. " + assertion)
    assert "main(argv=None)" in message


def test_the_assertion_grammar_is_anchored_to_the_quoted_form():
    for body in ("The signature stays; tests call `main(argv)`.",
                 "Define the helper so that tests call `main(argv)`.",
                 "Make it easy: `main(argv)` is called by tests.",
                 "Its signature\n`main(argv)` is an example on its own line."):
        assert declared_signatures("Keep `def main(argv=None):`. " + body, [], []) == {"main": ["argv=None"]}, body


def test_two_methods_of_one_name_are_one_name_as_before():
    """Documented limit: no class resolution, unchanged by cycle19."""
    body = "Add `def add(self, item):` to Store and `def add(self, other):` to Totals."
    assert declared_signatures(body, [], []) == {"add": ["self, item", "self, other"]}


def test_a_form_with_nested_parentheses_is_not_read_as_before():
    """Documented limit, unchanged by cycle19: the quoted-form pattern reads
    no nested parentheses, so such a form is neither a signature nor a
    contradiction."""
    assert declared_signatures("Keep `def main(argv=None):`. The signature is `main(argv=())`.", [], []) == {
        "main": ["argv=None"]}


def test_the_parameter_text_is_parsed_statically_and_bounded():
    assert SC._parameter_list("argv=None, *, env=None")
    assert not SC._parameter_list("<argv list>") and not SC._parameter_list("...")
    assert not SC._parameter_list("a" * (SC._MAX_PARAMETERS_CHARS + 1))
    assert not SC._parameter_list("__import__('os').system('true')"), "a call is not a parameter list"


# -- the message and the prompt say what a signature is -----------------------------------------

def test_the_refusal_and_the_decomposition_prompt_state_the_same_distinction():
    message = _refused("Define `def main(argv=None):`. Define `def main(argv):` instead.")
    assert "`def main(<parameters>)` form" in message and "'signature', 'defines' or 'make it'" in message
    assert "a quoted call such as a test's invocation is an example, never a signature" in message
    prompt = S._SCOPE_REQUEST
    assert 'written directly after "signature", "defines" or "make it"' in prompt
    assert "A quoted call, such as a test invoking the function or a wrong call expected to raise, is an " \
           "example, never a second signature" in prompt
    for phrase in ("signature", "defines", "make it"):
        assert SC._ASSERTED.search(f"x {phrase} "), phrase
