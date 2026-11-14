"""A run started with no terminal stops on the operator's question.

Found by the first three-vendor run of the merged engine (2026-11-12): the
planner asked a question, the run had been started in the background, and it
ended ``EOFError: EOF when reading a line`` with the question lost.
"""

import io

import pytest

from quadratus import cli
from quadratus.session import OperatorInputNeeded


def test_a_closed_terminal_is_the_operator_stop_and_keeps_the_question(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(OperatorInputNeeded) as stop:
        cli._answer("> ", "Which check proves the cases ran?")
    assert stop.value.args[0] == "Which check proves the cases ran?"


def test_an_answer_typed_at_the_terminal_is_returned_stripped(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("  the pytest check  \n"))
    assert cli._answer("> ", "Which check?") == "the pytest check"
