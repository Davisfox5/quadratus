"""Pre-rename environment variables keep working, loudly."""

from quadratus.config import _WARNED_LEGACY_ENV, env_with_legacy


def test_the_current_name_wins(monkeypatch):
    monkeypatch.setenv("QUADRATUS_THING", "new")
    monkeypatch.setenv("MULTI_LLM_THING", "old")
    assert env_with_legacy("QUADRATUS_THING", "MULTI_LLM_THING") == "new"


def test_the_legacy_name_is_honoured_when_the_current_one_is_unset(monkeypatch, capsys):
    _WARNED_LEGACY_ENV.discard("MULTI_LLM_THING")
    monkeypatch.delenv("QUADRATUS_THING", raising=False)
    monkeypatch.setenv("MULTI_LLM_THING", "old")
    assert env_with_legacy("QUADRATUS_THING", "MULTI_LLM_THING") == "old"
    assert "MULTI_LLM_THING is deprecated" in capsys.readouterr().err


def test_the_deprecation_notice_is_printed_once(monkeypatch, capsys):
    _WARNED_LEGACY_ENV.discard("MULTI_LLM_THING")
    monkeypatch.delenv("QUADRATUS_THING", raising=False)
    monkeypatch.setenv("MULTI_LLM_THING", "old")
    env_with_legacy("QUADRATUS_THING", "MULTI_LLM_THING")
    capsys.readouterr()
    env_with_legacy("QUADRATUS_THING", "MULTI_LLM_THING")
    assert capsys.readouterr().err == ""


def test_the_default_is_used_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("QUADRATUS_THING", raising=False)
    monkeypatch.delenv("MULTI_LLM_THING", raising=False)
    assert env_with_legacy("QUADRATUS_THING", "MULTI_LLM_THING", "fallback") == "fallback"


def test_an_empty_legacy_value_is_not_mistaken_for_unset(monkeypatch):
    _WARNED_LEGACY_ENV.discard("MULTI_LLM_THING")
    monkeypatch.delenv("QUADRATUS_THING", raising=False)
    monkeypatch.setenv("MULTI_LLM_THING", "")
    assert env_with_legacy("QUADRATUS_THING", "MULTI_LLM_THING", "fallback") == ""
