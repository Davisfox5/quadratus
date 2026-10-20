"""Typed scripted replies for the evidence loop's judges (test side, explicit).

A scripted auditor or reviewer answers the protocol the prompt states, as
a real one must: one audit line per requirement, one sufficiency line per
executed evidence obligation it judges, one disposition line per expected
finding id. Every judgment here is authored by the case with its own
basis and checked against what the prompt asked for; nothing is inferred
from a MET, from a receipt's status or from an obligation's presence in
the prompt. A case that tests a malformed, missing, duplicated or
contradictory reply does not use this module: it writes the raw reply.
Separate from tests.lifecycle.harness.fake_launch, which carries exactly
the response it is given.
"""
import re
from typing import Dict, List, Optional

_OBLIGATION_ROW = re.compile(
    r"^- (?P<oid>O\d+) \((?P<task>[^;]*); (?P<requirements>[^)]*)\): (?P<behavior>.*?)"
    r"(?:; evidence (?P<modality>\w+))?; runner (?P<runner>.*?); execution (?P<execution>\w+)", re.MULTILINE)
_EXPECTED_IDS = re.compile(r"Expected ids: (?P<ids>[^.\n]+)\.")
_ID = re.compile(r"^[A-Z][A-Z0-9]*-?\d+$")


def requested_obligations(prompt: str) -> List[dict]:
    """The evidence obligations the prompt lists (Session._obligations_block),
    each with its id, task, bound requirement ids, behavior, runner and
    execution state; empty when the prompt lists none."""
    rows = []
    for hit in _OBLIGATION_ROW.finditer(prompt or ""):
        rows.append(dict(oid=hit.group("oid"), task=hit.group("task"),
                         requirements=[r.strip() for r in hit.group("requirements").split(",") if r.strip()],
                         behavior=hit.group("behavior"), runner=hit.group("runner"),
                         execution=hit.group("execution"), modality=hit.group("modality") or "test"))
    return rows


def expected_finding_ids(prompt: str) -> List[str]:
    """The finding ids a recheck or review reassessment prompt expects a
    line for (Session._recheck_instructions); empty when the prompt names
    none, or names only the legacy 'the finding'."""
    hit = _EXPECTED_IDS.search(prompt or "")
    if not hit:
        return []
    ids = [part.strip() for part in hit.group("ids").split(",")]
    return [fid for fid in ids if _ID.match(fid)]


def audit_reply(prompt: str, requirements: Dict[str, str], obligations: Optional[Dict[str, str]] = None) -> str:
    """An auditor's reply: ``requirements`` maps each requirement id to its
    verdict text ('MET - <citation>' or 'NOT MET - <what is missing>');
    ``obligations`` maps an obligation id the prompt listed to the case's
    own sufficiency judgment ('SUFFICIENT <why>' or 'INSUFFICIENT <what it
    does not establish>'). An obligation the prompt did not list cannot be
    judged here (AssertionError): the case authored a judgment about
    nothing."""
    listed = {row["oid"] for row in requested_obligations(prompt)}
    unknown = sorted(set(obligations or {}) - listed)
    assert not unknown, f"the prompt lists no obligation {', '.join(unknown)}; listed: {sorted(listed) or 'none'}"
    lines = [f"{rid}: {text}" for rid, text in requirements.items()]
    lines.extend(f"{oid}: {text}" for oid, text in (obligations or {}).items())
    return "\n".join(lines)


def recheck_reply(prompt: str, dispositions: Optional[Dict[str, str]] = None, *,
                  default: Optional[str] = None) -> str:
    """A reviewer's recheck or reassessment reply: one line per expected
    finding id, '<id>: RESOLVED <how>', '<id>: UNRESOLVED <what remains>'
    or '<id>: INVALIDATED <reference>'. ``dispositions`` names ids the case
    judges itself; ``default`` is the disposition for every other expected
    id. A named id the prompt did not expect is an error; a prompt that
    expects no typed id (the legacy single-finding form) gets the default
    bare, as the legacy protocol reads it."""
    expected = expected_finding_ids(prompt)
    named = dict(dispositions or {})
    unknown = sorted(set(named) - set(expected))
    assert not unknown, f"the prompt expects no finding {', '.join(unknown)}; expected: {expected or 'none'}"
    if not expected:
        assert default is not None, "no typed id expected and no default disposition authored"
        return default
    lines = []
    for fid in expected:
        text = named.get(fid, default)
        assert text is not None, f"no disposition authored for expected finding {fid}"
        lines.append(f"{fid}: {text}")
    return "\n".join(lines)
