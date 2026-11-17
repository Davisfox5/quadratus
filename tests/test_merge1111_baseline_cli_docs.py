"""The example environment file, held to the roster the merge left.

The development line keyed the OpenAI roster rows by family, so the override
a seat reads is QUADRATUS_ALIAS_OPENAI_ASTRA. The example still showed
QUADRATUS_ALIAS_OPENAI_GPT_6_ASTRA, which nothing reads, and the docs test
could not see it: any name that starts QUADRATUS_ALIAS_ counts as one of the
family. Each test reads the code first and the text second.
"""

import re
from pathlib import Path

from quadratus import registry
from quadratus.config import DEFAULT_CLI_MODELS
from quadratus.latest import alias_env_var

ENV_EXAMPLE = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")


def test_every_alias_override_the_example_shows_is_one_a_roster_key_reads():
    read = {alias_env_var(model.key) for model in registry.ROSTER}
    shown = set(re.findall(r"^# (QUADRATUS_ALIAS_[A-Z0-9_]+)=", ENV_EXAMPLE, re.MULTILINE))
    shown.discard("QUADRATUS_ALIAS_CACHE")
    assert shown, "the example still shows an override"
    assert shown <= read, f"overrides no roster key reads: {sorted(shown - read)}"


def test_the_example_says_the_openai_tiers_are_families():
    for family in DEFAULT_CLI_MODELS["openai"].values():
        # A row's own key, not a version key that only answers for one.
        assert registry.resolve(f"openai:{family}").key == f"openai:{family}"
        assert alias_env_var(f"openai:{family}") == f"QUADRATUS_ALIAS_OPENAI_{family.upper()}"
    text = " ".join(ENV_EXAMPLE.replace("#", "").split())
    assert "The two OpenAI values name a family, not a release" in text
    assert "set QUADRATUS_ALIAS_OPENAI_<FAMILY> above" in text
