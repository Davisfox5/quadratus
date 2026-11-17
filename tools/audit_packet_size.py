"""Compare the audit packet's size under the old and the compact renderers,
offline, from a saved run (cycle13).

Read-only and call-free: it reads the run's committed checkpoint (and,
optionally, a saved prompt text you name), restores the carried state onto
a bare Session that has no project, no gate and no provider, renders the
sections the compaction changed twice on that one state (``old``: the
renderer before cycle13; ``new``: the compact one), and writes one JSON
report to a path outside the run folder. It never resumes the run, runs a
check, calls a model, reads the current checkout's fingerprint as the
historical source, or writes inside the run folder.

What it can and cannot say:

- ``old`` against ``new`` is like for like: the same restored state, the
  same audited ids, the same saved source identity.
- It is not a reconstruction of the prompt the auditor saw. A terminal
  checkpoint can be later than the last audit (the audit's own judgments,
  a later task, a different source): the report lists every mismatch it can
  detect under ``state_match`` and never calls unlike states an exact
  before/after.
- Sizes are characters. No token count is derived from them.

Usage: python tools/audit_packet_size.py RUN_DIR --out REPORT.json
       [--before-prompt SAVED_AUDIT_PROMPT.txt]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: The headings the compared sections open with.
_HEADINGS = {
    "obligations": "## Evidence obligations",
    "execution_debt": "## Execution debt",
    "review_observations": "## Earlier task-review observations",
    "audit_history": "## Earlier audits of these requirements",
}


def _refuse(message: str) -> None:
    raise SystemExit(f"audit_packet_size: {message}")


def _last_audit(audits) -> dict:
    for audit in reversed(audits or []):
        if audit.get("verdicts") and not audit.get("failed"):
            return audit
    return {}


def _session(state: dict, source):
    """A bare Session carrying ``state``: no project, no gate, no provider
    (any call raises), its store in a private temporary folder."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.checkpoint import restore_state
    from quadratus.session import Session, SessionConfig

    def no_call(*_a, **_k):
        raise RuntimeError("audit_packet_size makes no model call")
    store_dir = Path(tempfile.mkdtemp(prefix="audit-packet-size-"))
    # An empty private folder stands in for the project, so the renderers
    # read currency labels at all; nothing is ever read from it, and the
    # source identity is the saved one below, never a fingerprint of it.
    (store_dir / "project").mkdir()
    session = Session("(offline packet measurement)", ArtifactStore(store_dir / "artifacts"), no_call,
                      config=SessionConfig(project=store_dir / "project", requirements_ledger=True))
    restore_state(session, state)
    # The saved source identity, never the checkout's: every currency label
    # reads against the source the audit judged.
    session._source_fingerprint = lambda: source
    return session


def _sections(session, audited, *, compact: bool) -> dict:
    session._compact_audit_packet = compact
    obligations = session._obligations_block(list(session.evidence_obligations))
    split = obligations.split("\n\n## Evidence obligations", 1)
    debt = split[0] if len(split) == 2 else (obligations if "## Execution debt" in obligations else "")
    acceptance = ("\n\n## Evidence obligations" + split[1]) if len(split) == 2 else (
        "" if "## Execution debt" in obligations else obligations)
    return dict(obligations=acceptance, execution_debt=debt,
                review_observations=session._review_notes_block(dict.fromkeys(audited)),
                audit_history=session._audit_history(list(audited), judge=True))


def _prompt_sections(text: str) -> dict:
    """Section sizes of a saved prompt: each compared heading to the next
    level-two heading, or to the end."""
    out = {}
    for name, heading in _HEADINGS.items():
        start = text.find(heading)
        if start < 0:
            continue
        following = text.find("\n\n## ", start + len(heading))
        out[name] = (following if following >= 0 else len(text)) - start
    return out


def measure(run_dir: Path, out: Path, before_prompt: Path | None = None) -> dict:
    from quadratus import checkpoint
    run_dir, out = run_dir.resolve(), out.resolve()
    if out == run_dir or run_dir in out.parents:
        _refuse("the report must be written outside the run folder")
    body = checkpoint.load(run_dir)
    state = body.get("state") or {}
    if not state:
        _refuse("the checkpoint carries no state (it was written not resumable); nothing to render")
    audits = list(state.get("requirement_audits") or [])
    last = _last_audit(audits)
    if not last:
        _refuse("the checkpoint holds no completed audit to compare against")
    audited = list(last.get("verdicts") or {})
    audit_source = last.get("source")
    checkpoint_source = (body.get("identities") or {}).get("source")
    mismatches = []
    if checkpoint_source != audit_source:
        mismatches.append("the checkpoint's source identity differs from the source the last audit judged")
    later = [a for a in audits[audits.index(last) + 1:] if a.get("result") or a.get("failed")]
    if later:
        mismatches.append(f"{len(later)} audit record(s) after the last completed one (void or failed)")
    mismatches.append("the restored state is the checkpoint's, after the last audit: it holds that audit's own "
                      "judgments and its record, so neither renderer reproduces the prompt that audit was shown")
    session = _session(state, audit_source)
    # The history that audit was shown held only the audits before it.
    session.requirement_audits = audits[:audits.index(last)]
    old = _sections(session, audited, compact=False)
    new = _sections(session, audited, compact=True)
    required = [i for i in last.get("required") or [] if re.fullmatch(r"O\d+", str(i))]

    def present(text: str, oid: str) -> bool:
        return f"- {oid} (" in text
    report = dict(
        run=run_dir.name, comparison="old and new renderers on one restored state (like for like)",
        historical_prompt_reconstructed=False, state_match=dict(exact=False, mismatches=mismatches),
        sources=dict(last_audit=audit_source, checkpoint=checkpoint_source),
        audited=audited, audits_before_last=audits.index(last),
        sections={name: dict(old_chars=len(old[name]), new_chars=len(new[name])) for name in old},
        total=dict(old_chars=sum(len(v) for v in old.values()), new_chars=sum(len(v) for v in new.values())),
        required_obligation_ids=dict(
            checked=len(required),
            missing_old=[i for i in required if not any(present(t, i) for t in old.values())],
            missing_new=[i for i in required if not any(present(t, i) for t in new.values())]),
        units="characters; no token count is derived")
    if before_prompt is not None:
        text = Path(before_prompt).read_text(encoding="utf-8", errors="replace")
        report["before_prompt"] = dict(path=str(before_prompt), chars=len(text), sections=_prompt_sections(text),
                                       note="a saved prompt, compared as written; the state it was built from may "
                                            "differ from the restored one (see state_match)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--before-prompt", type=Path)
    args = parser.parse_args(argv)
    report = measure(args.run_dir, args.out, args.before_prompt)
    print(json.dumps(dict(total=report["total"], required=report["required_obligation_ids"],
                          exact=report["state_match"]["exact"]), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
