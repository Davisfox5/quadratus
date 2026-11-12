"""Delivery and evidence obligations of the session engine, as a mixin of
Session: NOT RUN reports and what they are bound to, inspections, PROVES
declarations and the cases a scope declares, recorded runs, restated
rows, DISCHARGES, and the obligations a close or DONE must find settled.

The methods moved out of session.py unchanged on 2026-11-12, when the split
of 2026-11-11 was redone after the development line was merged. They reach
the rest of Session only through ``self``; a method that reads a name a
test replaces on the session module stayed there. This module never
imports quadratus.session.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple

from . import delivery, judgment, obligations
from .check_facts import (
    _CLASS_CITATION,
    _CLASS_CLAIM,
    _class_declaration,
    _command_interpreter,
    _item_paths,
    _resolved_interpreter,
    _same_python_command,
    _unittest_module_command,
)
from .reply_parsers import (
    _CASE_CITATION,
    _CASE_NAME,
    _MAX_SCOPE_LINES,
    _MAX_SCOPE_PATH_CHARS,
    _MAX_SCOPE_PATHS,
    _OUTSIDE_SCOPE,
    _PROVES_ANY,
    _PROVES_BODY,
    _PROVES_LOOSE,
    _PROVES_PID,
    _RUNS_ANY,
    _RUNS_BODY,
    _SCENARIO_RANGE,
    _SOURCE_FILE,
    _UNRUNNABLE_CLAIM,
    _behavioural_error,
    _cited_cases,
    _inspection_attested,
    _proves_problem,
    _pure_reading,
)
from .scope import _matches as _scope_matches
from .scope import is_test_path
from .session_prompts import _PLAIN_VERDICT_RULE, _display_item, _replaced_note
from .session_types import _EDITING_ROLES
from .verdicts import inspection_lines as _inspection_lines

# One logger name for the engine, wherever its code lives.
log = logging.getLogger("quadratus.session")


class ObligationsMixin:
    """What an editing call owes for what it reported, and when that is settled.

    Session inherits this; nothing here is used on its own.
    """

    def _superseding_rows(self, rows: List[dict]) -> Dict[str, dict]:
        """For each superseded acceptance row, the one row that now holds the
        same proof, when it can be named exactly (cycle13): ``superseded_by``
        is a task id, so the successor is the single acceptance row of that
        task with the same scenario (proof id aside), the same requirement
        revisions and the same modality, followed while it is itself
        superseded, bounded by the number of rows and never revisiting a
        row. No successor, two of them, a cycle, or a successor whose
        scenario is not shown keeps the full row; nothing is mapped by
        guess."""
        def norm(o: dict):
            # The identity ``_supersede_obligation`` matched on: every
            # scenario field but the proof id, the requirement revisions and
            # the modality.
            scenario = dict(o.get("scenario") or {})
            return (sorted((k, json.dumps(v, sort_keys=True, default=str)) for k, v in scenario.items() if k != "proof"),
                    [list(r) for r in o.get("requirements") or ()], o.get("modality") or "test")
        out: Dict[str, dict] = {}
        for o in rows:
            if not o.get("superseded_by") or not o.get("scenario"):
                continue
            # Only a row whose behaviour is exactly its generated summary and
            # whose own scenario (its own proof and bound cases) is shown may
            # refer to a successor: a distinct claim or an over-bound scenario
            # keeps the full row (root review of 10a9433).
            if not (self._behavior_is_generated(o) and self._scenario_shown(o)):
                continue
            current, seen = o, {id(o)}
            for _ in range(len(rows)):
                candidates = [r for r in rows if r is not current and r.get("task") == current.get("superseded_by")
                              and r.get("kind", "acceptance") == "acceptance" and norm(r) == norm(o)]
                if len(candidates) != 1 or id(candidates[0]) in seen:
                    current = None
                    break
                current = candidates[0]
                seen.add(id(current))
                if not current.get("superseded_by"):
                    break
            else:
                current = None
            if current is not None and not current.get("superseded_by") and self._scenario_shown(current):
                out[o["oid"]] = current
        return out

    def _superseded_row_line(self, o: dict, successor: dict, modality: str) -> str:
        """A superseded row named against the row that holds its scenario
        now: everything that is its own (task, requirements, its proof id, modality,
        runner, execution, the reason it had before it was superseded,
        sufficiency and its judge, bound cases that differ) stays on the
        line; only the shared scenario text is referred to."""
        runner = (o.get("runner") or "NONE (prerequisite missing)") if modality == "test" else "none"
        before = (o.get("pre_supersession") or {}).get("reason")
        cases = list(o.get("cases") or ())
        own, theirs = self._scenario_field(o, "proof"), self._scenario_field(successor, "proof")
        return (f"- {o['oid']} ({o['task']}; {', '.join(r[0] for r in o['requirements'])}): superseded by task "
                f"{o['superseded_by']}; its proof {own} is the same scenario as {successor['oid']}"
                + (f" (proof {theirs})" if theirs and theirs != own else "")
                + f", stated there; evidence {modality}; "
                f"runner {runner}; execution {o['execution']}"
                + (f" (before it was superseded: {before})" if before else "")
                + f"; sufficiency {o['sufficiency']}"
                + (f" by {o['judged_by']}" if o.get("judged_by") else "")
                + (f"; its own bound cases: {', '.join(cases)}" if cases and cases != list(successor.get("cases") or ())
                   else ""))

    def _obligations_block(self, rows: List[dict]) -> str:
        if not rows:
            return ""
        # A restated item already executed is shown, with the line that judges
        # it, under its own heading (``_not_run_block``), never here as a row
        # to judge nothing about (cycle05).
        awaiting = set(self._restated_executed_ids())
        debt = [o for o in rows if delivery.purpose_of(o) != "acceptance" and o.get("oid") not in awaiting]
        rows = [o for o in rows if delivery.purpose_of(o) == "acceptance"]
        if not debt and not rows:
            return ""
        lines = []
        if debt:
            # Execution debt is shown apart from acceptance evidence and
            # never put to a seat for sufficiency (quality/completion batch,
            # 2026-10-31): a NOT RUN report or a skip fact asked whether
            # the named cases ran, the harness's own execution answers it,
            # and nothing about the requirements it holds follows either way.
            lines.append("\n\n## Execution debt (harness records: a lead's NOT RUN report or a check's skipped "
                         "cases, settled only by the harness's own execution of exactly the named cases on the "
                         "current source, gate and environment; never evidence for the requirements it holds; "
                         "judge nothing about these rows)")
            for o in debt:
                unknown = delivery.purpose_of(o) == "unknown"
                lines.append(f"- {o['oid']} ({o['task']}; {', '.join(r[0] for r in o['requirements'])}): "
                             + ("UNKNOWN PURPOSE (mixed record; held, never credited) " if unknown else "")
                             + f"{o['behavior']}; evidence {o.get('modality') or 'test'}; "
                             f"runner {o['runner'] or 'NONE (prerequisite missing)'}; "
                             f"execution {o['execution']}"
                             + (f" ({o['reason']})" if o.get("reason") else "")
                             + ("; held" if unknown else
                                "; settled" if o.get("execution") == "ran" else "; unsettled: holds its requirements"))
        if not rows:
            return "\n".join(lines)
        lines.append("\n\n## Evidence obligations (harness records: execution is what ran; sufficiency is "
                     "your judgment, never the harness's)")
        compact = getattr(self, "_compact_audit_packet", True)
        successors = self._superseding_rows(rows) if compact else {}
        for o in rows:
            modality = o.get("modality") or "test"
            successor = successors.get(o.get("oid"))
            if successor is not None:
                lines.append(self._superseded_row_line(o, successor, modality))
                continue
            runner = (o['runner'] or 'NONE (prerequisite missing)') if modality == "test" else (
                "none (reading claim: no runnable proof)" if modality == "reading" else
                "none (capture claim: measured by the harness's verified render, judged by the design reviewer)"
                if modality == "capture" else
                "none (legacy acceptance: no scenario declared, no case bound, mapping unknown)")
            # The scenario is stated once: a behaviour that is exactly the
            # summary generated from the row's own scenario is not repeated
            # beside it (cycle13); any other behaviour is a claim of its own
            # and is shown whole, and so is every behaviour whose scenario
            # is over the render bound and not shown.
            behavior = (f"scenario {self._scenario_field(o, 'proof')} (stated once below)"
                        if compact and self._behavior_is_generated(o) and self._scenario_shown(o) else o["behavior"])
            lines.append(f"- {o['oid']} ({o['task']}; {', '.join(r[0] for r in o['requirements'])}): "
                         f"{behavior}; evidence {modality}; runner {runner}; "
                         f"execution {o['execution']}"
                         + (f" ({o['reason']})" if o.get("reason") else "")
                         + f"; sufficiency {o['sufficiency']}"
                         + (f" by {o['judged_by']}" if o.get("judged_by") else "")
                         + self._scenario_line(o))
        if any(o.get("modality", "test") == "test" and o.get("execution") == "ran" for o in rows):
            lines.append("An executed test row is judged only by your own line, '<O-id>: SUFFICIENT - <what the "
                         "executed case shows for that requirement and scenario>' or '<O-id>: INSUFFICIENT <what "
                         "it does not establish>'; your MET or NOT MET on a requirement it binds does not judge "
                         "it, and unjudged it keeps that requirement not met.")
        if any(o.get("modality") == "legacy" for o in rows):
            lines.append("A legacy acceptance row is judged only by your own line, '<O-id>: SUFFICIENT - <the "
                         "project evidence you read that shows it>' or '<O-id>: INSUFFICIENT <what it does not "
                         "establish>'; your MET on its requirement does not judge it, and unjudged it keeps that "
                         "requirement not met.")
        if any(o.get("modality") == "capture" for o in rows):
            lines.append("A capture row (evidence capture, a visual scenario) is measured by the harness's verified "
                         "render and judged sufficient only by the design reviewer's own explicit line; you may "
                         "add '<O-id>: INSUFFICIENT <what the render does not show>', and your MET on its "
                         "requirement does not judge it.")
        if any(o.get("modality") == "reading" for o in rows):
            lines.append("A reading row (evidence reading, a documentation scenario) is judged only by your own "
                         "line, '<O-id>: SUFFICIENT - <why reading can demonstrate this exact requirement and "
                         "scenario, and what the cited file shows>' or '<O-id>: INSUFFICIENT <what reading cannot "
                         "establish, or what the file does not show>'. A SUFFICIENT line must cite the file the "
                         "scenario names; one that does not is not counted. A requirement about runtime "
                         "behaviour is not demonstrated by a document that describes it: say INSUFFICIENT. Your "
                         "MET on its requirement does not judge the row, and unjudged it keeps that requirement "
                         "not met.")
        return "\n".join(lines)

    def _inherited_not_run(self, entry: dict) -> Optional[dict]:
        """The standing NOT RUN item of an earlier task that ``entry`` is the
        same report of, or None (cycle10; root review of f7fc186, PR73
        6723584415: equal path sets inherited a different scenario about the
        same test file, and a repeat after the application under test
        changed). Every one of these must hold, and anything unknown keeps
        the report as this task's own debt:

        - the whole item is the same text (whitespace and case aside), and
          its binding (kind, scenario, cases, problem) is the same, with no
          scenario of this task in it;
        - it names at least one path, and the reporting seat names no
          requirement for it (``| for: R<n>`` is the caller's to check);
        - it was reported under the same gate and environment;
        - the project's source fingerprint, now, equals the one recorded
          with the earlier report, both known, so nothing the scenario
          exercises has changed since (application code included); and
        - this task's measured changes, where the harness has them, are
          empty.

        Such a repeat is linked to its owner and adds no obligation; a
        report that differs in any of these is a new report."""
        binding = entry.get("binding") or {}
        if binding.get("scenario") or not _item_paths(str(entry.get("item") or "")):
            return None
        source = entry.get("source")
        if not source:
            return None
        if self._task_changed_files(self._active_spec):
            return None
        text = " ".join(str(entry.get("item") or "").lower().split())

        def same_binding(other: dict) -> bool:
            keys = ("kind", "scenario", "cases", "problem")
            return all((other.get("binding") or {}).get(k) == binding.get(k) for k in keys)
        for prior in self._standing_not_run():
            if (prior.get("task") == entry.get("task") or prior.get("kind") == "skipped" or not prior.get("oid")
                    or prior.get("association_claimed")):
                continue
            if (" ".join(str(prior.get("item") or "").lower().split()) == text and same_binding(prior)
                    and prior.get("gate") == entry.get("gate") and prior.get("environment") == entry.get("environment")
                    and prior.get("source") == source):
                return prior
        return None

    def _associate_not_run(self, entry: dict, declared=()) -> None:
        """Which of the task's covered requirements a NOT RUN item holds
        (Davis, 2026-11-06, turn-tracking-01 O34: an unexecuted interpreter
        fallback in a test helper held every requirement its task covered,
        a documentation one among them). The hold narrows only when the
        whole item is accounted for by the task's own declared scenarios:
        every scenario it names, every case it binds and, for an item bound
        by path operands, every path it names is a declared scenario, a case
        of one, or a scenario's location; the hold is then the union of
        their requirements within the task's covers. One unmatched part, or
        nothing bound, keeps every covered requirement held, and the record
        names the unmatched part (root review of a3de6d6, item 5). The
        reporting seat's own ``| for: R<n>`` is kept as a claim and never
        narrows the hold: an editor's word is not acceptance authority
        (same review, item 4). The item, its reason and its execution are
        never changed or dropped; ``requirements_covered`` keeps what the
        task covered."""
        covers = [r for r in (entry.get("requirements") or []) if r]
        entry["requirements_covered"] = list(covers)
        rows = [r for r in self.evidence_obligations
                if r.get("task") == entry.get("task") and r.get("kind") == "acceptance" and not r.get("superseded_by")]

        def held_by(match) -> set:
            return {str(ref[0]) for r in rows if match(r) for ref in (r.get("requirements") or ())}
        binding = entry.get("binding") or {}
        pids = [p.strip() for p in str(binding.get("scenario") or "").split(",") if p.strip()]
        cases = list(dict.fromkeys(str(c) for c in (entry.get("cases") or ())))
        # Every path the item names is accounted for, whatever grammar bound
        # the rest: a path that is the file of a bound case is that case, and
        # any other path must be a declared scenario's location (root review
        # of 508a9a8: "P1 and other.py" narrowed to P1's requirement).
        case_files = {c.split("::")[0] for c in cases}
        paths = [p for p in dict.fromkeys(_item_paths(str(entry.get("item") or ""))) if p not in case_files]
        found, unmatched, parts = set(), [], []
        named = ([(pid, lambda r, pid=pid: r.get("proof_id") == pid) for pid in pids]
                 + [(case, lambda r, case=case: case in (r.get("cases") or ())) for case in cases]
                 + [(path, lambda r, path=path: str(r.get("location") or "") == path) for path in paths])
        for part, match in named:
            reqs = held_by(match)
            if reqs:
                found.update(reqs)
                parts.append(part)
            else:
                unmatched.append(part)
        found &= set(covers)
        claimed = list(dict.fromkeys(declared))
        if claimed:
            entry["association_claimed"] = claimed
        claim = (f"; {entry.get('author')} claims it serves {', '.join(claimed)} (recorded, never narrows the hold)"
                 if claimed else "")
        # An item the binder could not bind at all never narrows on a path
        # it happens to mention: prose around it is not accounted for.
        if binding.get("kind") in (None, "none") and not unmatched:
            unmatched = ["the item is bound to no scenario, case or path operand"] if parts else []
        if parts and not unmatched and found:
            entry["requirements"] = [r for r in covers if r in found]
            entry["association"] = "bound: the whole item matches declared scenarios (" + ", ".join(parts)[:200] + ")" + claim
        elif unmatched:
            entry["association"] = ("unknown: part of the item matches no declared scenario, case or location of this "
                                    "task (" + ", ".join(unmatched)[:200] + "); every covered requirement stays held "
                                    "until it runs or the whole item is bound to declared scenarios" + claim)
        else:
            entry["association"] = ("unknown: the item names no declared scenario, case or location of this task; "
                                    "every covered requirement stays held until it runs or the whole item is bound "
                                    "to declared scenarios" + claim)
        if entry["requirements"] != covers:
            self._note(f"task {entry.get('task')}: NOT RUN item held against {', '.join(entry['requirements'])} "
                       f"({entry['association'][:160]}): {str(entry.get('item'))[:80]}")

    def _reading_only(self, entry: dict) -> bool:
        """A NOT RUN item that only asks to read existing project files."""
        paths = _pure_reading(str(entry.get("item") or ""))
        if not paths or (entry.get("binding") or {}).get("kind") != "paths" or not self.project:
            return False
        root = Path(self.project)
        return all((root / p).is_file() for p in paths)

    def _file_digests(self, files) -> Dict[str, Optional[str]]:
        import hashlib
        out: Dict[str, Optional[str]] = {}
        for rel in files:
            try:
                out[rel] = hashlib.sha256((Path(self.project) / rel).read_bytes()).hexdigest()
            except OSError:
                out[rel] = None
        return out

    def _attest_inspections(self, task: str, peer: str, stage: str, reply: str) -> None:
        """A completed review or recheck attests the inspection gaps its own
        prompt handed it, and only on its explicit judgment (root review of
        a3de6d6, items 2 and 3). Each hand-off (``_inspection_note``) is
        bound to this task, seat and stage and to the files' bytes and the
        source as they were dispatched; the reply must carry 'READ <id> -
        <what they show>' for the gap and no 'UNREAD <id>' line. The
        attestation records the dispatched digests, never the bytes after
        the reply, so a file changed in between is not attested as read and
        reopens the gap. The hand-off is consumed either way; a missing,
        empty or refused judgment leaves the gap open, recorded."""
        # Read by verdicts.py: a READ counts only as 'READ <id> - <what the
        # files show>', so "READ <id> not yet" attests nothing.
        verdicts: Dict[str, List[tuple]] = {}
        for kind, gid, text in _inspection_lines(reply or ""):
            verdicts.setdefault(gid, []).append((kind, text))
        for gap in self.inspection_gaps:
            handed = next((h for h in reversed(gap.get("handed") or [])
                           if h.get("task") == task and h.get("peer") == peer and h.get("stage") == stage
                           and not h.get("consumed")), None)
            if handed is None:
                continue
            handed["consumed"] = True
            said = verdicts.get(str(gap.get("id")).upper(), [])
            reads = [text for kind, text in said if kind == "READ" and text]
            problem = ("the reviewer said it could not read them" if any(kind == "UNREAD" for kind, _ in said)
                       else "no 'READ <id> - <what they show>' line" if not reads or any(
                           not kind for kind, _ in said)
                       else "a file was unreadable when it was handed" if any(
                           d is None for d in (handed.get("files") or {}).values()) else "")
            if problem:
                gap.setdefault("refusals", []).append(dict(by=peer, stage=stage, task=task, why=problem))
                self._note(f"task {task}: {peer}'s {stage} does not attest the reading of "
                           f"{', '.join(gap['files'])}: {problem}")
                continue
            gap["attested"] = dict(by=peer, stage=stage, task=task, files=dict(handed["files"]),
                                   source=handed.get("source"), judgment=reads[0][:300], handed_at=handed.get("at"))
            self._note(f"task {task}: {peer}'s completed {stage} attests the reading of {', '.join(gap['files'])}")

    def _open_inspections(self) -> List[dict]:
        """Inspection gaps without a valid attestation on the files as they
        are now. Valid is the same rule ``tools/truth_check.py`` applies to
        the saved record, so a resumed or carried record is read the same
        way live and saved (root review of 508a9a8)."""
        return [g for g in self.inspection_gaps
                if not _inspection_attested(g) or self._file_digests(g["files"]) != g["attested"].get("files")]

    def _bind_not_run(self, entry: dict) -> None:
        """Bind a NOT RUN item to what it names, as the harness can identify
        it, reading the whole item in every grammar at once: declared
        scenarios of the reporting task (``P<n>``, through the cases their
        acceptance rows bind), explicit ``file::[Class::]test[param]`` ids
        checked against their file, and ``Class.test``, ``Class::test`` or
        bare ``test_*`` names defined in exactly one test file. The item is
        discharged only by all of them: a claim that names P1 and a case, or
        two scenarios, binds the union, and one unresolvable part binds
        nothing (root's controls on bd0164f: mixed claims cleared on one
        grammar's identities). A parametrized id keeps its parameter. An
        item naming no scenario or case binds by path operands (the older
        rule) or by nothing. The binding is on the entry as ``binding``
        (kind, scenario, cases, problem) and its case ids as ``cases``; the
        item and its reason stay as written. An identity claim, valid or
        not, never falls back to a path."""
        item = str(entry.get("item") or "")
        binding = dict(kind="none", scenario=None, cases=[], problem="")
        command = _unittest_module_command(item)
        if command is not None:
            # A documented ``python -m unittest <module>`` command (completion
            # workflow, 2026-10-30; evidence-authority-02 t5: the item named a
            # module, not a file, and bound to nothing while the file's cases
            # ran under other rows). It binds to every case the named module
            # files define and is discharged only by a complete run of exactly
            # that command, from the project root, on the current source,
            # whose own report shows those cases passed; another runner's
            # execution of the same cases never stands in for it.
            argv, modules = command
            files, problems = [], []
            interpreter = _resolved_interpreter(argv[0], self.project)
            if interpreter is None:
                problems.append(f"interpreter {argv[0]} cannot be resolved in this environment")
            for module in modules:
                rel = module.replace(".", "/") + ".py"
                if self.project and (Path(self.project) / rel).is_file():
                    files.append(rel)
                else:
                    problems.append(f"{module} is not a test module file in the project")
            cases = self._module_case_ids(files) if not problems else []
            if not problems and not cases:
                problems.append("the named module defines no test case")
            binding.update(kind="command", command=" ".join(argv), files=files, interpreter=interpreter,
                           cases=sorted(cases) if not problems else [], problem="; ".join(problems)[:300])
            entry["binding"], entry["cases"] = binding, list(binding["cases"])
            if binding["problem"]:
                self._note(f"task {entry.get('task')}: NOT RUN item not bound ({binding['problem']}): {item[:80]}")
            return
        whole_class = _CLASS_CLAIM.match(item.strip())
        if whole_class is not None:
            # A whole test class, ``file::Class`` (class-claim batch,
            # 2026-10-30; completion-workflow-02 t6: the item named the
            # class whose eight methods both gate receipts showed passed,
            # and the item bound to nothing). It binds to the methods the
            # class body declares, as a floor, and is discharged only by a
            # later complete run from the project root whose own report
            # attests the class's membership and shows every attested and
            # declared member passed (``_class_run_cases``). A selection
            # inside the module, a hook or pattern that left a member out,
            # an unattested run or a changed class never discharges it.
            file = whole_class.group(1)[2:] if whole_class.group(1).startswith("./") else whole_class.group(1)
            declared, problem = _class_declaration(self.project, file, whole_class.group(2))
            binding.update(kind="class", class_id=f"{file}::{whole_class.group(2)}", file=file,
                           class_name=whole_class.group(2), declared=list(declared), cases=list(declared),
                           problem=problem[:300])
            entry["binding"], entry["cases"] = binding, list(binding["cases"])
            if binding["problem"]:
                self._note(f"task {entry.get('task')}: NOT RUN item not bound ({binding['problem']}): {item[:80]}")
            return
        scenarios, range_problems, item_ids = self._not_run_scenario_ids(entry, item)
        if range_problems:
            # An undefined, reversed or ambiguous range binds nothing and says
            # why (cycle22): never its endpoints, never a dropped middle.
            binding.update(kind="scenario", scenario=None, problem="; ".join(range_problems)[:300])
            entry["binding"], entry["cases"] = binding, []
            self._note(f"task {entry.get('task')}: NOT RUN item not bound ({binding['problem']}): {item[:80]}")
            return
        if scenarios and _UNRUNNABLE_CLAIM.search(item_ids):
            # A claim a passing run cannot establish beside the scenarios it
            # names (run21 O70: "mutation detection and P1-P4 HTTP
            # acceptance"): held as written; the suite passing never shows a
            # mutant was detected.
            binding.update(kind="mixed", scenario=", ".join(scenarios),
                           problem=("names a claim a passing run cannot establish ("
                                    + _UNRUNNABLE_CLAIM.search(item_ids).group(0).lower()
                                    + ") beside its scenarios; held as written"))
            entry["binding"], entry["cases"] = binding, []
            self._note(f"task {entry.get('task')}: NOT RUN item not bound ({binding['problem']}): {item[:80]}")
            return
        explicit = _cited_cases(item)
        rest = _CASE_CITATION.sub(" ", item)
        names = list(dict.fromkeys(m.group(1) for m in _CASE_NAME.finditer(rest)))
        leftover = _CASE_NAME.sub(" ", rest)
        if _CLASS_CITATION.search(leftover):
            # A whole-class citation beside other claims (a scenario, a
            # case, a second class): bound only when it is the whole item,
            # so a mixed claim is held as written rather than shrunk to the
            # part the harness can read.
            binding.update(kind="case", problem="names a whole test class beside other claims; a class is "
                                                 "bound only when it is the whole item: "
                                                 + " ".join(item.split())[:120])
            entry["binding"], entry["cases"] = binding, []
            self._note(f"task {entry.get('task')}: NOT RUN item not bound ({binding['problem']}): {item[:80]}")
            return
        # A word shaped like a case outside any path operand (``Class.test_x
        # .extra``) is a claim the harness cannot read whole, not a path.
        unreadable = any(re.search(r"\btest\w*", tok) for tok in leftover.split()
                         if "/" not in tok and not _SOURCE_FILE.search(tok))
        if "::" in leftover or "[" in leftover or "]" in leftover or unreadable:
            # Part of the item is shaped like a case id the harness cannot
            # read whole (a suffix after the case, an unclosed or stray
            # bracket): the claim is held as written, never its readable
            # prefix and never a path.
            binding.update(kind="case", problem="names a case id the harness cannot read whole: "
                                                 + " ".join(leftover.split())[:120])
            entry["binding"], entry["cases"] = binding, []
            self._note(f"task {entry.get('task')}: NOT RUN item not bound ({binding['problem']}): {item[:80]}")
            return
        if not (scenarios or explicit or names):
            if _item_paths(item):
                binding["kind"] = "paths"
            else:
                binding["problem"] = "names no scenario, case or path the harness can identify"
            entry["binding"], entry["cases"] = binding, []
            return
        binding["kind"] = ("scenario" if scenarios and not (explicit or names)
                           else "case" if not scenarios else "mixed")
        binding["scenario"] = ", ".join(scenarios) if scenarios else None
        cases, problems = [], []
        for scenario in scenarios:
            rows = [r for r in self.evidence_obligations
                    if r.get("task") == entry.get("task") and r.get("kind") == "acceptance"
                    and r.get("proof_id") == scenario and not r.get("superseded_by")]
            if not rows:
                problems.append(f"{scenario} is not a scenario this task declares")
            elif not list(rows[-1].get("cases") or ()):
                problems.append(f"{scenario} binds no case yet (no valid PROVES line)")
            else:
                cases.extend(str(c) for c in rows[-1]["cases"])
        if explicit or names:
            resolved, problem = self._resolve_case_ids(explicit + [n.replace("::", ".") for n in names])
            cases.extend(resolved)
            if problem:
                problems.append(problem)
        if problems:
            binding["problem"] = "; ".join(problems)[:300]
        else:
            binding["cases"] = sorted(set(cases))
        entry["binding"] = binding
        entry["cases"] = list(binding["cases"])
        if binding["problem"]:
            self._note(f"task {entry.get('task')}: NOT RUN item not bound ({binding['problem']}): {item[:80]}")

    def _not_run_scenario_ids(self, entry: dict, item: str):
        """``(scenarios, problems, text)``: the scenario ids a NOT RUN item
        names, with inclusive ranges ``P<a>-P<b>`` (ASCII hyphen or en dash)
        expanded only when every id in the range is a scenario the reporting
        task declares (cycle22; run21 t8 wrote "P1–P4", which bound P1 and P4
        and dropped P2 and P3, while "P1-P4" bound nothing). A reversed,
        single-point, chained or partly undeclared range is a problem and
        binds nothing. ``text`` is the item with ranges replaced by their
        ids, for the later grammars."""
        declared = {r.get("proof_id") for r in self.evidence_obligations
                    if r.get("task") == entry.get("task") and r.get("kind") == "acceptance"
                    and r.get("proof_id") and not r.get("superseded_by")}
        problems, expanded = [], []

        def expand(match):
            whole = " ".join(match.group(0).split())
            if match.group(3) is not None:
                problems.append(f"{whole} chains more than one range; name each range or scenario on its own")
                return " "
            low, high = int(match.group(1)), int(match.group(2))
            if low >= high:
                problems.append(f"{whole} is not an increasing range")
                return " "
            ids = [f"P{n}" for n in range(low, high + 1)]
            missing = [i for i in ids if i not in declared]
            if missing:
                problems.append(f"{whole} names {', '.join(missing)}, which this task does not declare")
                return " "
            expanded.extend(ids)
            return " " + " ".join(ids) + " "
        text = _SCENARIO_RANGE.sub(expand, item)
        ids = list(dict.fromkeys(re.findall(r"(?<![\w./:-])(P[0-9]+)(?![\w./:-])", text)))
        return ids, problems, text

    def _not_run_runnable(self, entry: dict) -> bool:
        """Whether a check can discharge the item as it stands now: the
        recorded decision (``executable``), and none of its bound cases gone
        from the current source (``_bound_cases_gone``). Every reader of
        "runnable" asks this, so the route notes, the editing decision and
        the DONE declaration plan agree."""
        executable = entry.get("executable") if "executable" in entry else self._not_run_executable(entry)
        return bool(executable) and not self._bound_cases_gone(entry)

    def _rebind_not_run(self, task) -> None:
        """A scenario-bound NOT RUN item follows its scenario's binding: when
        a later PROVES line rebinds or invalidates the scenario, the item is
        bound again to what the scenario binds now (nothing, when the
        declaration is invalid) and its typed row's cases follow, so a case
        set the lead has since withdrawn never discharges it. An execution
        already recorded against the old cases is reset: it showed other
        cases."""
        for entry in self.unexecuted_acceptance:
            if entry.get("task") != task or (entry.get("binding") or {}).get("kind") != "scenario":
                continue
            before = list(entry.get("cases") or ())
            self._bind_not_run(entry)
            if list(entry.get("cases") or ()) == before:
                continue
            for row in self.evidence_obligations:
                if (row.get("kind") == "not_run" and row.get("task") == task
                        and (row.get("entry") or {}).get("item") == entry.get("item")):
                    row["cases"] = list(entry.get("cases") or ())
                    if row.get("execution") in ("ran", "failed"):
                        row.update(execution="missing", receipts=[], source_digest=None,
                                   reason="the scenario was rebound after the execution")
                    self._sync_obligation(row)

    def _module_case_ids(self, files) -> List[str]:
        """Every ``file::[Class::]test`` a test module file defines, read with
        ``ast`` and never executed, for a command bound to whole modules."""
        import ast
        if not self.project:
            return []
        out = []
        for rel in files:
            try:
                tree = ast.parse((Path(self.project) / rel).read_text(errors="ignore"))
            except (OSError, SyntaxError, ValueError):
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                    out.append(f"{rel}::{node.name}")
                elif isinstance(node, ast.ClassDef):
                    for inner in node.body:
                        if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and inner.name.startswith("test"):
                            out.append(f"{rel}::{node.name}::{inner.name}")
        return sorted(set(out))

    def _gate_runs_command(self, binding: dict) -> bool:
        """Whether the effective gate runs exactly the declared python
        command from the project root, on the same resolved interpreter
        (``_same_python_command``), so the harness itself can discharge an
        item bound to it."""
        gate = self._current_gate()
        if gate is None or not self.project or not (binding or {}).get("command"):
            return False
        import shlex

        from .integration import _gate_commands
        root = Path(self.project).resolve()
        try:
            commands = [c for c in (_gate_commands(gate, root) or []) if not c.skip_reason]
        except Exception:  # noqa: BLE001 -- an unreadable gate runs nothing
            return False
        for candidate in commands:
            cwd = (root / candidate.cwd).resolve() if candidate.cwd else root
            joined = " ".join(shlex.quote(str(a)) for a in candidate.argv)
            run = dict(command=joined, interpreter=_command_interpreter(joined, cwd))
            if cwd == root and _same_python_command(run, binding):
                return True
        return False

    def _mark_not_run_display(self) -> None:
        """The display of a NOT RUN item's discharge, written from the one
        discharge decision (``_discharge_record``) on the current source,
        gate and environment, and cleared when that decision no longer holds
        (completion workflow, 2026-10-30; evidence-authority-02: the typed
        rows showed t6's bound cases ran under the merge gate while
        ``unexecuted_acceptance[].discharged_at`` stayed null). Skip facts
        keep the decision ``_discharge_named`` wrote. The mark is display
        only: nothing reads it as evidence, and the independent checker
        reports a mark the record does not justify.

        A restated item whose cases ran but whose independent judgment does
        not hold on the current source (missing, insufficient, or stale) is
        executed, not discharged (root review of bae3830, item 1: the mark
        said discharged while both deciders held it): ``discharged_*`` stay
        null and the run is kept as ``executed_*`` with ``discharge_state``
        "executed, awaiting judgment", so the display, the live decision
        (``_discharging_check``) and the saved checker say one thing."""
        for entry in self.unexecuted_acceptance:
            if entry.get("kind") == "skipped":
                continue
            decided = self._discharge_record(entry)
            entry["discharge_display"] = "bound/1"
            awaiting = bool(decided is not None and entry.get("restatements")
                            and not self._restated_judged(entry))
            if decided is None:
                entry.update(executed_at=None, executed_run=None, executed_cases=None)
            else:
                entry.update(executed_at=decided["index"], executed_run=decided["run"],
                             executed_cases=list(decided["cases"]))
            if decided is None or awaiting:
                entry.update(discharged_at=None, discharged_run=None, discharged_cases=None)
                entry["discharge_state"] = "executed, awaiting judgment" if awaiting else "standing"
            else:
                entry.update(discharged_at=decided["index"], discharged_run=decided["run"],
                             discharged_cases=list(decided["cases"]))
                entry["discharge_state"] = "discharged"

    def _mark_not_run_display_safely(self) -> None:
        try:
            self._mark_not_run_display()
        except Exception:  # noqa: BLE001 -- a display never fails the run's end
            log.warning("could not refresh the NOT RUN discharge display", exc_info=True)

    def _resolve_case_ids(self, names) -> Tuple[List[str], str]:
        """``(case ids, problem)``: each name resolved to exactly one
        ``file::[Class::]test`` defined in the project's test files (read
        with ``ast``, never executed); an explicit id is checked against its
        own file. A name defined in no file, or in more than one, resolves
        nothing and names the problem, so a same-named case elsewhere never
        stands in for the one reported."""
        import ast
        if not self.project:
            return [], "no project to resolve cases in"
        root = Path(self.project)
        defined: Dict[str, List[str]] = {}
        for test_file in sorted(root.rglob("test*.py")):
            if ".quadratus" in test_file.parts or any(part in ("node_modules", ".venv", "venv") for part in test_file.parts):
                continue
            try:
                tree = ast.parse(test_file.read_text(errors="ignore"))
            except (OSError, SyntaxError, ValueError):
                continue
            rel = test_file.relative_to(root).as_posix()
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                    defined.setdefault(node.name, []).append(f"{rel}::{node.name}")
                elif isinstance(node, ast.ClassDef):
                    for inner in node.body:
                        if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and inner.name.startswith("test"):
                            defined.setdefault(f"{node.name}.{inner.name}", []).append(f"{rel}::{node.name}::{inner.name}")
                            defined.setdefault(inner.name, []).append(f"{rel}::{node.name}::{inner.name}")
        all_ids = {cid for ids in defined.values() for cid in ids}
        resolved, problems = [], []
        for name in names:
            name = str(name).strip()
            base, bracket, param = name.partition("[")
            suffix = bracket + param
            if "::" in base and (base.split("::")[0].endswith(".py") or "/" in base.split("::")[0]):
                if base in all_ids:
                    # The parameter is part of the identity: only a run
                    # that shows exactly that instance discharges it.
                    resolved.append(base + suffix)
                else:
                    problems.append(f"{name} is not defined where it says")
                continue
            hits = sorted(set(defined.get(base, [])))
            if len(hits) == 1:
                resolved.append(hits[0] + suffix)
            elif not hits:
                problems.append(f"{name} is not defined in any test file")
            else:
                problems.append(f"{name} is defined in {len(hits)} places ({', '.join(hits[:3])})")
        if problems:
            return [], "; ".join(problems)[:300]
        return sorted(set(resolved)), ""

    def _record_proves(self, key, role, reply) -> None:
        """Bind the active task's proof scenarios to the named cases an
        editing reply delivers: every line that starts with ``PROVES:`` is a
        declaration, read whole. A well-formed line is
        'PROVES: P<n> - file::[Class::]test_case' naming one of the task's
        scenarios and a case in a test file; several well-formed lines for
        one scenario bind its case set. A malformed line fails closed: when
        it names a scenario, that scenario's binding becomes an invalid
        declaration (its cases cleared, execution and sufficiency reset)
        and no earlier valid binding satisfies it; when it names none, the
        whole reply's declaration is invalid and every scenario of the task
        is reset. A whole, valid one-case line naming a scenario the task
        never declared is the one exception (cycle16): it is recorded as a
        problem, binds nothing and resets nothing else. A later reply of only well-formed lines restores a
        binding; a scenario a reply does not mention keeps its binding. A
        binding is a claim of where the proof lives, never execution or
        sufficiency."""
        task = getattr(self._active_spec, "task_id", "run")
        mine = {r.get("proof_id"): r for r in self.evidence_obligations
                if r.get("task") == task and r.get("kind") == "acceptance" and r.get("proof_id")}
        if not mine:
            return
        bound: Dict[str, List[str]] = {}
        invalid: Dict[str, str] = {}
        whole_invalid = []
        undeclared = []
        for match in _PROVES_ANY.finditer(reply or ""):
            body = match.group(1).strip()
            well = _PROVES_BODY.fullmatch(body)
            pid_only = _PROVES_PID.match(body)
            if well is None:
                loose = _PROVES_LOOSE.fullmatch(body)
                problem = (_proves_problem(loose.group(2)) if loose
                           else f"'{body[:80]}' is not 'P<n> - file::[Class::]test_case'")
                if pid_only and pid_only.group(1) in mine:
                    invalid[pid_only.group(1)] = problem
                else:
                    whole_invalid.append(problem)
                continue
            pid, case = well.group(1), well.group(2).strip().strip("`")
            if pid not in mine:
                known = _CASE_CITATION.fullmatch(case)
                if known is not None and is_test_path(known.group(1)):
                    # A whole, valid one-case line naming a scenario the task
                    # never declared (cycle16, cycle15 t4's P8): the line is
                    # refused and binds nothing, and it does not reset the
                    # declared scenarios it never named. Anything less than a
                    # valid case keeps the whole-reply reset below.
                    undeclared.append(f"{pid}: not a scenario of this task; nothing was bound for it")
                else:
                    whole_invalid.append(f"{pid}: not a scenario of this task")
                continue
            cited = _CASE_CITATION.fullmatch(case)
            if cited is None or not is_test_path(cited.group(1)):
                invalid[pid] = _proves_problem(case)
                continue
            if case in bound.setdefault(pid, []):
                # The strict protocol: distinct cases bind a set; the same
                # case declared twice is an invalid declaration, never
                # collapsed in silence (root's finding on a1b1840).
                invalid[pid] = f"duplicate PROVES line for {case[:80]}"
                continue
            bound[pid].append(case)
        problems = [f"{pid}: {why}" for pid, why in invalid.items()] + whole_invalid + undeclared
        reset = set(mine) if whole_invalid else set(invalid)
        for pid in sorted(reset):
            record = mine[pid]
            why = invalid.get(pid) or "; ".join(whole_invalid)
            record.update(cases=[], binding=f"invalid declaration: {why[:160]}", execution="missing",
                          sufficiency="unjudged", judged_by="", judged_binding="", receipts=[], source_digest=None,
                          discharged_cases=None, reason="invalid PROVES declaration")
            record.setdefault("binding_history", []).append(dict(role=role, author=key, cases=[], invalid=why[:160],
                                                                 after_check=len(self.checks)))
        for pid, cases in bound.items():
            if pid in reset:
                continue
            record = mine[pid]
            previous = tuple(record.get("cases") or ())
            record["cases"] = list(cases)
            record["binding"] = "bound"
            if record.get("reason") == "invalid PROVES declaration":
                record["reason"] = ""
            record.setdefault("binding_history", []).append(dict(role=role, author=key, cases=list(cases),
                                                                 after_check=len(self.checks)))
            if previous and previous != tuple(cases):
                self._note(f"task {task}: {pid} rebound from {', '.join(previous)} to {', '.join(cases)}")
        self._rebind_not_run(task)
        if problems:
            self.proof_binding_problems.extend(dict(task=task, role=role, author=key, problem=p) for p in problems)
            self._note(f"task {task}: PROVES declaration invalid: {'; '.join(problems)[:200]}")

    def _restated_executed_ids(self, audited=None, auditor: Optional[str] = None) -> List[str]:
        """Ids of restated NOT RUN items the harness's own complete run on
        the current source executed and no current independent judgment
        holds: evidence awaiting a judgment (cycle05). With ``audited``, only
        items holding one of those requirements; with ``auditor``, only items
        that seat may judge (it did not write the restatement)."""
        from collections import Counter
        counts = Counter(e.get("oid") for e in self.unexecuted_acceptance if e.get("oid"))
        out = []
        for e in self._standing_not_run():
            if not (e.get("oid") and counts[e["oid"]] == 1 and e.get("restatements")):
                continue
            if self._discharge_record(e) is None:
                continue
            if audited is not None and not any(r in audited for r in e.get("requirements") or ()):
                continue
            if auditor is not None and auditor == (e["restatements"][-1] or {}).get("author"):
                continue
            out.append(e["oid"])
        return out

    def _lower_unexercised_errors(self, found: dict, audited, original=None, lowered=None) -> List[dict]:
        """Turn an error or refusal requirement's MET into NOT MET unless it
        cites a test case executed and passed on this source; never the other
        way. The auditor's reasons, applicability notes and readings do not
        lift it: only a run does (Davis, 2026-10-16). ``original`` is the
        auditor's verdict before any other lowering: a MET already lowered
        for an unexecuted obligation still carries this rule's reason too,
        so neither fact hides the other."""
        texts = self.memory.ledger.requirements
        out = []
        for rid, (met, why) in list((original if original is not None else found).items()):
            if not met or rid not in audited or not _behavioural_error(texts.get(rid, "")):
                continue
            if self._executed_cases(_cited_cases(why), rid=rid):
                continue
            reason = ("an error or refusal requirement: the MET cites no exact test case shown executed and "
                      "passed on this source (a complete approved check's case record, or a passing scratch "
                      "demonstration of this requirement); negative-path behaviour must be shown by a run, "
                      "not read")
            held_met, held_why = found.get(rid, (met, why))
            earlier = next((e for e in (lowered or ()) if e.get("requirement") == rid), None)
            if not held_met and held_why != why:
                reason = f"{reason}; also {held_why}"[:600]
            found[rid] = (False, reason)
            if earlier is not None:
                earlier["reason"] = reason
            else:
                out.append(dict(requirement=rid, auditor_said=why[:200], reason=reason))
        return out

    def _sync_obligation(self, record: dict) -> None:
        """Re-derive the typed fields (binding digest, established,
        executable) after the session marked a record's modality."""
        record.update(delivery.EvidenceObligation.from_dict(record).to_dict())

    def _obligation_receipt(self, record: dict) -> Optional[dict]:
        """The harness receipt that would discharge ``record`` now, from the
        session's own check records, or None: for a NOT RUN item the later
        complete required run naming every path whole; for a skip fact the
        same command's run that executed the named cases; for a declared
        acceptance behavior the task's last passing check on the current
        source. Built from the check as recorded; nothing a model wrote."""
        entry = record.get("entry") or {}
        kind = record.get("kind")
        matching = [e for e in self.unexecuted_acceptance
                    if e.get("task") == record["task"] and e.get("item") == entry.get("item")]
        if kind in ("not_run", "skipped"):
            for found in matching:
                decided = self._discharge_record(found)
                if decided is not None:
                    index = decided["index"]
                    check = self.checks[index]
                    # Exactly the decision's evidence: the run that decided
                    # it and the cases it verified, never a second reading
                    # of the check and never the claim.
                    return dict(id=f"check-{index + 1}", source=check.get("source"), gate=check.get("gate"),
                                runner="gate", run=decided["run"], **self._receipt_identity(check, record),
                                completed=True, passed=bool(check.get("passed")),
                                passed_cases=list(decided["cases"]), skipped_cases=[])
            return None
        # A declared acceptance behavior: discharged only by a passing check
        # on exactly the current source, under the obligation's gate and
        # environment, binding the requirement revisions the row binds
        # (delivery.discharge), whose own complete runs name every bound
        # case as passed. No bound case, no receipt: a green check that
        # exercised something else discharges nothing. When the run happened
        # is not part of that identity: a later task whose draft left the
        # bytes unchanged is shown by the run that already passed on them
        # (root's two-task control on fff4e99, where the row's registration
        # index refused the receipt and the reviewer read "missing" on a
        # source the harness had checked). A failed, filtered, other-source,
        # other-gate or other-revision run is refused exactly as before.
        cases = [str(c) for c in (record.get("cases") or ())]
        if not cases:
            return None
        current = self._source_fingerprint() if self.project else None
        environment = self._environment_id()
        bound = [(str(rid), rev) for rid, rev in (record.get("requirements") or ())]

        def binds(run: dict) -> bool:
            # The revisions the run launched under are the row's: checked
            # here as well as at discharge, so a newer run under another
            # revision never shadows an older one that binds the right ones.
            held = run.get("requirements") if isinstance(run.get("requirements"), dict) else {}
            return all(held.get(rid) == rev for rid, rev in bound)
        for index in range(len(self.checks) - 1, -1, -1):
            check = self.checks[index]
            if (not check.get("passed") or check.get("source") != current
                    or (check.get("gate") or "") != (record.get("gate") or "")
                    or (check.get("environment") or "") != (environment or "") or not binds(check)):
                continue
            if set(cases) <= self._check_passed_cases(check):
                return dict(id=f"check-{index + 1}", source=check.get("source"), gate=check.get("gate"),
                            runner="gate", **self._receipt_identity(check, record), completed=True, passed=True,
                            passed_cases=list(cases), skipped_cases=[])
        # The harness's own run of the task's checks on this exact source
        # before a review or a recheck (_observe_checks) is the same
        # execution under the same identity: it discharges the row the same
        # way, so the seat judging the evidence sees what ran (workflow
        # repair, 2026-10-24: the ticket run's reviewers read "execution
        # missing" on rows whose cases the harness had just run and passed,
        # because only self.checks was read here). A failed run discharges
        # nothing, as with a check; the proof rows name it.
        for index in range(len(self.review_checks) - 1, -1, -1):
            seen = self.review_checks[index]
            if (not seen.get("passed") or seen.get("source") != current
                    or (seen.get("gate") or "") != (record.get("gate") or "")
                    or (seen.get("environment") or "") != (environment or "") or not binds(seen)):
                continue
            if set(cases) <= self._check_passed_cases(self._observation_named(seen)):
                return dict(id=f"observed-{index + 1}", source=seen.get("source"), gate=seen.get("gate"),
                            runner="gate", provenance=f"harness run of the task's checks on this source before "
                                                      f"{seen.get('stage')}",
                            **self._receipt_identity(seen, record), completed=True, passed=True,
                            passed_cases=list(cases), skipped_cases=[])
        return self._evidence_receipt(record) or self._scratch_receipt(record)

    @staticmethod
    def _receipt_identity(check: dict, record: dict) -> dict:
        """The identity a check carries as recorded: the environment it ran
        in and the requirement revisions current when it ran, read off the
        check, never relabelled from now (root's receipt finding on
        f4fe5b7). A check recorded without them discharges nothing: None is
        no revision and no environment."""
        revisions = check.get("requirements") if isinstance(check.get("requirements"), dict) else {}
        return dict(environment=check.get("environment"),
                    requirements=[[rid, revisions.get(rid)] for rid, _ in record["requirements"]])

    def _not_run_executable(self, entry: dict) -> bool:
        """Whether a check can ever discharge the item as written: bound to
        cases (a scenario, case, class or command claim), or naming only test
        files a run can take whole. An item naming production files, or
        nothing a run takes, is not: running a repair task never clears it
        (cycle02, completion-cycle01 O15 named tickets/api.py)."""
        binding = entry.get("binding") or {}
        if entry.get("cases"):
            return True
        if binding.get("kind") == "paths":
            paths = _item_paths(str(entry.get("item") or ""))
            return bool(paths) and all(is_test_path(p) for p in paths)
        return False

    def _not_run_owned(self, entry: dict, task: str) -> bool:
        """Whether ``task``'s lead may restate the item: the task that
        reported it, or one that took it over by DISCHARGES."""
        return entry.get("task") == task or any(t.get("task") == task for t in entry.get("taken_over_by") or ())

    def _owned_unexecutable(self, task: str) -> List[dict]:
        """Standing NOT RUN items ``task`` reported or took over that name
        nothing a check can run and are not restated yet."""
        return [e for e in self._standing_not_run() if e.get("oid") and self._not_run_owned(e, task)
                and (not e.get("restatements") or self._bound_cases_gone(e))
                and not self._not_run_runnable(e)]

    # -- scope-blocked repair routing (cycle04) ---------------------------------
    def _scope_path_problem(self, spec, raw: str) -> Tuple[str, bool]:
        """Why ``raw`` cannot be an outside-scope repair site for ``spec``, and
        whether the reason is an operator prohibition; ("", False) when it is
        a project file a later authorized task could be granted. Read with
        the existing scope semantics: a path the task's grant permits (an
        unrestricted scope permits everything) is inside it."""
        from .deptree import DEPENDENCY_DIRS
        path = str(raw or "").strip().strip("`'\"").removeprefix("./")
        if not path or len(path) > _MAX_SCOPE_PATH_CHARS:
            return "not a project-relative path", False
        if path.startswith(("/", "~")) or "\\" in path or any(c in path for c in "*?[]{}") or ":" in path:
            return "not a project-relative file path (absolute, home, glob or drive)", False
        parts = PurePosixPath(path).parts
        if not parts or any(p in ("..", ".") for p in path.split("/")) or path.endswith("/"):
            return "escapes the project or names a directory", False
        if parts[0] in (".quadratus", ".git") or ".git" in parts:
            return "a harness or version-control path", True
        if any(p in DEPENDENCY_DIRS for p in parts):
            return "inside a dependency tree", True
        root = Path(self.project).resolve()
        here = Path(self.project)
        for part in parts:
            here = here / part
            if here.is_symlink():
                return "passes through a link", True
        try:
            if not (Path(self.project) / path).resolve().is_relative_to(root):
                return "resolves outside the project", False
            if (Path(self.project) / path).is_dir():
                # A file the repair must touch, not a folder grant; a new file
                # that does not exist yet is still a valid name.
                return "names an existing directory, not a file", False
        except (OSError, RuntimeError, ValueError):
            return "cannot be resolved inside the project", False
        scope = getattr(spec, "scope", None)
        if scope is not None and any(_scope_matches(path, g) for g in scope.forbidden_paths or ()):
            return "forbidden by the operator", True
        if any(not limit.permits(path) for limit in self._outer_limits(spec)):
            return "outside the operator's limits", True
        if self._policy_denies(path):
            return "policy-denied or sensitive", True
        if scope is None or scope.permits(path):
            return "inside this task's grant", False
        return "", False

    def _next_run_seq(self) -> int:
        """The next run number across this session's check attempts and
        observed runs (cycle04 correction): their order decides which failure
        is current. Derived from the records, so a restored session goes on
        counting from what it carried."""
        seqs = [c.get("seq") for o in self.task_outcomes for c in o.checks]
        if self._outcome is not None and self._outcome not in self.task_outcomes:
            seqs += [c.get("seq") for c in self._outcome.checks]
        seqs += [c.get("seq") for c in getattr(self, "review_checks", []) or []]
        return 1 + max([q for q in seqs if isinstance(q, int)] or [0])

    def _task_runs(self, task_id: str) -> List[Tuple[str, dict]]:
        """``task_id``'s check runs in order: its observed pre-review runs and
        its check attempts, by run number; records without one (an earlier
        engine) in the order they happen, observed before attempts."""
        outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == task_id), None)
        if outcome is None and self._outcome is not None and self._outcome.task_id == task_id:
            outcome = self._outcome
        observed = [("observed", c) for c in getattr(self, "review_checks", []) or [] if c.get("task") == task_id]
        attempts = [("attempt", c) for c in (outcome.checks if outcome is not None else [])]
        runs = observed + attempts
        if all(isinstance(r.get("seq"), int) for _, r in runs):
            runs.sort(key=lambda kr: kr[1]["seq"])
        return runs

    @staticmethod
    def _case_state(run: dict, case: str) -> Tuple[str, Optional[tuple]]:
        """Whether ``case`` failed in ``run`` (with its failure identity:
        exception type, crash site and message digest, as the harness's
        producer recorded them), passed, or is unknown: a passing run passed
        it; a failed run whose every failed command has a parsed report
        failed it if a record names it and passed it otherwise; anything
        else is unknown."""
        if run.get("passed"):
            return "passed", None
        receipts = [r for r in run.get("receipts") or [] if r.get("status") not in ("passed", "skipped")]
        reports = [r.get("report") for r in receipts] if receipts else [run.get("report")]
        if not reports or any(not (isinstance(rep, dict) and rep.get("state") == "parsed") for rep in reports):
            return "unknown", None
        for rep in reports:
            for f in rep.get("failures") or []:
                if isinstance(f, dict) and str(f.get("nodeid")) == case:
                    return "failed", (str(f.get("exc_type") or ""), f.get("site"), f.get("message_sha"))
        return "passed", None

    def _task_failed_cases(self, task_id: str) -> Tuple[set, dict]:
        """The failed case ids of ``task_id``'s latest check run only (an
        attempt or an observed pre-review run), with that run's position and
        identity: an earlier failure a later run superseded is not current
        (root review of b9d136e, R2)."""
        runs = self._task_runs(task_id)
        if not runs:
            return set(), {}
        kind, run = runs[-1]
        anchor = dict(kind=kind, position=len(runs) - 1, seq=run.get("seq"), attempt=run.get("attempt"),
                      stage=run.get("stage"), source=run.get("source"))
        if run.get("passed"):
            return set(), anchor
        cases = set()
        receipts = [r for r in run.get("receipts") or [] if r.get("status") not in ("passed", "skipped")]
        for report in ([r.get("report") for r in receipts] if receipts else [run.get("report")]):
            if isinstance(report, dict) and report.get("state") == "parsed":
                cases |= {str(f.get("nodeid")) for f in report.get("failures") or [] if isinstance(f, dict)}
        anchor["identities"] = {c: list(self._case_state(run, c)[1] or ()) for c in cases}
        return cases, anchor

    def _case_declaration_current(self, decl: dict) -> bool:
        """Whether a case declaration still describes the same failure
        episode (root reviews of b9d136e, R2, and of 8bab1f4): every run of
        its task after the one it was made against still shows that case
        failing with the same failure identity, the exception type, the
        crash site (file and line) and the message digest the harness's
        producer recorded. A run that passed it, or any other failure of it
        (another assertion in the same test is another failure), ends the
        episode and the claim with it. A claim whose failure carried no site
        or message (an older or a node report) cannot be shown to continue
        and does not hold. An unknown run (no parsed report) says nothing
        either way. Source changes alone do not end it: in-scope edits
        beside a production blocker that leave the same failure keep it."""
        anchor = decl.get("anchor") or {}
        position = anchor.get("position")
        if not isinstance(position, int):
            return False
        runs = self._task_runs(decl.get("task"))
        if position >= len(runs):
            return False
        expected = tuple((anchor.get("identities") or {}).get(decl.get("id")) or ())
        if len(expected) != 3 or not expected[1] or not expected[2]:
            return False
        for _, run in runs[position + 1:]:
            state, identity = self._case_state(run, decl.get("id"))
            if state == "passed" or (state == "failed" and tuple(identity or ()) != expected):
                return False
        return True

    def _record_scope_declarations(self, key, role, reply) -> None:
        """Read the owning lead's 'OUTSIDE SCOPE: <id> - <path>[, ...]' lines
        from an editing reply (cycle04). Each is an untrusted routing claim:
        accepted only for this task's open blocking finding or a failed case
        of its latest check, and only for paths inside the project and
        outside this task's grant (``_scope_path_problem``). An accepted
        finding is routed ``scope`` (no further editor round for it in this
        task: ``_routed_ids``); an accepted case can let a later gate fix be
        skipped (``_scope_covers``) and is carried into the task's check
        debt. Every line, accepted or not, is kept with its reasons; a
        repeated identical declaration changes nothing. Nothing is granted,
        edited, closed or proved."""
        spec = self._active_spec
        if role not in _EDITING_ROLES or spec is None or not self.project or not reply:
            return
        lines = _OUTSIDE_SCOPE.findall(reply)
        if not lines:
            return
        task_id = spec.task_id
        scope = getattr(spec, "scope", None)
        grouped: Dict[str, List[str]] = {}
        for ident, paths in lines[:_MAX_SCOPE_LINES]:
            ident = ident.strip().strip("`'\".,;")
            listed = grouped.setdefault(ident, [])
            for raw in re.split(r"\s*,\s*", paths):
                raw = raw.strip().strip("`'\"")
                if raw and raw not in listed:
                    listed.append(raw)
        source = self._source_fingerprint()
        failed, anchor = self._task_failed_cases(task_id)
        for ident, raw_paths in grouped.items():
            entry = dict(task=task_id, role=role, author=key, id=ident, source=source, at_check=len(self.checks),
                         lines_read=min(len(lines), _MAX_SCOPE_LINES), paths=[], forbidden=[], rejected=[])
            if len(lines) > _MAX_SCOPE_LINES:
                entry["lines_ignored"] = len(lines) - _MAX_SCOPE_LINES
            if len(raw_paths) > _MAX_SCOPE_PATHS:
                entry["rejected"].extend(dict(path=p[:_MAX_SCOPE_PATH_CHARS], why="past the per-id path bound")
                                         for p in raw_paths[_MAX_SCOPE_PATHS:])
            record = next((r for r in self.review_findings if r.get("fid") == ident), None)
            if scope is not None and getattr(scope, "review_only", False):
                entry.update(kind="none", status="rejected",
                             why="a review-only task reports and does not repair; its findings already go to "
                                 "separately scoped tasks")
            elif record is not None:
                entry["kind"] = "finding"
                if record.get("task") != task_id:
                    entry.update(status="rejected", why=f"{ident} belongs to {record.get('task')}, not {task_id}")
                elif record.get("closed") or record.get("kind") == "unreviewed":
                    entry.update(status="rejected", why=f"{ident} is not an open blocking finding")
            elif ident in failed:
                identity = (anchor.get("identities") or {}).get(ident)
                entry.update(kind="case", anchor=dict(anchor, identities={ident: identity}))
                if not (identity and len(identity) == 3 and identity[1] and identity[2]):
                    # Recorded, but it can never cover a gate: nothing shows
                    # which failure it names (no site or message).
                    entry["identity_unknown"] = True
            else:
                entry.update(kind="unknown", status="rejected",
                             why=f"{ident} is neither an open finding of {task_id} nor a failed case of its latest "
                                 "check")
            if entry.get("status") != "rejected":
                for raw in raw_paths[:_MAX_SCOPE_PATHS]:
                    problem, forbidden = self._scope_path_problem(spec, raw)
                    path = raw.strip().strip("`'\"").removeprefix("./")
                    if not problem:
                        entry["paths"].append(path)
                    elif forbidden:
                        entry["forbidden"].append(dict(path=path[:_MAX_SCOPE_PATH_CHARS], why=problem))
                    else:
                        entry["rejected"].append(dict(path=path[:_MAX_SCOPE_PATH_CHARS], why=problem))
                if entry["paths"]:
                    entry["status"] = "routed"
                elif entry["forbidden"]:
                    entry.update(status="forbidden",
                                 why="every named path is one the operator forbids; no task may be routed to it")
                else:
                    entry.update(status="rejected", why="no named path is a project file outside this task's grant")
            previous = [d for d in self.scope_declarations if d.get("task") == task_id and d.get("id") == ident
                        and d.get("status") == "routed"
                        and (d.get("kind") != "case" or self._case_declaration_current(d))]
            known = sorted({p for d in previous for p in d.get("paths") or []})
            if entry.get("status") == "routed" and set(entry["paths"]) <= set(known):
                entry["repeat"] = True
            self.scope_declarations.append(entry)
            if entry.get("status") == "routed" and record is not None:
                paths = sorted(set(known) | set(entry["paths"]))
                if not record.get("route") or record.get("route") == "scope":
                    record.update(route="scope", scope_paths=paths,
                                  route_note=f"its lead declared the repair site outside {task_id}'s scope: "
                                             + ", ".join(paths))
                else:
                    record["scope_paths"] = paths
            elif entry.get("status") == "forbidden" and record is not None:
                record["scope_forbidden"] = True
            if entry.get("status") != "routed" or not entry.get("repeat"):
                self._note(f"{task_id}: OUTSIDE SCOPE {ident}: {entry.get('status')}"
                           + (f" ({', '.join(entry['paths'])})" if entry["paths"] else f" ({entry.get('why', '')})"))

    def _scope_case_paths(self, task_id: str) -> Dict[str, List[str]]:
        """Accepted outside-scope paths of ``task_id``, by failed case id."""
        out: Dict[str, List[str]] = {}
        for d in self.scope_declarations:
            if (d.get("task") == task_id and d.get("kind") == "case" and d.get("status") == "routed"
                    and self._case_declaration_current(d)):
                out[d["id"]] = sorted(set(out.get(d["id"], [])) | set(d.get("paths") or []))
        return out

    def _check_failures_complete(self, result) -> Tuple[Optional[set], str]:
        """The failed case ids of a failed check when every failed command's
        harness report names every failure as a test-body case, or (None,
        why) for anything else: a command with no parsed report, a timeout or
        a runner error, a setup or collection error, a failure count the
        record does not cover, or a truncated record."""
        receipts = list(getattr(result, "receipts", None) or [])
        failed = [r for r in receipts if getattr(r, "status", None) not in ("passed", "skipped")]
        reports = [getattr(r, "report", None) for r in failed] if receipts else [getattr(result, "report", None)]
        if receipts and any(getattr(r, "status", None) != "failed" for r in failed):
            return None, "a command did not run to a test result"
        cases = set()
        for report in reports:
            if not isinstance(report, dict) or report.get("state") != "parsed":
                return None, "a failed command has no parsed harness report"
            failures = [f for f in report.get("failures") or [] if isinstance(f, dict)]
            counts = report.get("counts") or {}
            if (report.get("truncated") or report.get("collect_errors")
                    or int(counts.get("errors") or 0) or not failures
                    or int(counts.get("failed") or 0) + int(counts.get("errors") or 0) != len(failures)):
                return None, "the failure record is incomplete, truncated or has setup or collection errors"
            if any(f.get("when") != "call" for f in failures):
                return None, "a failure was raised outside a test body"
            cases |= {str(f.get("nodeid")) for f in failures}
        return cases, ""

    def _scope_covers(self, spec, result) -> Optional[dict]:
        """Whether every failed case of ``result`` has this task's accepted
        outside-scope declaration (cycle04), so a gate fix under the same
        grant cannot repair any of it: the coverage record, or None. Mixed,
        unknown, unparsed, setup and incomplete failures are never covered:
        the existing repair route decides them."""
        cases, why = self._check_failures_complete(result)
        if not cases:
            return None
        declared = self._scope_case_paths(spec.task_id)
        if not all(c in declared for c in cases):
            return None
        return dict(task=spec.task_id, cases=sorted(cases),
                    paths=sorted({p for c in cases for p in declared[c]}), at_check=len(self.checks))

    def _check_scope(self, task_id: str, attempt: dict) -> Optional[dict]:
        """The outside-scope view of one failed check attempt for its debt:
        the accepted paths, which failed cases are declared and which are
        not (an incomplete record is said, never filled in). None when no
        case of it was declared."""
        declared = self._scope_case_paths(task_id)
        if not declared:
            return None
        cases = set()
        complete = True
        receipts = [r for r in attempt.get("receipts") or [] if r.get("status") not in ("passed", "skipped")]
        for report in ([r.get("report") for r in receipts] if receipts else [attempt.get("report")]):
            if isinstance(report, dict) and report.get("state") == "parsed":
                cases |= {str(f.get("nodeid")) for f in report.get("failures") or [] if isinstance(f, dict)}
            else:
                complete = False
        covered = sorted(c for c in cases if c in declared)
        if not covered:
            return None
        return dict(paths=sorted({p for c in covered for p in declared[c]}), cases_declared=covered,
                    cases_undeclared=sorted(cases - set(covered)), record_complete=complete)

    def _explain_unsettled_check(self, spec, fid: str) -> str:
        """Why the check finding ``fid`` that ``spec`` named in RESOLVES is
        still open, from the current records, persisted on the finding as
        ``unsettled`` (cycle04; completion-cycle03 read 'did not establish
        acceptance' while t6's own check had passed and three new blockers
        and a refused fix round were the barrier): the task's latest check
        on which source (missing, failed, passed; current or earlier), the
        blocking review findings the task left open, the latest refused
        repair or correction round with its numbers, and the owner
        resolution outcomes of the review findings it named. A passing check
        is stated as such and settles nothing; an owner recheck never asked
        is ``not_attempted``, never an adverse verdict."""
        finding = next((f for f in self.findings if f["id"] == fid), None)
        if finding is None:
            return ""
        outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == spec.task_id), None)
        last = outcome.checks[-1] if outcome is not None and outcome.checks else None
        current = self._source_fingerprint() if self.project else None
        if last is None:
            check = dict(state="missing")
        else:
            counts = dict(passed=0, failed=0, skipped=0)
            parsed = True
            for receipt in last.get("receipts") or [dict(report=last.get("report"))]:
                report = receipt.get("report")
                if isinstance(report, dict) and report.get("state") == "parsed":
                    c = report.get("counts") or {}
                    counts["passed"] += int(c.get("passed") or 0)
                    counts["failed"] += int(c.get("failed") or 0) + int(c.get("errors") or 0)
                    counts["skipped"] += int(c.get("skipped") or 0)
                else:
                    parsed = False
            source = last.get("source")
            check = dict(state="passed" if last.get("passed") else "failed", attempt=last.get("attempt"),
                         source="current" if source and source == current else "earlier" if source else "unknown",
                         counts=counts if parsed else None)
        blockers = sorted(r["fid"] for r in self.review_findings
                          if r.get("task") == spec.task_id and not r.get("closed") and r.get("kind") != "unreviewed")
        refused = next((a for a in reversed(self.plan_admissions)
                        if a.get("task") == spec.task_id and not a.get("feasible")
                        and a.get("scope") not in ("mandatory", "full", "terminal")), None)
        row = next((r for r in reversed(self.review_resolutions) if r.get("task") == spec.task_id), {})
        record = dict(task=spec.task_id, check=check, blockers_left=blockers,
                      refused_round=(dict(scope=refused.get("scope"), needed=(refused.get("needed") or {}).get("tokens"),
                                          available=(refused.get("left") or {}).get("tokens"),
                                          short=(refused.get("short") or {}).get("tokens"),
                                          detail=str(refused.get("detail"))[:300]) if refused else None),
                      owner_resolution=dict(row.get("outcomes") or {}), source=current)
        parts = [f"{fid} not settled by {spec.task_id}: "
                 + ("it has no check of its own on record" if check["state"] == "missing" else
                    f"its last check (attempt {check.get('attempt')}) {check['state']} on the {check['source']} source"
                    + (f" ({check['counts']['passed']} passed, {check['counts']['failed']} failed, "
                       f"{check['counts']['skipped']} skipped)" if check.get("counts") else ""))]
        if blockers:
            parts.append(f"{spec.task_id} closed with blocking findings {', '.join(blockers)} open")
        if record["refused_round"] and record["refused_round"].get("needed") is not None:
            r = record["refused_round"]
            parts.append(f"the {r['scope']} round was refused: {r['needed']:,} tokens needed, "
                         f"{r['available']:,} available, short {r['short'] or 0:,}")
        if record["owner_resolution"]:
            parts.append("owner resolution: " + ", ".join(f"{k} {v}" for k, v in record["owner_resolution"].items()))
        record["why"] = "; ".join(parts)[:600]
        finding.setdefault("unsettled_history", []).append(dict(record))
        finding["unsettled"] = record
        return record["why"]

    def _scope_route_text(self, task_id: str, paths: List[str]) -> str:
        return (f"its lead declared the repair site outside {task_id}'s scope: {', '.join(paths)}; a task whose "
                "SCOPE includes them may RESOLVES it (a claim, not proof: its reviewer still decides)")

    def _record_runs(self, key, role, reply) -> None:
        """An owning lead's restatement of standing NOT RUN items as exact
        cases (cycle02): one 'RUNS: O<n> - file::[Class::]test_case' line per
        case, read from an editing reply or the declaration call of the task
        that reported the item or took it over. Every case must be defined
        now in a test file (``_resolve_case_ids``); any problem with an
        item's lines leaves that item exactly as it was. The original item,
        reason, task and requirements never change: the restatement is kept
        with its author, the cases it replaced and the source, and the item
        is then discharged only by a later-or-recorded complete run on the
        current source whose case record shows every case passed AND an
        independent judgment that the cases exercise the whole reported
        behaviour (``_restated_judged``). A line about another task's item,
        an unknown or settled id, or prose binds nothing."""
        if role not in _EDITING_ROLES + ("proof-binding",):
            return
        task = getattr(self._active_spec, "task_id", "run")
        lines = list(_RUNS_ANY.finditer(reply or ""))
        if not lines:
            return
        # An id held by two items identifies neither: nothing is applied to
        # it (root review of 7a141e3, item 2).
        from collections import Counter
        counts = Counter(e.get("oid") for e in self.unexecuted_acceptance if e.get("oid"))
        by_oid = {e["oid"]: e for e in self.unexecuted_acceptance if e.get("oid") and counts[e["oid"]] == 1}
        wanted: Dict[str, List[str]] = {}
        problems = []
        poisoned: set = set()
        whole_invalid = False
        for match in lines:
            body = _RUNS_BODY.fullmatch(match.group(1))
            if body is None:
                # A malformed line that names an item invalidates that item's
                # declaration whole; one that names none invalidates the
                # reply's (root review of 7a141e3, item 3: a valid line beside
                # a malformed one for the same item was applied as its whole
                # binding).
                named = re.match(r"\s*(O\d+)\b", match.group(1))
                if named:
                    poisoned.add(named.group(1))
                else:
                    whole_invalid = True
                problems.append(f"'{match.group(1).strip()[:80]}' is not 'O<n> - file::[Class::]test_case'")
                continue
            oid, case = body.group(1), body.group(2).strip("`")
            entry = by_oid.get(oid)
            if entry is None or not self._not_run_owned(entry, task):
                problems.append(f"{oid}: not a NOT RUN item this task reported or took over, or its id is ambiguous")
                continue
            wanted.setdefault(oid, []).append(case)
        if whole_invalid:
            problems.append("a malformed RUNS line names no item, so no RUNS line of this reply is read")
            wanted = {}
        for oid in sorted(poisoned & set(wanted)):
            problems.append(f"{oid}: a malformed RUNS line names it, so none of its lines is read")
            wanted.pop(oid)
        for oid, cases in wanted.items():
            entry = by_oid[oid]
            cited = [_CASE_CITATION.fullmatch(c) for c in cases]
            if any(c is None or not is_test_path(c.group(1)) for c in cited):
                problems.append(f"{oid}: each RUNS line names one exact test case")
                continue
            if len(set(cases)) != len(cases):
                problems.append(f"{oid}: the same case is declared twice")
                continue
            resolved, why = self._resolve_case_ids(cases)
            if why:
                problems.append(f"{oid}: {why}")
                continue
            entry.setdefault("restatements", []).append(dict(
                task=task, role=role, author=key, cases=list(resolved), after_check=len(self.checks),
                source=self._source_fingerprint() if self.project else None,
                previous=dict(binding=entry.get("binding"), cases=list(entry.get("cases") or []))))
            entry["binding"] = dict(kind="case", cases=list(resolved), restated_by=task)
            entry["cases"] = list(resolved)
            entry["restated_judgment"] = None
            for row in self.evidence_obligations:
                if row.get("oid") == oid:
                    row.setdefault("binding_history", []).append(dict(role=role, author=key, cases=list(resolved),
                                                                      after_check=len(self.checks), restated=True))
                    row.update(cases=list(resolved), runner="gate" if self._current_gate() is not None else None,
                               execution="missing", receipts=[], source_digest=None, restated=True)
            self._note(f"task {task}: {oid} restated by {key} as {', '.join(resolved)}; an independent judgment "
                       "that these cases exercise the reported behaviour is still needed")
        if problems:
            self.proof_binding_problems.extend(dict(task=task, role=role, author=key, problem=f"RUNS: {p}")
                                               for p in problems)
            self._note(f"task {task}: RUNS lines not read: {'; '.join(problems)[:200]}")
        if wanted:
            self._refresh_obligations()

    def _restated_revisions(self, entry: dict) -> Dict[str, int]:
        """The current revisions of the requirements a NOT RUN item holds."""
        revisions = self._requirement_revisions()
        return {rid: int(revisions.get(rid, 1)) for rid in sorted(entry.get("requirements") or [])}

    def _restated_current(self, entry: dict) -> bool:
        """Whether a restated item's recorded judgment, of either verdict,
        is current and independent: on exactly its current cases, by a seat
        that did not write the restatement, on the current source and under
        the current revisions (the identity ``_restated_judged`` requires,
        without its verdict). A current INSUFFICIENT is a decision already
        made, never a missing one (root review of c69fb36)."""
        judged = entry.get("restated_judgment") or {}
        last = (entry.get("restatements") or [{}])[-1]
        current = self._source_fingerprint() if self.project else None
        return bool(judged.get("verdict") in ("sufficient", "insufficient")
                    and list(judged.get("cases") or []) == list(entry.get("cases") or [])
                    and judged.get("by") and judged.get("by") != last.get("author")
                    and current and judged.get("source") == current
                    and dict(judged.get("revisions") or {}) == self._restated_revisions(entry))

    def _restated_judged(self, entry: dict) -> bool:
        """A restated item's independent judgment as it stands now:
        SUFFICIENT, on exactly its current cases, by a seat that did not write
        the restatement, made on the current source and under the current
        revisions of the requirements the item holds. A judgment of another
        source (a later edit may have changed what the cases assert: root
        review of 7a141e3, item 1) or revision is stale: the item awaits a
        fresh independent judgment, which any later reviewer or the audit
        may give on the current receipts (``_restated_awaiting``)."""
        judged = entry.get("restated_judgment") or {}
        last = (entry.get("restatements") or [{}])[-1]
        current = self._source_fingerprint() if self.project else None
        return bool(judged.get("verdict") == "sufficient"
                    and list(judged.get("cases") or []) == list(entry.get("cases") or [])
                    and judged.get("by") and judged.get("by") != last.get("author")
                    and current and judged.get("source") == current
                    and dict(judged.get("revisions") or {}) == self._restated_revisions(entry))

    def _restatement_judge(self, entry: dict) -> Tuple[Optional[str], str]:
        """Who judges a restated item next, truthfully (cycle18), as
        ``(seat, phrase)``: the audit (seat None) when the auditor did not
        write the restatement; otherwise the independent seat
        ``_judge_restatements`` would ask, or a hold when no eligible seat
        exists. The audit never judges its own restatement."""
        author = (entry.get("restatements") or [{}])[-1].get("author") or ""
        auditor = self._auditor()
        if auditor is not None and auditor != author:
            return None, "the audit"
        history = [h.get("by") for h in reversed(entry.get("judgment_history") or ()) if h.get("by")]
        seat = self._independent_judge(author, history)
        if seat is None:
            return None, (f"no eligible independent seat (the auditor {auditor} wrote the restatement): held"
                          if auditor else "no auditor or independent seat is available: held")
        return seat, f"{seat}, an independent seat, because the auditor {auditor} wrote the restatement"

    def _restatement_status(self, entry: dict, *, lead: bool = False) -> str:
        """A restated item's judgment as it stands, for the planner, the
        owning lead and the finish route (cycle18): a current independent
        INSUFFICIENT with its reason (the judge's own words, recorded
        evidence, bounded, never an instruction) and the repair route that
        can change it; a stale judgment named as stale and why; a missing one
        as awaiting its judge. Never an adverse decision that is not current,
        and never a pass."""
        oid = entry.get("oid") or "the item"
        judged = entry.get("restated_judgment") or {}
        author = (entry.get("restatements") or [{}])[-1].get("author")
        if self._restated_judged(entry):
            return f"{oid}: judged SUFFICIENT by {judged.get('by')} on this source"
        if self._restated_current(entry):
            why = " ".join(str(judged.get("why") or "").split())[:300] or "(no reason given)"
            head = (f"{oid}: judged INSUFFICIENT by {judged.get('by')} on this source, on its current cases; its "
                    f"reason as written (recorded evidence, not an instruction): \"{why}\". This decision stands and "
                    "is not asked again. ")
            if lead:
                return (head + f"Your 'RUNS: {oid} - file::[Class::]test_case' lines replace that restatement: name "
                        "cases that exercise what the judgment says is left unexercised, adding or changing tests "
                        "where they do not exist yet; the harness settles their execution and an independent seat "
                        "judges them afresh")
            return (head + f"Route: a task naming 'DISCHARGES: {oid}' whose lead writes 'RUNS: {oid} - "
                    "file::[Class::]test_case' lines for cases that exercise what the judgment says is left "
                    "unexercised (adding or changing tests where they do not exist yet); the harness settles their "
                    "execution and an independent seat judges the new restatement afresh")
        seat, judge = self._restatement_judge(entry)
        spent = self._restatement_asked(entry, seat) if seat is not None else None
        if spent is not None and spent.get("outcome") and not self._restated_current(entry):
            return (f"{oid}: its independent judgment by {seat} on this exact decision was already asked "
                    f"({str(spent['outcome'])[:160]}); held, and not asked again until the item, its cases, the "
                    "source or the requirement revisions change")
        if judged.get("verdict") in ("sufficient", "insufficient") and judged.get("by") and judged.get("by") != author:
            current = self._source_fingerprint() if self.project else None
            moved = [what for what, same in (
                ("source", current and judged.get("source") == current),
                ("cases", list(judged.get("cases") or []) == list(entry.get("cases") or [])),
                ("requirement revisions", dict(judged.get("revisions") or {}) == self._restated_revisions(entry)))
                if not same]
            return (f"{oid}: its {str(judged['verdict']).upper()} judgment by {judged['by']} is stale (another "
                    f"{', '.join(moved) or 'state'}), so it decides nothing now; it awaits a fresh judgment by {judge}")
        return f"{oid}: awaits an independent judgment by {judge}"

    def _restated_awaiting(self, task: Optional[str]) -> List[dict]:
        """Restated items a reviewer of ``task`` may judge: those this task's
        lead restated and that are not judged as they stand, and any other
        restated item whose judgment is stale or missing (cycle02 correction:
        a stale judgment is reachable, never a permanent hold)."""
        out = []
        for e in self.unexecuted_acceptance:
            if not (e.get("restatements") and e.get("oid")) or self._restated_judged(e):
                continue
            mine = (e["restatements"][-1] or {}).get("task") == task
            if mine or e.get("restated_judgment") or self._discharge_record(e) is not None:
                out.append(e)
        return out

    def _judge_restated(self, auditor: str, reply: str, task: Optional[str] = None, *,
                        oids: Optional[Sequence[str]] = None) -> None:
        """An independent seat's own '<O-id>: SUFFICIENT - <why> or INSUFFICIENT <why>'
        line on a restated NOT RUN item (cycle02): whether the restated cases
        exercise the whole reported behaviour. The restating task's reviewer
        (``task``: its review or recheck) or the audit; recorded on the item
        with the cases judged and the source. A line on an item not restated,
        or by the restatement's author, judges nothing. With ``oids``, only
        those items are read: a seat asked about a set judges exactly that
        set, and a line about any other item is not applied (cycle18)."""
        restated = ([e for e in self.unexecuted_acceptance if e.get("restatements") and e.get("oid")]
                    if task is None else self._restated_awaiting(task))
        ids = [e["oid"] for e in restated]
        restated = [e for e in restated if ids.count(e["oid"]) == 1]
        if oids is not None:
            restated = [e for e in restated if e["oid"] in set(oids)]
        if not restated:
            return
        explicit, conflicts = judgment.sufficiency_reading(reply or "", [e["oid"] for e in restated])
        for entry in restated:
            verdict, why = explicit.get(entry["oid"], (None, ""))
            if entry["oid"] in conflicts or verdict is None:
                continue
            if auditor == (entry["restatements"][-1] or {}).get("author"):
                continue
            entry["restated_judgment"] = dict(by=auditor, verdict=verdict, why=str(why)[:300],
                                              cases=list(entry.get("cases") or []),
                                              source=self._source_fingerprint() if self.project else None,
                                              revisions=self._restated_revisions(entry))
            entry.setdefault("judgment_history", []).append(dict(entry["restated_judgment"]))

    def _restated_review_block(self, spec) -> str:
        """For the restating task's reviewer: each NOT RUN item this task's
        lead restated as cases, and the one line that judges it."""
        task = getattr(spec, "task_id", None)
        items = [e for e in self._restated_awaiting(task) if e.get("restatements")]
        if not items:
            return ""
        lines = []
        for e in items:
            last = e["restatements"][-1] or {}
            lines.append(f"- {e['oid']} (reported by {e['task']}): NOT RUN: {_display_item(e['item'])[:300]}"
                         + (f" - {e['why'][:160]}" if e.get("why") else "")
                         + _replaced_note(e)
                         + (f"; restated by this task's lead as {', '.join(e.get('cases') or [])}"
                            if last.get("task") == task else
                            f"; restated by {last.get('task')}'s lead as {', '.join(e.get('cases') or [])}; its "
                            "earlier judgment no longer holds for the source as it is now"))
        return ("\n\n## NOT RUN items restated as cases that need your judgment\n" + "\n".join(lines)
                + "\nFor each, add one line '<O-id>: SUFFICIENT - <how these cases exercise the whole reported "
                "behaviour>' or '<O-id>: INSUFFICIENT <what they leave unexercised>'. Judge the cases against "
                "everything the item reported, not whether they pass: the harness settles execution itself. These "
                "lines do not change your verdict form; without a SUFFICIENT line the item stays held. "
                + _PLAIN_VERDICT_RULE)

    def _row_route_note(self, row: dict) -> str:
        """The real route for a row no run can settle as it stands, stated
        where the planner reads the rows (cycle02): a NOT RUN item naming
        nothing a check can run, and an acceptance row whose bound case is
        stale."""
        if row.get("kind") == "not_run":
            entry = next((e for e in self.unexecuted_acceptance if e.get("oid") == row.get("oid")), None)
            if entry is not None and entry.get("restatements") and self._discharging_check(entry) is None:
                # A restated item's judgment as it stands, and who judges it
                # next (cycle18): a current adverse decision carries its
                # reason and the repair route; stale or missing is said so.
                if self._discharge_record(entry) is not None or self._restated_current(entry):
                    return "; " + self._restatement_status(entry)
                return ""
            if entry is None or self._discharging_check(entry) is not None:
                return ""
            gone = self._bound_cases_gone(entry)
            if gone:
                return (f"; its bound case {', '.join(gone)} is no longer defined on the current source and the latest "
                        "complete passing run did not execute it, so no run clears it; route: 'RUNS: "
                        f"{row['oid']} - file::[Class::]test_case' lines for exact current cases, from its own task's "
                        f"lead or from a task naming 'DISCHARGES: {row['oid']}' whose lead writes them, settled "
                        "against a complete passing run on the current source and judged by an independent seat "
                        "against everything the item originally reported; nothing is matched for you")
            if entry.get("restatements"):
                return ""
            if not self._not_run_runnable(entry):
                return (f"; names nothing a check can run: no repair run clears it; route: 'RUNS: {row['oid']} - "
                        f"file::[Class::]test_case' lines from its own task's lead, or a task naming 'DISCHARGES: "
                        f"{row['oid']}' whose lead writes them, judged by an independent seat")
            return ""
        if row.get("kind") == "acceptance" and str(row.get("binding") or "").startswith("stale"):
            return ("; route: a task that declares this scenario again exactly (same operation, state, expect, "
                    "requirements and evidence kind) supersedes the row and binds the current case; the old "
                    "verdict never transfers")
        return ""

    def _discharges_problem(self, tokens) -> str:
        """Why a DISCHARGES line cannot be taken, or "" (also for no line).
        Each id must be a standing NOT RUN item (cycle02)."""
        if tokens is None:
            return ""
        if not tokens:
            return "DISCHARGES names no item; name the standing NOT RUN items by id (O<n>)."
        standing = {e.get("oid") for e in self._standing_not_run() if e.get("oid")}
        unknown = [t for t in tokens if t not in standing]
        if unknown:
            return (f"DISCHARGES names {', '.join(unknown)}, which are not standing NOT RUN items"
                    + (f" (standing: {', '.join(sorted(standing))})" if standing else " (none is standing)") + ".")
        return ""

    def _take_over_not_run(self, spec, oids) -> None:
        """Record that ``spec`` takes over these NOT RUN items: kept on the
        item beside its original task, reason and requirements, never in
        place of them."""
        for entry in self.unexecuted_acceptance:
            if entry.get("oid") in oids:
                entry.setdefault("taken_over_by", []).append(dict(task=spec.task_id, after_check=len(self.checks)))
                self._note(f"{spec.task_id} takes over {entry['oid']} ({entry['task']}'s NOT RUN item)")

    def _discharges_block(self, spec) -> str:
        """For the lead of a task that took over NOT RUN items: each item as
        reported, and the one way to restate it."""
        task = getattr(spec, "task_id", None)
        items = [e for e in self.unexecuted_acceptance
                 if any(t.get("task") == task for t in e.get("taken_over_by") or ())]
        if not items:
            return ""
        lines = [f"- {e['oid']} (reported by {e['task']}, holds {', '.join(e.get('requirements') or [])}): "
                 f"NOT RUN: {_display_item(e['item'])[:400]}" + (f" - {e['why'][:200]}" if e.get("why") else "")
                 # A current independent INSUFFICIENT on the restatement this
                 # task replaces: what the judge said is left unexercised,
                 # as evidence for the new cases (cycle18).
                 + (f"\n  current restatement as {', '.join(e.get('cases') or [])}: "
                    + self._restatement_status(e, lead=True)
                    if e.get("restatements") and self._restated_current(e) and not self._restated_judged(e) else "")
                 for e in items]
        return ("## NOT RUN items this task takes over\n" + "\n".join(lines)
                + "\nFor each, once the cases that exercise the whole reported behaviour exist in the project's "
                "tests, write one line 'RUNS: O<n> - file::[Class::]test_case' per case in your reply. The harness "
                "settles them only from a complete passing run on the current source, and the independent audit "
                "judges whether the cases exercise everything the item reported; a case chosen because it "
                "passes, or that exercises only part of it, leaves the item held.")

    def _discharging_check(self, entry: dict) -> Optional[int]:
        """The index of the first later check that discharges a NOT RUN or
        skip record (the rule _standing_not_run applies), or None: the
        index of ``_discharge_record``."""
        record = self._discharge_record(entry)
        if record is not None and entry.get("restatements") and not self._restated_judged(entry):
            # Executed is not shown meaningful: a restated item stands until
            # an independent seat judges the cases sufficient (cycle02).
            return None
        return None if record is None else record["index"]

    def _refresh_obligations(self) -> None:
        """Settle every typed obligation against the record as it stands:
        a receipt that matches discharges execution (delivery.discharge,
        every identity checked); an execution on another source or a
        superseded requirement revision is stale (delivery.refresh). Nothing
        here judges sufficiency."""
        self._mark_not_run_display()
        if not self.evidence_obligations:
            return
        self._mark_stale_bindings()
        current_source = self._source_fingerprint() if self.project else None
        revisions = self._requirement_revisions()
        refreshed = []
        environment = self._environment_id()
        for record in self.evidence_obligations:
            if record.get("modality") in ("reading", "legacy"):
                # What a reading claim or a legacy string is about is the
                # current source under the effective gate and environment:
                # its binding (and so any judgment's currency) moves with
                # them (root's currency control on a1b1840).
                record = dict(record, source_digest=current_source, gate=self._obligation_current_gate(record),
                              environment=environment)
            if record.get("modality") == "capture":
                # Measured or not: the task's verified design evidence on
                # exactly this source, in the declared state, delivered to
                # its reviewer (execution only; sufficiency is the design
                # reviewer's explicit line). Any drift is not measured.
                measured, why = self._capture_measurement(record)
                if measured is not None:
                    record = dict(record, execution="ran", source_digest=current_source, receipts=[measured],
                                  gate=self._obligation_current_gate(record), environment=environment, reason="")
                else:
                    record = dict(record, execution="missing", source_digest=None, receipts=[],
                                  gate=self._obligation_current_gate(record), environment=environment, reason=why)
                refreshed.append(dict(record, **delivery.EvidenceObligation.from_dict(record).to_dict()))
                continue
            if record.get("sufficiency") == "sufficient" and not self._scenario_shown(record):
                # An approval of a row whose whole scenario can no longer be
                # shown is stale: nothing shown, nothing approved.
                record = dict(record, sufficiency="unjudged", judged_by="", judged_binding="",
                              sufficiency_note=f"{record['oid']}: approval dropped; the scenario is not shown whole")
            if (record.get("kind") == "acceptance" and record.get("discharged_cases") is not None
                    and list(record.get("discharged_cases") or []) != list(record.get("cases") or [])):
                # The scenario was rebound to other cases since it was
                # discharged: the old execution and any judgment of it are
                # about evidence that no longer binds (root's stale-binding
                # control); nothing is kept from them.
                record = dict(record, execution="missing", sufficiency="unjudged", judged_by="", judged_binding="",
                              receipts=[], source_digest=None, reason="rebound to other cases",
                              discharged_cases=None)
            obligation = delivery.EvidenceObligation.from_dict(record)
            current_gate = self._obligation_current_gate(record)
            obligation = delivery.refresh(obligation, current_source=current_source, current_requirements=revisions,
                                          current_gate=current_gate, current_environment=environment)
            receipt = self._obligation_receipt(record) if obligation.runner is not None else None
            if (receipt is None and obligation.runner is None and record.get("kind") == "not_run"
                    and (record.get("modality") or "test") == "test"):
                # A NOT RUN row registered when the gate did not yet name what
                # it reports gets no runner, while its item can still be
                # discharged later by the harness's own complete passing run
                # naming every path whole (cycle13 O59/O63/O64: the items read
                # discharged, the rows read missing, and the rows lowered
                # R14-R17). The row asks the same decision the item uses
                # (``_obligation_receipt`` over ``_discharge_record``,
                # re-derived from the recorded checks, never a stored marker);
                # only a decision on the row's own gate, environment and
                # requirement revisions gives it the gate as runner, and
                # ``delivery.discharge`` then checks every identity as for
                # any other row.
                decided = self._obligation_receipt(record)
                if (decided is not None and decided.get("runner") == "gate" and decided.get("passed")
                        and (decided.get("gate") or "") == (current_gate or "")
                        and (decided.get("environment") or "") == environment
                        and (decided.get("gate") or "") == (obligation.gate or "")
                        and all(int(revisions.get(rid, 1)) == int(rev) for rid, rev in obligation.requirements)):
                    obligation = replace(obligation, runner="gate")
                    record = dict(record, runner_basis=(f"the harness's own run {decided['run']} in {decided['id']} "
                                                        "executed what this report names, after it was registered"))
                    receipt = decided
            # A receipt from a check under a gate or in an environment that
            # is no longer the effective one discharges nothing now, even
            # though it matched the obligation when it ran.
            if receipt is not None and ((receipt.get("gate") or "") != (current_gate or "")
                                        or (receipt.get("environment") or "") != environment):
                receipt = None
            if receipt is not None and current_source and (obligation.execution != "ran"
                                                            or obligation.source_digest != current_source):
                out = delivery.discharge(obligation, receipt, current_source=current_source,
                                         current_requirements=revisions)
                obligation = out.obligation
                if out.accepted and record.get("kind") == "acceptance":
                    record = dict(record, discharged_cases=list(obligation.cases))
            refreshed.append(dict(record, **obligation.to_dict()))
        self.evidence_obligations = refreshed

    def _judge_obligations(self, auditor: str, reply: str, found: dict, *, ids=None, modalities=None,
                           oids=None) -> None:
        """Sufficiency from the audit: only an explicit, scoped
        '<O-id>: SUFFICIENT|INSUFFICIENT' line in the judging seat's own
        reply decides, and the record names it (``sufficiency_source``). No
        verdict on a requirement implies anything about a row it binds:
        finding-lifecycle-02 showed the harness's lowering of R9 cascading
        through implied sufficiency into R3, R4 and R11, and root's review
        of bd0164f showed the same family on first judgment (R1 MET plus R5
        NOT MET on a shared row labelled it insufficient; all MET labelled an
        unjudged row sufficient). Fresh or stale executed evidence stays
        unjudged until a seat judges it in a reply it was already asked for;
        no call is added. A judgment already made about exactly this
        evidence stands. Two lines for one id (a duplicate or a
        contradiction) are no verdict: the obligation is left unjudged with
        the conflict noted. Only obligations binding an audited requirement
        (``ids``; every one when None) are judged. Bound to exactly the
        evidence judged. ``found`` is unused and kept for the callers'
        shape."""
        explicit, conflicts = judgment.sufficiency_reading(reply, [o["oid"] for o in self.evidence_obligations])
        refreshed = []
        for record in self.evidence_obligations:
            obligation = delivery.EvidenceObligation.from_dict(record)
            if ids is not None and not any(r[0] in ids for r in obligation.requirements):
                refreshed.append(record)
                continue
            if modalities is not None and record.get("modality", "test") not in modalities:
                refreshed.append(record)
                continue
            if oids is not None and record.get("oid") not in oids:
                # A line naming a row this seat was not shown (another
                # task's, or one not delivered to it) judges nothing and
                # overwrites nothing (root on 4fac862).
                refreshed.append(record)
                continue
            purpose = delivery.purpose_of(record)
            if purpose != "acceptance":
                # Execution debt is settled by execution, never judged, and
                # an unknown-purpose row is held and never credited: a line
                # a seat writes about either is kept as a note and applied
                # to nothing, so it can neither lower nor credit the
                # requirements the row holds (quality/completion batch,
                # 2026-10-31; P12 lowered six METs this way).
                why_not = ("execution debt is settled by execution, not judged" if purpose == "execution"
                           else "a row of unknown purpose is held and never credited")
                said, _why = explicit.get(obligation.oid, (None, ""))
                if obligation.oid in conflicts:
                    refreshed.append(dict(record, sufficiency_note=(
                        f"{conflicts[obligation.oid]}; not applied ({auditor}): {why_not}")))
                elif said is not None:
                    refreshed.append(dict(record, sufficiency_note=(
                        f"{obligation.oid}: {said.upper()} by {auditor} not applied: {why_not}")))
                else:
                    refreshed.append(record)
                continue
            if obligation.oid in conflicts:
                obligation = replace(obligation, sufficiency="unjudged", judged_by="", judged_binding="")
                refreshed.append(dict(record, **obligation.to_dict(), sufficiency_note=conflicts[obligation.oid],
                                      sufficiency_source=""))
                continue
            verdict, why = explicit.get(obligation.oid, (None, ""))
            source = f"explicit line by {auditor}" if verdict is not None else ""
            reading = record.get("modality") == "reading"
            legacy = record.get("modality") == "legacy"
            capture = record.get("modality") == "capture"
            extra = {}
            if capture and verdict == "sufficient" and modalities is None:
                # A capture row is judged sufficient only by the design
                # reviewer that was shown the renders; the requirements
                # audit may reject it, never approve it.
                verdict = None
                extra = dict(sufficiency_note=f"{obligation.oid}: SUFFICIENT by {auditor} not counted; a capture "
                                              "row is judged by the design reviewer")
            # A reading claim or a legacy string (mapping unknown) is judged
            # only by its own explicit line: a verdict on its requirement
            # implies nothing about it (root's controls on d515d51 and
            # a1b1840: never silently sufficient, and the planner's choice
            # of modality or file authorizes no exemption). A reading
            # SUFFICIENT must cite the file the scenario names on the
            # current source; one that does not is not a judgment of it.
            if verdict is None:
                # No scoped line for this row: nothing changes. A judgment
                # already made about exactly this evidence stands
                # (``sufficiency_current``), a fresh or stale row stays
                # unjudged, and a verdict on a requirement the row binds
                # judges nothing about it (root's controls on 2ab56eb and
                # bd0164f). An explicit line above still decides.
                refreshed.append(record)
                continue
            if reading and verdict == "sufficient":
                location = str(record.get("location") or "")
                if location not in self._resolve_citations(why):
                    verdict = None
                    extra = dict(sufficiency_note=f"{obligation.oid}: SUFFICIENT did not cite {location}")
                    self._note(f"{obligation.oid}: SUFFICIENT by {auditor} did not cite {location}; not counted")
            if verdict == "sufficient" and obligation.execution != "ran" and not (reading or legacy):
                verdict = None
            if verdict == "sufficient" and not self._scenario_shown(record):
                # The seat could not be shown the whole scenario it would
                # be judging sufficient: neither its explicit line nor a
                # verdict on the requirement counts (an INSUFFICIENT still
                # does).
                verdict = None
                extra = dict(sufficiency_note=f"{obligation.oid}: scenario not shown whole to {auditor}; "
                                              "SUFFICIENT not counted")
                self._note(f"{obligation.oid}: SUFFICIENT by {auditor} not counted; the scenario was not shown whole")
            if verdict is not None:
                obligation = delivery.judge_sufficiency(obligation, verdict, judged_by=auditor,
                                                        reading=reading or legacy)
                extra = dict(extra, sufficiency_source=source)
                if reading or legacy:
                    extra = dict(extra, judged_source=self._source_fingerprint() if self.project else None)
            refreshed.append(dict(record, **obligation.to_dict(), **extra))
        self.evidence_obligations = refreshed

    def _supersede_obligation(self, record: dict) -> None:
        """A later task that declares the exact same scenario (operation,
        prior state, expectation, location, evidence and, for a capture,
        the declared visual state) on the same requirement revisions and
        modality takes that proof over from an earlier task's row, run or
        not (an earlier measurement was of an earlier source; the successor
        must establish fresh proof and sufficiency of its own). The earlier
        row is marked ``superseded_by`` and kept on the record, immutable,
        and no longer holds the requirement: a capped repair continued by a
        later repair, or a repair redone by a later one, moves the debt to
        the declaration that now owns it and never erases it. A scenario
        that differs in any field, another requirement set or revision,
        another modality, a legacy row, a NOT RUN or skip fact (no
        scenario), predecessor cap debt and open findings are untouched
        (root, SAME Astra and SAME Sol on 4fac862)."""
        scenario = [list(s) for s in record.get("scenario") or []]
        if not scenario:
            return
        wanted = [list(r) for r in record.get("requirements") or ()]
        for earlier in self.evidence_obligations:
            if (earlier is record or earlier.get("task") == record.get("task") or earlier.get("kind") != "acceptance"
                    or earlier.get("superseded_by")):
                continue
            same = [list(s) for s in earlier.get("scenario") or []]
            if (same and [s for s in same if s[0] != "proof"] == [s for s in scenario if s[0] != "proof"]
                    and [list(r) for r in earlier.get("requirements") or ()] == wanted
                    and earlier.get("modality") == record.get("modality")):
                earlier["pre_supersession"] = dict(reason=earlier.get("reason", ""))
                earlier["superseded_by"] = record.get("task")
                earlier["reason"] = f"superseded by task {record.get('task')}'s declaration of the same scenario"
                self._note(f"{earlier['oid']} ({earlier.get('task')}): superseded by {record.get('task')}'s "
                           f"declaration of the same scenario")

    def _rebind_capture_rows(self, spec, record: dict, why: str) -> List[str]:
        """After the one accepted redeclaration (``_recapture_declared``):
        this task's capture rows now claim the effective declared state,
        not the dispatched one, before the fresh independent design review
        is asked. The dispatched declaration, its problem and whatever was
        measured or judged under it are kept on the row as lineage
        (``capture_lineage``, never rewritten; ``capture_declared`` is the
        first declaration), and nothing judged or measured under the old
        state carries over: execution, receipts and sufficiency start
        again, so a reviewer shown the dispatched state is never relabelled
        as having judged the redeclared one (``_design_context_drift``
        compares the shown row to the row as it stands). Rows of other
        tasks, other modalities and superseded rows are untouched. Returns
        the rebound row ids (root's recapture identity gap on c9a665b)."""
        effective = self._capture_context(spec.scope)
        rebound = []
        for row in self.evidence_obligations:
            if (row.get("task") != spec.task_id or row.get("modality") != "capture" or row.get("superseded_by")
                    or row.get("kind") != "acceptance"):
                continue
            scenario = [list(s) for s in row.get("scenario") or []]
            before = dict(scenario).get("capture")
            if before == effective:
                continue
            lineage = list(row.get("capture_lineage") or [])
            lineage.append(dict(capture=before, why=str(why)[:300], execution=row.get("execution"),
                                sufficiency=row.get("sufficiency"), judged_by=row.get("judged_by", ""),
                                receipts=list(row.get("receipts") or [])))
            row.update(scenario=[[k, effective] if k == "capture" else [k, v] for k, v in scenario],
                       capture_lineage=lineage, capture_declared=row.get("capture_declared", before),
                       execution="missing", sufficiency="unjudged", judged_by="", judged_binding="", receipts=[],
                       source_digest=None, reason="capture redeclared; measured and judged afresh")
            self._sync_obligation(row)
            self._reconcile_supersession(row, [s for s in scenario if s[0] != "proof"])
            rebound.append(row["oid"])
        if rebound:
            record["rebound_rows"] = list(rebound)
            self._note(f"task {spec.task_id}: capture rows {', '.join(rebound)} rebound to the redeclared state")
        return rebound

    def _reconcile_supersession(self, row: dict, previous: list) -> None:
        """Supersession follows the effective scenario. A capture row this
        task's dispatched declaration superseded (``previous``: the same
        state and requirements, now redeclared away from) is lifted:
        ``superseded_by`` is removed and its reason restored, and its own
        execution and judgment, insufficient or otherwise, stay exactly as
        they were; a row of an earlier task that declares the effective
        state is superseded now, by the ordinary rule. Nothing else on the
        record moves: a reading or test row this task superseded, or a
        capture row superseded for another state, stays as it is (root on
        c9a665b: t1 superseded by t2's A must not stay superseded by t2's
        B)."""
        scenario = [s for s in ([list(x) for x in row.get("scenario") or []]) if s[0] != "proof"]
        wanted = [list(r) for r in row.get("requirements") or ()]
        for earlier in self.evidence_obligations:
            if (earlier is row or earlier.get("superseded_by") != row.get("task")
                    or earlier.get("modality") != row.get("modality")):
                continue
            same = [s for s in ([list(x) for x in earlier.get("scenario") or []]) if s[0] != "proof"]
            if same != previous or [list(r) for r in earlier.get("requirements") or ()] != wanted:
                continue
            if same == scenario:
                continue
            earlier.pop("superseded_by", None)
            earlier["reason"] = (earlier.pop("pre_supersession", None) or {}).get("reason", "")
            earlier["supersession_lifted"] = (f"task {row.get('task')} redeclared its capture to another state; "
                                              f"{earlier['oid']} holds its own proof again")
            self._note(f"{earlier['oid']} ({earlier.get('task')}): no longer superseded; {row.get('task')} "
                       "redeclared its capture to another state")
        self._supersede_obligation(row)

    def _register_acceptance_obligations(self, spec, covers) -> None:
        """At dispatch: each acceptance behavior the task's scope declares,
        bound to its covers, with the gate as runner when the task has one."""
        scope = getattr(spec, "scope", None)
        proofs = getattr(scope, "proofs", None)
        if proofs:
            for proof in proofs:
                modality = proof.get("evidence", "test")
                scenario = dict(proof=proof["id"], operation=proof["operation"], state=proof["state"],
                                expect=proof["expect"], location=proof["location"], evidence=modality)
                if modality == "capture":
                    # The declared visual state is part of the claim: route,
                    # steps and fixture (SCOPE capture, normalized) or the
                    # lead's own capture when none is declared. Two scenarios
                    # that read the same but show different states are
                    # different proofs (root on 4fac862).
                    scenario["capture"] = self._capture_context(scope)
                record = self._register_obligation(
                    dict(task=spec.task_id, requirements=[r for r in proof["requirements"] if r in (covers or [])],
                         item=f"{proof['id']}: {proof['operation']}; given {proof['state']}; expect {proof['expect']}; "
                              f"in {proof['location']}", after_check=len(self.checks),
                         after_observed=len(self.review_checks), modality=modality,
                         location=proof["location"], scenario=scenario),
                    kind="acceptance")
                if record is not None:
                    # The modality is what the scenario declared and the
                    # dispatch admitted (``_proof_problem``), never inferred
                    # from the file name: a test scenario is an executable
                    # claim that only its named executed case discharges and
                    # that, unexecuted, lowers its requirement; a reading
                    # scenario (documentation) names no runnable proof, is
                    # never discharged by a check, and counts only on the
                    # audit's own judgment of it citing the file on the
                    # current source (``_judge_obligations``).
                    record.update(proof_id=proof["id"], location=proof["location"], modality=modality,
                                  executable_claim=modality == "test",
                                  binding="unbound" if modality == "test" else (
                                      "reading claim (documentation): accepted only on the audit's explicit "
                                      "per-obligation judgment citing the file on the current source"
                                      if modality == "reading" else
                                      "capture claim (visual): measured only by the harness's verified render of "
                                      "the declared state on the current source; sufficient only by the design "
                                      "reviewer's explicit per-obligation judgment"),
                                  runner=record.get("runner") if modality == "test" else None)
                    self._supersede_obligation(record)
            return
        for item in (getattr(scope, "acceptance", None) or ()):
            record = self._register_obligation(dict(task=spec.task_id, item=str(item), requirements=list(covers or []),
                                                    after_check=len(self.checks),
                                                    after_observed=len(self.review_checks)),
                                               kind="acceptance")
            if record is not None and "binding" not in record:
                # A legacy acceptance string names no scenario and no case:
                # its mapping is unknown and stays so on the record. No
                # check discharges it (no case can bind to it: PROVES binds
                # only declared scenarios) and no verdict on its requirement
                # is read as its sufficiency. It counts only when the audit
                # judges it by an explicit per-id line about the current
                # source, and unjudged it keeps its requirement unmet. It
                # claims no execution, so it is not lowered as unexecuted.
                record.update(binding="unknown (legacy acceptance): no scenario declared, no case bound; judged "
                                      "only by an explicit O-line from the audit",
                              modality="legacy", executable_claim=False, legacy=True, runner=None)
                self._sync_obligation(record)

    def _deterministic_blockers(self) -> Dict[str, str]:
        """Requirements the record already holds not met on the current
        source, by facts ``_lower_unexecuted`` applies to an audit's MET
        afterwards and that no audit, final challenge or cap step can
        change: a standing NOT RUN item that holds it, or an acceptance
        obligation of it whose binding is stale, that has no authorized
        runner, or whose execution failed (reading and legacy rows, and a
        bound row merely not executed yet, are the audit's to settle).
        Empty when the state is not known: no ledger, no project, or no
        check on the current source (cycle02)."""
        if not self.config.requirements_ledger or not self.project or not self.checks:
            return {}
        current = self._source_fingerprint()
        if not current or self.checks[-1].get("source") != current:
            return {}
        self._refresh_obligations()
        # A restated item already executed on this source awaits only the
        # audit's judgment: evidence that can still be settled, not a blocker.
        standing = [e for e in self._standing_not_run()
                    if not (e.get("restatements") and self._discharge_record(e) is not None)]
        out: Dict[str, str] = {}
        # Whether settling them needs a code or test change: an item no check
        # can run, or a stale or unbound row, is settled by a declaration
        # (RUNS, DISCHARGES, an exact redeclaration) when its cases exist; a
        # bound item not shown passing, or a failed execution, needs an edit.
        self._blockers_need_editing = False
        for rid in self.memory.ledger.requirements:
            parts = []
            held = [e for e in standing if rid in (e.get("requirements") or ())]
            # A bound case gone from the current source is settled by a
            # declaration of exact current cases, not by an edit (cycle22).
            if any(self._not_run_runnable(e) for e in held):
                self._blockers_need_editing = True
            if held:
                parts.append("NOT RUN: " + "; ".join(f"{e['task']}: {_display_item(e['item'])[:100]}" for e in held))
            # Only rows no audit, challenge or cap step can settle: a stale
            # binding, no authorized runner, or a failed execution. A bound
            # row merely not executed yet is left to the audit and the final
            # challenge, which may run it.
            unmet = [o for o in self.evidence_obligations
                     if any(r[0] == rid for r in o.get("requirements") or ()) and o.get("execution") != "ran"
                     and o.get("modality") not in ("reading", "legacy") and not o.get("superseded_by")
                     and o.get("kind") != "not_run"
                     and (str(o.get("binding") or "").startswith("stale") or o.get("runner") is None
                          or o.get("execution") == "failed")]
            if any(o.get("execution") == "failed" for o in unmet):
                self._blockers_need_editing = True
            if unmet:
                parts.append("not executed: " + "; ".join(
                    f"{o['oid']} ({'no runner' if o.get('runner') is None else o.get('execution')}"
                    + (f": {o['reason']}" if o.get("reason") else "") + ")" for o in unmet))
            if parts:
                out[rid] = "; ".join(parts)[:400]
        return out

    def _obligation_view(self, *, ids=None) -> str:
        """The same recorded view for the planner and the audit (an explicit
        run's audit lists only its audited requirements). The last one shown
        is kept for the run record (result.json "obligations")."""
        if not (self.config.requirements_ledger and self.memory.ledger.requirements):
            return ""
        view = self._obligations(ids)
        self.obligation_view = view.to_dict()
        return "\n\n" + obligations.render(view)
