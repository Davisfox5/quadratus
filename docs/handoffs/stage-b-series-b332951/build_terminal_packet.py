"""Terminal packet for the Stage B series: read-only over the manifest and every cell's records.
v2 (2026-10-03): Codex readback corrections applied; raw decision fields and model identities included."""
import hashlib, json, re, sys
from datetime import datetime
from pathlib import Path
out = Path(sys.argv[1]); m = json.load(open(out / "cells" / "manifest.json"))
graders_dir = Path(m["packet"]["graders"]["dir"])
frozen_digest = hashlib.sha256(json.dumps(m["frozen"], sort_keys=True).encode()).hexdigest()
grader_now = {str(p.relative_to(graders_dir)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(graders_dir.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}
graders_unchanged = grader_now == m["frozen"]["graders"]
FMT = "%Y-%m-%dT%H:%M:%SZ"

def money(report):
    mm = re.search(r"\*\*Total: \$([0-9.]+)\*\*", report); return float(mm.group(1)) if mm else None

cells = []
for c in m["cells"]:
    row = dict(name=c["name"], task=c["task"], arm=c["arm"], state=c["state"], started_at=c.get("started_at"), finished_at=c.get("finished_at"),
               seconds=c.get("seconds"), engine_sha=c.get("engine_sha"), error=c.get("error"), integrity=c.get("integrity"),
               grade_withheld=c.get("grade_withheld"), grade_unverified=bool(c.get("grade_unverified")), project=c.get("project"), run_dir=c.get("run_dir"))
    g = c.get("grade")
    row["grade"] = None if not g else dict(passed=g["passed"], total=g["total"], requirements={r["argv"][-1]: ("pass" if r["passed"] else "fail") for r in g["results"]},
                                           output_tails={r["argv"][-1]: r["output_tail"][-600:] for r in g["results"] if not r["passed"]})
    if c.get("run_dir") and (Path(c["run_dir"]) / "result.json").exists():
        rd = Path(c["run_dir"]); r = json.load(open(rd / "result.json")); b = r.get("budget") or {}
        row["engine"] = dict(run_id=rd.name, completed=r.get("completed"), error=r.get("error"), stop_reason=b.get("stop_reason"),
                             attempts=b.get("reserved_attempts"), reported_tokens=b.get("reported_tokens"), input_tokens=b.get("input_tokens"),
                             output_tokens=b.get("output_tokens"), unknown_usage_attempts=b.get("unknown_usage_attempts"),
                             budget_api_cost_usd=b.get("api_cost_usd"), budget_cost_boundary=b.get("cost_boundary"),
                             tasks=r.get("tasks"), failed_tasks=r.get("failed_tasks"), turn_limited_tasks=r.get("turn_limited_tasks"),
                             checks=[bool(ch.get("passed")) for ch in r.get("checks") or []], design_checks=r.get("design_checks") or [],
                             requirements_status=(r.get("requirements") or {}).get("status"), audits=(r.get("requirements") or {}).get("audits"),
                             survey={k: (r.get("survey") or {}).get(k) for k in ("unique_causes", "recovered", "open", "repeats", "allowance_spent")},
                             hypotheses=(r.get("survey") or {}).get("hypotheses") or [],
                             api_price_counterfactual_usd=money((rd / "report.md").read_text()) if (rd / "report.md").exists() else None)
        decs = []
        for d in r.get("decisions") or []:
            probs = d.get("probabilities")
            if isinstance(probs, str) and probs.startswith("{"):
                probs = json.loads(probs)
            decs.append(dict(task=d.get("task"), decision=d.get("decision"), answer=d.get("answer"), default=d.get("default"),
                             labels_version=d.get("labels"), source=d.get("source"), confidence=d.get("confidence"),
                             selected_label_probability=(probs or {}).get(d.get("answer")), probabilities=probs,
                             model=(d.get("usage") or {}).get("model"), host=(d.get("usage") or {}).get("host"), seconds=(d.get("usage") or {}).get("seconds"),
                             input_tokens=(d.get("usage") or {}).get("input_tokens"), output_tokens=(d.get("usage") or {}).get("output_tokens"), error=d.get("error")))
        row["jev_decisions"] = decs
        calls = []
        for line in (rd / "invocations.jsonl").read_text().splitlines():
            e = json.loads(line)
            calls.append(dict(task=e.get("task"), role=e.get("role"), requested_model=e.get("requested_model"), canonical_model=e.get("canonical_model"),
                              resolved_model=e.get("resolved_model"), wire_model=e.get("wire_model"), selected=e.get("selected"), invoked=e.get("invoked", True),
                              origin=e.get("origin"), outcome=e.get("outcome"), provider_outcome=e.get("provider_outcome"), attempt=e.get("attempt"),
                              input_tokens=e.get("input_tokens"), cached_input_tokens=e.get("cached_input_tokens"), output_tokens=e.get("output_tokens"),
                              seconds=round(e.get("seconds") or 0, 1), model_turns=e.get("model_turns"), turn_limited=e.get("turn_limited"),
                              tool_failures=(e.get("tool_failures") or "")[:300] if isinstance(e.get("tool_failures"), str) else e.get("tool_failures")))
        row["calls"] = calls
        denials = []
        if (rd / "trace.jsonl").exists():
            for line in (rd / "trace.jsonl").read_text().splitlines():
                e = json.loads(line)
                if e.get("vendor") == "claude" and e.get("role") in ("lead", "revision"):
                    tools = e.get("tool_calls") if isinstance(e.get("tool_calls"), list) else []
                    denied = [t for t in tools if t.get("name") in ("Edit", "Write") and t.get("outcome") == "denied"]
                    denials.append(dict(task=e.get("task"), role=e.get("role"), invocation=e.get("invocation_id"), session=e.get("session_id"), model=e.get("model"),
                                        denied_edits=len(denied), files_written=len(e.get("files_written") or []),
                                        denied_paths=sorted({(t.get("path") or "").replace(str(c["project"]) + "/", "") for t in denied}),
                                        commands_errored=sum(1 for x in (e.get("commands") or []) if x.get("outcome") == "error"), commands=len(e.get("commands") or [])))
        row["claude_lead_calls"] = denials
    cells.append(row)

ran = [c for c in cells if c["state"] == "ran"]
invoked = [k for c in ran for k in c["calls"] if k["invoked"]]
totals = dict(cells=len(cells), executed=len(ran), failed=sum(c["state"] == "failed" for c in cells),
              interrupted=sum(c["state"] == "interrupted" for c in cells), integrity_failed=sum(c["state"] == "integrity-failed" for c in cells),
              unlaunched=sum(c["state"] == "prepared" for c in cells),
              reported_tokens=sum(c["engine"]["reported_tokens"] or 0 for c in ran), attempts=sum(c["engine"]["attempts"] or 0 for c in ran),
              summed_cell_seconds=round(sum(c["seconds"] or 0 for c in ran), 1),
              wall_span=str(datetime.strptime(m["stopped"]["at"], FMT) - datetime.strptime(m["launches"][0]["at"], FMT)),
              api_price_counterfactual_usd=round(sum(c["engine"]["api_price_counterfactual_usd"] or 0 for c in ran), 2),
              budget_api_cost_usd_sum=round(sum(c["engine"]["budget_api_cost_usd"] or 0 for c in ran), 4),
              jev_decisions=sum(len(c["jev_decisions"]) for c in ran),
              jev_decision_tokens=sum((d["input_tokens"] or 0) + (d["output_tokens"] or 0) for c in ran for d in c["jev_decisions"]),
              jev_input_tokens=sum(d["input_tokens"] or 0 for c in ran for d in c["jev_decisions"]))
totals["jev_estimated_usd_at_0_042_per_M_input"] = round(totals["jev_input_tokens"] * 0.042 / 1_000_000, 6)
import collections
kinds, diffs = collections.Counter(), collections.Counter()
for c in ran:
    for d in c["jev_decisions"]:
        (kinds if d["decision"] == "task.kind" else diffs)[d["answer"]] += 1
totals["jev_kind_answers"], totals["jev_difficulty_answers"] = dict(kinds), dict(diffs)
by_model = {}
for k in invoked:
    t = by_model.setdefault(k["canonical_model"], dict(calls=0, input_tokens=0, output_tokens=0, seconds=0.0, resolved=set()))
    t["calls"] += 1; t["input_tokens"] += k["input_tokens"] or 0; t["output_tokens"] += k["output_tokens"] or 0; t["seconds"] = round(t["seconds"] + k["seconds"], 1)
    if k.get("resolved_model"): t["resolved"].add(k["resolved_model"])
for t in by_model.values(): t["resolved"] = sorted(t["resolved"])

pairs = {}
for c in cells: pairs.setdefault(c["task"], {})[c["arm"]] = c
comparability = []
for task, arms in pairs.items():
    j, r = arms.get("jev"), arms.get("rule")
    opus = [(a, k["task"], k["denied_edits"]) for a, cell in (("jev", j), ("rule", r)) for k in (cell.get("claude_lead_calls") or []) if k["denied_edits"]]
    reasons = []
    if any(cell["state"] != "ran" for cell in (j, r)): reasons.append("not both executed")
    if opus: reasons.append("an assigned lead (claude:opus) was denied every project Edit/Write: " + ", ".join(f"{a} {t} ({n} denials)" for a, t, n in opus))
    if any(cell.get("grade_withheld") for cell in (j, r)): reasons.append("a grade was withheld by the hold's integrity check")
    note = ""
    if task == "f2-project-search":
        note = ("no Opus-lead confound found in this narrow review; not certified unaffected: same lead seat (grok:default), tied 4/5, "
                "differing planner input (decider_labels=all omits the KIND instruction) and decomposition, one repeat")
    comparability.append(dict(task=task, comparable_as_jev_vs_rule_evidence=not reasons, reasons=reasons, note=note,
                              grade=dict(jev=(j.get("grade") or {}).get("passed"), rule=(r.get("grade") or {}).get("passed"), total=(j.get("grade") or r.get("grade") or {}).get("total")),
                              tasks_closed=dict(jev=(j.get("engine") or {}).get("tasks"), rule=(r.get("engine") or {}).get("tasks"))))

packet = dict(
    version=2, series=str(out), packet_digest=m["packet_digest"], frozen_inputs_digest=frozen_digest, frozen=m["frozen"],
    graders_unchanged_at_packet_time=graders_unchanged, engine_sha=m["packet"]["engine_sha"], engine=m.get("engine"),
    launches=m.get("launches"), prepared_at=m.get("prepared_at"), stopped=m.get("stopped"), overrides=m.get("overrides", []),
    hold=json.load(open(out / "hold.json")) if (out / "hold.json").exists() else None,
    hold_correction=json.load(open(out / "hold.correction.json")) if (out / "hold.correction.json").exists() else None,
    bounds=dict(limits=m["packet"]["limits"], max_tasks=m["packet"]["max_tasks"], survey_recovery=m["packet"]["survey_recovery"], repeats=m["packet"]["repeats"], jev_model=m["packet"]["jev_model"]),
    gametape_base=m["packet"]["project"]["base_sha"], totals=totals, usage_by_model=by_model, comparability=comparability, cells=cells,
    preflight_liveness_ledger=json.load(open(out / "preflight-liveness-ledger.json")) if (out / "preflight-liveness-ledger.json").exists() else None,
    costs=dict(
        subscription="every vendor CLI call ran on Claude Max, ChatGPT and SuperGrok subscriptions; no per-call invoice exists",
        counterfactual=f"${totals['api_price_counterfactual_usd']} is the engine's API list-price counterfactual for the executed cells; it is not a charge",
        budget_api_cost="the run budget's api_cost_usd is 0.0 on every cell because the CLI transport is excluded from that boundary (cost_boundary field); it is not evidence of zero cost",
        jev=f"about ${totals['jev_estimated_usd_at_0_042_per_M_input']} estimated at the published $0.042/M input rate on {totals['jev_input_tokens']:,} input tokens; invoice unverified",
        verified_invoice="none"),
    corrections_v2=[
        "Jev decision counts: 14 = 7 kind + 7 difficulty; kind backend 3, frontend 3, test 1; difficulty standard 4, simple 3 (the #35 terminal comment said backend 4 and simple 4).",
        "Confidence and selected-label probability are distinct fields and both are reported per decision; the lowest difficulty confidence is f5 t1 simple at 0.34 (probability 0.51), not 0.54.",
        "9,095.5 s is the sum of cell durations; the series' wall span from launch to stop is 1:26:17.",
        "hold.json's 'mechanism' promised a post-hoc grade for the withheld f5 jev cell; that grade was not run (preservation, Codex 5969458776) and hold.correction.json records the change; hold.json is unchanged as raw evidence.",
        "f2: no Opus-lead confound found in this narrow review, not certified unaffected.",
        "Costs: $159.77 is counterfactual only; Jev about $0.001 is estimated; no invoice is verified; budget api_cost_usd=0 reflects the CLI exclusion, not zero cost.",
    ],
    caveats=[
        "Every executed cell ended on reported_token_threshold (2,500,000, applied after a response returns); no cell completed its feature by the engine's own judgement.",
        "claude:opus leads ran in Claude CLI permissionMode default and were denied every project Edit/Write (CLAUDE_SPEC has no write_args); pairs where the rule arm assigned an Opus lead are not comparable as Jev-versus-rule evidence. Grok and Sol leads wrote normally. Underlying reason for the hold.",
        "The hold was applied by appending an uncommitted marker line to docs/jev-stage-b-plan.md in the engine checkout, which the runner's integrity check reads as a changed engine and refuses to launch past; that is the technical stop mechanism, and the Opus editing-access fault is the reason.",
        "f3 R1 failed on both arms on a grader substring assertion stricter than the goal's wording; both frozen totals stand, the one-point gap is R5 (Codex, #35 5969338718).",
        "f5 jev grade withheld by the hold's integrity check; not graded post hoc.",
        "f6 and f7 (4 cells) were prepared and never launched.",
        "No claim of Jev quality benefit or savings is supported by these pairs.",
    ])
(out / "terminal-packet.json").write_text(json.dumps(packet, indent=2) + "\n")
print("terminal-packet.json v2 written; totals:", {k: totals[k] for k in ("executed", "unlaunched", "attempts", "reported_tokens", "summed_cell_seconds", "wall_span", "api_price_counterfactual_usd", "jev_estimated_usd_at_0_042_per_M_input", "jev_kind_answers", "jev_difficulty_answers")})
