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


_REQUIRED_KINDS = {"requirements": "requirement", "review findings carried to you": "finding",
                   "acceptance rows": "row", "executed restatements": "restatement"}
_REQUIRED_LINE = re.compile(r"^- (" + "|".join(re.escape(k) for k in _REQUIRED_KINDS) + r") \([^)]*\): (.+)$",
                            re.MULTILINE)


def required_lines(prompt: str) -> Dict[str, List[str]]:
    """The ids an audit prompt's 'Lines this reply must contain' block asks
    for, by kind (requirement, finding, row, restatement); read from the
    last such block, as a completion prompt names only what it asks."""
    block = prompt.rsplit("## Lines this reply must contain", 1)
    if len(block) < 2:
        return {}
    out: Dict[str, List[str]] = {}
    for kind, ids in _REQUIRED_LINE.findall(block[1]):
        out[_REQUIRED_KINDS[kind]] = [i.strip() for i in ids.split(",") if i.strip()]
    return out


def audit_draws(replay) -> list:
    """The audit calls that are audits, not the one same-auditor completion
    of omitted ids (cycle05), whose prompt names what it lacked."""
    return [c for c in replay.of("auditor") if "It gave no line for:" not in c.prompt]


_SCENARIO_FIELDS = re.compile(r"^  scenario: proof: (?P<proof>.*?); operation: (?P<operation>.*?); "
                              r"state: (?P<state>.*?); expect: (?P<expect>.*?); location: (?P<location>.*?)"
                              r"(?:; evidence: |; capture: |; bound cases: |$)", re.MULTILINE)
_COMPACT = re.compile(r"^scenario \S+ \(stated once below\)$")
_SUPERSEDED = re.compile(r"^superseded by task \S+; the same scenario as (?P<oid>O\d+), stated there$")


def requested_obligations(prompt: str) -> List[dict]:
    """The evidence obligations the prompt lists (Session._obligations_block),
    each with its id, task, bound requirement ids, behavior, runner and
    execution state; empty when the prompt lists none. A row the compact
    rendering states once (cycle13) gets back the generated behavior its
    scenario line spells out, and a superseded row its successor's."""
    rows = []
    for hit in _OBLIGATION_ROW.finditer(prompt or ""):
        behavior = hit.group("behavior")
        if _COMPACT.match(behavior):
            fields = _SCENARIO_FIELDS.search(prompt, hit.end())
            if fields is not None:
                behavior = (f"{fields['proof']}: {fields['operation']}; given {fields['state']}; "
                            f"expect {fields['expect']}; in {fields['location']}")[:300]
        rows.append(dict(oid=hit.group("oid"), task=hit.group("task"),
                         requirements=[r.strip() for r in hit.group("requirements").split(",") if r.strip()],
                         behavior=behavior, runner=hit.group("runner"),
                         execution=hit.group("execution"), modality=hit.group("modality") or "test"))
    by_oid = {r["oid"]: r for r in rows}
    for row in rows:
        superseded = _SUPERSEDED.match(row["behavior"])
        if superseded and superseded["oid"] in by_oid:
            row["behavior"] = by_oid[superseded["oid"]]["behavior"]
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


def judged(prompt: str, requirements: Dict[str, str], *, approve: Optional[Dict[str, str]] = None,
           reject: Optional[Dict[str, str]] = None) -> str:
    """An auditor's reply whose sufficiency lines are authored by the case
    for named evidence: ``approve`` maps the exact behavior text of an
    obligation the prompt lists executed (as the obligations block renders
    it, e.g. ``'P1: add(1, 2); given nothing stored; expect 3 is returned;
    in tests/test_app.py'`` or a NOT RUN item's text) to the case's own
    SUFFICIENT basis; ``reject`` maps one to its INSUFFICIENT gap. A named
    behavior the prompt does not list executed is an error (the case would
    be judging nothing); every other row is left exactly as the case left
    it, unjudged. Never a transform of the verdicts."""
    rows: Dict[str, list] = {}
    for row in requested_obligations(prompt):
        if row["oid"] not in {r["oid"] for r in rows.get(row["behavior"], [])}:
            rows.setdefault(row["behavior"], []).append(row)
    obligations: Dict[str, str] = {}
    for mapping, word in ((approve or {}, "SUFFICIENT"), (reject or {}, "INSUFFICIENT")):
        for behavior, text in mapping.items():
            listed = rows.get(behavior)
            assert listed, f"the prompt lists no obligation for {behavior!r}; listed: {sorted(rows) or 'none'}"
            # Every row that carries the named scenario (a repair task
            # declares the same one again) gets the case's line.
            for row in listed:
                if word == "SUFFICIENT" and row["execution"] != "ran":
                    # Nothing executed, nothing the case can call shown: the
                    # row is left as it is (the engine would refuse the line).
                    continue
                obligations[row["oid"]] = f"{word} {text}"
    return audit_reply(prompt, requirements, obligations)


def scenario_text(proof: dict) -> str:
    """A declared proof scenario as the obligations block renders its row
    (Session._register_obligation's behavior text), for ``judged``."""
    return f"{proof['id']}: {proof['operation']}; given {proof['state']}; expect {proof['expect']}; in {proof['location']}"


def judging_scenarios(base, *proofs, basis: str):
    """A named fixture judgment: the reply of ``base`` (a scripted auditor)
    plus this fixture's own SUFFICIENT line, with its authored ``basis``,
    for exactly the declared scenarios ``proofs`` (proof dicts as the
    fixture's own SCOPE declares them) that the prompt lists executed. It
    never reads a verdict, never writes INSUFFICIENT and never touches a
    row it does not name. A named row the reply already speaks about (one
    line or several: an authored judgment, conflict or restatement) is
    left exactly as written; a line about another row (a sibling reading
    judgment from ``judging``) neither blocks nor changes the named line
    (root's note on the first draft, which returned the whole reply
    unchanged whenever any O-line existed)."""
    import re as _re
    texts = [scenario_text(p) for p in proofs]

    def auditor(call, replay):
        reply = base(call, replay)
        if not isinstance(reply, str):
            return reply
        spoken = set(_re.findall(r"^\s*(O\d+)\s*:", reply, _re.MULTILINE))
        lines, seen = [], set()
        for row in requested_obligations(call.prompt):
            if (row["behavior"] in texts and row["execution"] == "ran" and row["modality"] == "test"
                    and row["oid"] not in spoken and row["oid"] not in seen):
                seen.add(row["oid"])
                lines.append(f"{row['oid']}: SUFFICIENT {basis}")
        return reply + ("\n" + "\n".join(lines) if lines else "")
    return auditor

