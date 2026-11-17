"""What a child process inherits from the harness's environment.

The harness holds credentials its own process needs (a billed API key the
operator opted into, the Jev decider's key) and whatever the operator's shell
exported (GitHub and cloud tokens); ``load_dotenv`` adds every line of
``.env``. A child given all of that can read it: a vendor CLI is an agent
with a shell, and a check, preview server or probe runs project code. So
every child gets a copy with the credential-shaped names left out.

Names are removed rather than allowlisted. A strict allowlist would break
installs this cannot be tested against: proxies, CA bundles, vendor home
folders, locale, PATH managers. Only names are read, never values, so a
credential inside a value (``DATABASE_URL``, a proxy with a password, a
``GIT_CONFIG_VALUE_n`` header) is not seen. This narrows what a child is
handed; it is not a sandbox, and a file under HOME stays readable.

Two profiles, one rule (``withheld``):

- ``vendor_env(vendor)`` for a vendor CLI call. Everything the rule names is
  removed except the calling vendor's own subscription sign-in variable.
- ``check_env()`` for gate commands, preview servers, captures and readiness
  probes. Everything the rule names is removed, sign-in variables included.

``QUADRATUS_ENV_PASS=NAME,NAME`` keeps exact names for both, with one limit:
a vendor CLI is never handed a billed API key (``NEVER_FOR_A_VENDOR_CLI``) or
another vendor's sign-in, whatever the operator names. Those were removed
before this module existed, and a key a check needs must not move the model
calls onto billed transport as a side effect.

Each profile returns the new environment and the sorted names it removed.
``os.environ`` is never changed, so the harness's own reads of its keys are
unaffected.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Dict, FrozenSet, List, Mapping, Optional, Tuple

__all__ = ["CREDENTIAL_WORDS", "NEVER_FOR_A_VENDOR_CLI", "PASS_VARIABLE", "VENDOR_LOGIN", "check_env",
           "credential_shaped", "moves_transport", "query_env", "vendor_env", "withheld", "withheld_names",
           "withheld_notice"]

log = logging.getLogger(__name__)

#: The operator's escape: exact names, comma separated, kept for every child.
PASS_VARIABLE = "QUADRATUS_ENV_PASS"

#: Words that mark a name as a credential. One list for two uses: a child's
#: environment drops a name that has one as a part, and a capture profile
#: may not set a name that contains one anywhere (``preview._ENV_RESERVED``).
#: Parts here, so ``KEYBOARD_LAYOUT``, ``MONKEY``, ``XAUTHORITY`` and
#: ``MAX_THINKING_TOKENS`` are not credentials.
CREDENTIAL_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")
#: Short enough to sit inside an ordinary word: a credential only as a whole
#: part. PASS is how many tools spell a password (``DB_PASS``, ``SMTP_PASS``);
#: ``GIT_ASKPASS`` names a program and is another part.
_WHOLE_PARTS = frozenset({"KEY", "AUTH", "PASS"})
#: Long enough to also be read at the end of a part, where a name runs words
#: together (``PGPASSWORD``, ``NGROK_AUTHTOKEN``), plus the plurals that are
#: always secrets and the run-together spellings of a key. KEYS and TOKENS
#: are not here: ``MAX_THINKING_TOKENS`` is a count.
_PART_ENDINGS = tuple(word for word in CREDENTIAL_WORDS if word not in _WHOLE_PARTS) + (
    "SECRETS", "PASSWORDS", "CREDENTIALS", "PASSPHRASE", "COOKIE", "COOKIES",
    "APIKEY", "ACCESSKEY", "SECRETKEY", "PRIVATEKEY", "AUTHKEY", "AUTHORIZATION")
#: A plural that is a secret only after one of these: ``API_KEYS``,
#: ``ACCESS_TOKENS``.
_PLURALS = frozenset({"KEYS", "TOKENS"})
_PLURAL_OWNERS = frozenset({"API", "ACCESS", "PRIVATE"})
#: Tool families whose variables are credentials or point at a credential
#: store, whatever the rest of the name says.
_CREDENTIAL_PREFIXES = ("AWS_", "AZURE_", "GCP_", "GOOGLE_", "GH_", "GITHUB_", "GITLAB_", "NPM_",
                        "PYPI_", "TWINE_", "DOCKER_", "OP_", "VAULT_")
#: Agent sockets, a cluster login and sshpass's password, named whole.
_CREDENTIAL_NAMES = frozenset({"SSH_AUTH_SOCK", "GPG_AGENT_INFO", "KUBECONFIG", "SSHPASS"})
#: git's own name for the n-th config entry: KEY is a config key here.
#: Removing it while GIT_CONFIG_COUNT stays makes every git command fail.
_GIT_CONFIG_KEY = re.compile(r"GIT_CONFIG_KEY_\d+")
#: A proxy, CA bundle or TLS switch under one of the prefixes is network
#: plumbing, not a credential (``DOCKER_HTTPS_PROXY``,
#: ``npm_config_https_proxy``, ``AWS_CA_BUNDLE``): without it a child behind
#: a proxy cannot connect.
_NETWORK_SUFFIXES = ("_PROXY", "_NOPROXY", "_CA_BUNDLE", "_CAFILE", "_CA_CERTS", "_CAINFO", "_CA",
                     "_STRICT_SSL")

#: Vendor families where a base URL, host, header set or another provider's
#: settings re-route a call. Their keys and auth tokens are credential-shaped
#: already. Model names and the like stay: they pick a model, not a transport.
_TRANSPORT_PREFIXES = ("ANTHROPIC_", "OPENAI_", "XAI_", "GROK_")
_TRANSPORT_PARTS = frozenset({"URL", "URI", "BASE", "HOST", "ENDPOINT", "HEADERS",
                              "BEDROCK", "VERTEX", "FOUNDRY"})
#: A vendor that left the lineup: nothing here should be reaching it.
_TRANSPORT_FAMILIES = ("GEMINI_",)
#: Switches and keys that move a subscription call onto a billed provider. A
#: seed list, like every table of vendor names here.
_TRANSPORT_NAMES = frozenset({"CODEX_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                              "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE",
                              "AWS_BEARER_TOKEN_BEDROCK"})

#: The one variable each vendor's CLI may keep: its own subscription sign-in.
#: Keyed by ``CLISpec.vendor``. codex and grok sign in through files in their
#: home folder and keep nothing, so neither ever receives the claude token.
VENDOR_LOGIN: Dict[str, Tuple[str, ...]] = {"claude": ("CLAUDE_CODE_OAUTH_TOKEN",)}

#: Billed API keys. A vendor CLI that finds one can answer on it in place of
#: the subscription, so no vendor CLI is handed one, not even through
#: ``QUADRATUS_ENV_PASS``: an operator who passes a key to the project's
#: tests has not asked for billed model calls. The billed backend is chosen
#: in the harness's own settings, never by a child's environment.
NEVER_FOR_A_VENDOR_CLI = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                                    "XAI_API_KEY", "GROK_API_KEY", "CODEX_API_KEY"})


def _parts(upper: str) -> List[str]:
    """The name's parts, split on anything that is not a letter or digit,
    each without a trailing number (``API_KEY2`` is still a key)."""
    return [part for part in (re.sub(r"\d+$", "", raw) for raw in re.split(r"[^A-Z0-9]+", upper)) if part]


def credential_shaped(name: str) -> bool:
    """Whether a variable's name reads as a credential, in any letter case."""
    upper = name.upper()
    if upper in _CREDENTIAL_NAMES:
        return True
    if upper == PASS_VARIABLE or _GIT_CONFIG_KEY.fullmatch(upper):
        return False  # a list of names and a config key, never a secret
    parts = _parts(upper)
    if any(part in _WHOLE_PARTS or part.endswith(_PART_ENDINGS) for part in parts):
        return True
    # MYSQL_PWD is a password; PWD and OLDPWD alone are directories.
    if "PWD" in parts and len(parts) > 1:
        return True
    if any(part in _PLURALS and parts[at] in _PLURAL_OWNERS for at, part in enumerate(parts[1:])):
        return True
    return upper.startswith(_CREDENTIAL_PREFIXES) and not upper.endswith(_NETWORK_SUFFIXES)


def moves_transport(name: str) -> bool:
    """Whether a variable can move a subscription call onto billed or
    redirected transport without being a credential by name."""
    upper = name.upper()
    if upper in _TRANSPORT_NAMES or upper.startswith(_TRANSPORT_FAMILIES):
        return True
    return upper.startswith(_TRANSPORT_PREFIXES) and bool(_TRANSPORT_PARTS.intersection(_parts(upper)))


def withheld(name: str) -> bool:
    """The one rule: a name no child inherits unless a profile or the
    operator keeps it."""
    return credential_shaped(name) or moves_transport(name)


def _passed(source: Mapping[str, str]) -> FrozenSet[str]:
    return frozenset(name.strip() for name in (source.get(PASS_VARIABLE) or "").split(",") if name.strip())


def _vendor_named(name: str) -> bool:
    """A withheld name that belongs to a model vendor: its key, token, base
    URL or provider switch."""
    upper = name.upper()
    return upper in _TRANSPORT_NAMES or upper.startswith(_TRANSPORT_PREFIXES + _TRANSPORT_FAMILIES)


def _never_for(keep: Tuple[str, ...], name: str) -> bool:
    """A name the operator cannot pass to this vendor CLI: a billed API key,
    or a sign-in variable that belongs to another vendor."""
    if name.upper() in NEVER_FOR_A_VENDOR_CLI:
        return True
    return name not in keep and any(name in logins for logins in VENDOR_LOGIN.values())


#: (who, what was said, name) already logged, so a run of several hundred
#: calls says each thing once.
_announced: set = set()
_announce_lock = threading.Lock()


def _first_time(who: str, said: str, names: List[str]) -> List[str]:
    with _announce_lock:
        new = [name for name in names if (who, said, name) not in _announced]
        _announced.update((who, said, name) for name in new)
    return new


def _announce(who: str, removed: List[str], kept: List[str], refused: List[str], vendor_call: bool) -> None:
    """Names only, never values: an operator who relied on an inherited
    variable can see what stopped reaching a child, and how to keep it."""
    # A refused name has its own message: the hint below would be wrong for it.
    removed = _first_time(who, "removed", [name for name in removed if name not in refused])
    refused = _first_time(who, "refused", refused)
    kept = _first_time(who, "kept", kept)
    if removed:
        log.info("%s do not inherit %s: the names read as credentials or can re-route a vendor call. "
                 "Set %s=NAME,NAME to keep one.", who, ", ".join(removed), PASS_VARIABLE)
    if refused:
        # Part of what the operator asked for is not done, so this is never
        # left at a level the default logging hides.
        log.warning("%s do not inherit %s although %s names it: a vendor CLI is never handed a billed API "
                    "key or another vendor's sign-in. Checks, previews and probes keep it.",
                    who, ", ".join(refused), PASS_VARIABLE)
    if kept:
        # The operator asked for it, so it is kept. A vendor's own token or
        # base URL handed to its CLI is how a subscription call ends up
        # billed or redirected, and that is never left silent: said once, as
        # a warning.
        moved = vendor_call and any(_vendor_named(name) for name in kept)
        log.log(logging.WARNING if moved else logging.INFO, "%s keep %s because %s names it.%s",
                who, ", ".join(kept), PASS_VARIABLE,
                " A vendor token, base URL or provider switch kept for a vendor CLI can move its calls "
                "off the subscription." if moved else "")


def _build(who: str, keep: Tuple[str, ...], base: Optional[Mapping[str, str]],
           vendor_call: bool = False) -> Tuple[Dict[str, str], List[str]]:
    source = dict(os.environ if base is None else base)  # one reading, never the live mapping
    passed = _passed(source)
    env: Dict[str, str] = {}
    removed: List[str] = []
    kept: List[str] = []
    refused: List[str] = []
    for name, value in source.items():
        if not withheld(name) or name in keep:
            env[name] = value
        elif name in passed and not (vendor_call and _never_for(keep, name)):
            env[name] = value
            kept.append(name)
        else:
            removed.append(name)
            if name in passed:
                refused.append(name)
    removed.sort()
    _announce(who, removed, sorted(kept), sorted(refused), vendor_call)
    return env, removed


def vendor_env(vendor: str, base: Optional[Mapping[str, str]] = None) -> Tuple[Dict[str, str], List[str]]:
    """The environment for one vendor CLI call and the names left out.

    ``vendor`` is the spec's vendor key. The caller adds what it sets itself
    (the spec's ``env``, TMPDIR in a disposable copy, the fan-out switches)
    after this, so nothing it sets is ever removed."""
    return _build(f"{vendor or 'vendor'} CLI calls", VENDOR_LOGIN.get(vendor, ()), base, vendor_call=True)


def check_env(base: Optional[Mapping[str, str]] = None) -> Tuple[Dict[str, str], List[str]]:
    """The environment for a gate command, preview server, capture or
    readiness probe, and the names left out. No sign-in variable is kept:
    project code has no use for a vendor login."""
    return _build("checks, previews and probes", (), base)


def query_env(vendor: str = "", base: Optional[Mapping[str, str]] = None) -> Tuple[Dict[str, str], List[str]]:
    """The environment for a vendor binary asked for its version or its model
    listing, and the names left out. It is the vendor's own program, so the
    vendor limit holds (no billed key and no vendor's sign-in variable, even
    when ``QUADRATUS_ENV_PASS`` names one), and no sign-in is kept: a version
    or a listing is not a model call. Sign-in files under HOME still apply."""
    return _build(f"{vendor or 'vendor'} CLI version and catalogue queries", (), base, vendor_call=True)


def withheld_names(base: Optional[Mapping[str, str]] = None) -> List[str]:
    """The names in the environment that a check would not inherit, for a
    status display. Reads only; logs nothing."""
    source = dict(os.environ if base is None else base)
    passed = _passed(source)
    return sorted(name for name in source if withheld(name) and name not in passed)


def withheld_notice(base: Optional[Mapping[str, str]] = None) -> str:
    """One line for a status display or the start of a run: the names a child
    process is not given and how to keep one, or "" when nothing is withheld.
    Names only, never values. The removal itself is logged at a level the
    default logging hides, so without this line a check that fails for want
    of an inherited variable shows only the project's own error."""
    names = withheld_names(base)
    if not names:
        return ""
    return ("Environment variables not passed to checks, previews, probes or vendor CLIs (a vendor CLI "
            f"keeps only its own subscription sign-in): {', '.join(names)}. "
            f"Set {PASS_VARIABLE}=NAME,NAME to keep one.")
