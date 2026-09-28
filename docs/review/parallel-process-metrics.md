# Parallel build: process metrics for the first six lanes

## TL;DR

- **What this is.** Timings for the first six parallel worker lanes on PR #25 (Sol efficiency, Sol packaging, Sol acceptance, Opus completion, Opus findings, Opus evidence), from the moment each posted a ready candidate to its first review and its clearance. Every number comes from GitHub's server `created_at` on a named PR #25 comment. Clock times written inside comment bodies are not used.
- **What it is not.** It is not a speedup claim and not a product efficiency claim. There is no matched baseline, meaning the same scope built serially and measured the same way. Nothing here says the parallel run was faster or cheaper than an alternative.
- **Where things stood at 03:57:28Z.** All six lanes had a cleared candidate. Three of them (acceptance, packaging, evidence) were in the offline assembly `a9d0a93`, which root cleared within composition/test scope (5863060753). The other three were assigned to a second assembly that had not been posted yet. None of the six is wired into `Session`, and nothing was adopted or run live.
- **Wall clock.** 108:46 from launch (5862035398, 02:08:42Z) to assembly clearance.
- **Review queue.** The first review started 1–10 minutes after a candidate was ready, and a first clearance took 11–40 minutes. The exception is the scorecard, which took 106:33 and five correction rounds.
- **The largest single wait was integration.** Cleared candidates sat 31–71 minutes before the assembly picked them up. Completion was cleared at 02:38:30 and was still not in an assembly at 03:57:28 (79 minutes).
- **Re-review routing was the second largest.** After findings and evidence posted their corrections, 15 minutes passed before a re-reviewer was assigned. Both waits exceeded the two-window trigger in 5862103165.
- **Most first-round corrections were preventable.** Three of the five non-scorecard first reviews asked for a ruff I001 import-order fix. The frozen test image has no ruff.
- **Coordination volume.** 131 PR #25 comments in the window (02:07:21Z–03:57:28Z), roughly 1.2 a minute.

## Source and method

- **Data.** The local export `work/pr25-current.jsonl` (647 comments, 2026-09-22T22:59:34Z to 2026-09-28T03:49:48Z), plus a live `gh api …/issues/25/comments?since=…` read for everything after 03:49:48Z. The live read supplies only 5863060753 (03:57:28Z, assembly clearance) and 5863064745 (03:57:58Z, outside the window).
- **Clock.** GitHub `created_at`, to the second, UTC. Host clocks written inside comment bodies are ignored. They disagree with the server: Grok readiness progress 5862306715 says "02:45Z" and was posted at 02:40:28Z, and Claude's 5861822104 says "01:42" and self-corrects to 01:41 in 5861852007.
- **Event definitions.**
  - **Start** is the lane's first own post (claim or acknowledgment).
  - **Ready** is the first post naming an immutable candidate SHA as ready for review.
  - **Review start** is a posted reviewer claim. Where no claim was posted, it is unobserved.
  - **Verdict** is the first review result for that SHA.
  - **Correction** is the next candidate SHA from the lane.
  - **Clearance** is the post that clears the candidate within its stated scope. Scoped clearance is not integration or acceptance.
- **Durations.** Durations are `mm:ss` between two comment timestamps. Each segment is the time between two posts. It is not effort: a session may have been idle, working on something else, or waiting on tools during it.
- **Private reasoning.** Only posted comments are read. No session transcripts or private reasoning were used.
- **Reproducing a number.** Look up the two comment IDs and subtract their `created_at` values.

## The six lanes

The lanes are the six assigned in 5862035398 and 5862056172 (items 1–6). The Grok lanes (7–9) and later rolling lanes (J30, J31, seams, E1 and others) are outside this scope. They appear below only where they touch the six.

### Summary

| Lane | Start → ready | Ready → review start | Ready → first verdict | Correction rounds | Ready → clearance | In a cleared assembly at 03:57:28? |
|---|---|---|---|---|---|---|
| Sol efficiency (scorecard) | 3:34 | 6:00 | 8:02 (not cleared) | 5 | **106:33** | No. Assigned to the next assembly in 5863060753 |
| Sol packaging (wheel) | 5:25 | 9:26 (3:22 more blocked, not fetchable) | 16:00 (correction) | 1 | 27:05 | Yes, `a9d0a93` |
| Sol acceptance (manifest) | 5:23 | 1:04 | 6:19 (correction) | 1 | 12:36 | Yes, `a9d0a93` |
| Opus completion | 17:02 | unobserved | 10:35 (cleared) | 0 | 10:35 | No. Assigned to the next assembly |
| Opus findings (projection) | 16:11 | 9:27 | 11:10 (correction) | 1 | 29:51 | No. The follow-on `a66cbb6` is assigned to the next assembly |
| Opus evidence (tests) | 18:58 | 6:53 | 8:53 (correction) | 1 | 39:17 | Yes, `a9d0a93` |

- Median ready → clearance is 28:28, the mean of 27:05 and 29:51. With the scorecard excluded, the range is 10:35–39:17.
- There were nine correction rounds across the six lanes, five of them on the scorecard.

### Per-lane boundaries

Each row is one event. The comment ID is the evidence, and the times are server UTC on 2026-09-28.

#### 1. Sol efficiency: `tools/workflow_scorecard.py`

| Event | Comment | Time | SHA |
|---|---|---|---|
| Start (before the launch comment) | 5862024690 | 02:07:21 | base 9eabf69 |
| Ready v1 | 5862054722 | 02:10:55 | a787c51 |
| Review claim (Sol packaging) | 5862109357 | 02:16:55 | |
| Verdict: 3 findings | 5862131131 | 02:18:57 | |
| Correction 1 | 5862206036 | 02:28:21 | 739bd5b |
| Re-review claim | 5862267521 | 02:35:44 | |
| Verdict: 1 hole | 5862279986 | 02:37:15 | |
| Correction 2 | 5862300044 | 02:39:46 | ca592b0 |
| Verdict (Codex Sol; claim and result in one post): 2 holes | 5862348492 | 02:45:05 | |
| Verdict (Grok lane 8, overlapping): 5 items | 5862390835 | 02:49:47 | |
| Fresh consolidation author assigned | 5862453854 | 02:56:29 | |
| Correction 3 | 5862496798 | 03:01:44 | b5d6054 |
| Reviewer assigned; root pushed the branch so it could be fetched | 5862570855 | 03:08:56 | |
| Verdict: 2 gaps | 5862590704 | 03:11:14 | |
| Fresh schema-fix author assigned | 5862699144 | 03:20:55 | |
| Correction 4 | 5862715437 | 03:22:57 | 53d8dbe |
| Verdict: prior fixed, 1 small gap | 5862829115 | 03:33:11 | |
| Correction 5 | 5862977093 | 03:47:56 | 4297e22 |
| Clearance (root; offline observational tool only) | 5863060753 | 03:57:28 | |

The 106:33 splits as follows:
- 51:31 from a candidate to its verdict, covering queue and review;
- 55:02 from a verdict to the next candidate, covering routing and authoring.

Two of the five corrections were written by fresh author sessions, assigned 6:42 and 9:41 after the verdict they answered. The lane started 81 seconds before the launch comment 5862035398 (02:08:42).

#### 2. Sol packaging: `tools/verify_workflow_wheel.py`

| Event | Comment | Time | SHA |
|---|---|---|---|
| Start | 5862026724 | 02:07:36 | |
| Ready (local only, not on origin) | 5862073795 | 02:13:01 | 8a83795 |
| Root assigns Claude | 5862110665 | 02:17:02 | |
| Claude claims; blocked because the SHA is not on origin | 5862158458 | 02:22:27 | |
| Pushed, now fetchable | 5862185681 | 02:25:49 | |
| Verdict: I001 plus docs wording | 5862211416 | 02:29:01 | |
| Correction | 5862288208 | 02:38:17 | ea8dcd5 |
| Clearance (Claude; wheel layout and producer boundary only) | 5862303286 | 02:40:06 | |
| Imported into the assembly | 5862900978 | 03:39:42 | a9d0a93 |

27:05 = 9:26 queue + 3:22 blocked on fetch + 3:12 review + 9:16 correction + 1:49 recheck.

#### 3. Sol acceptance: `tools/workflow_acceptance.py`

| Event | Comment | Time | SHA |
|---|---|---|---|
| Start | 5862049157 | 02:10:18 | |
| Ready (pushed) | 5862097343 | 02:15:41 | a6146da |
| Review claim (Claude) | 5862107676 | 02:16:45 | |
| Duplicate review claim (root) | 5862110665 | 02:17:02 | |
| Verdict (Claude): I001 | 5862154976 | 02:22:00 | |
| Root finding: FAIL vs missing proof (inside a coordination post) | 5862193380 | 02:26:44 | |
| Correction | 5862196505 | 02:27:08 | 87610b1 |
| Clearance (root; coverage inventory only) | 5862205507 | 02:28:17 | |
| Imported into the assembly | 5862900978 | 03:39:42 | a9d0a93 |

12:36 = 1:04 queue + 5:15 review + 5:08 correction + 1:09 recheck. The two reviewers' claims crossed 17 seconds apart, and Claude stood down in 5862158458.

#### 4. Opus completion: `quadratus/completion_decision.py`

| Event | Comment | Time | SHA |
|---|---|---|---|
| Start | 5862054421 | 02:10:53 | |
| All three Opus sessions hit their 40-turn bound and were resumed | 5862193380 | 02:26:44 | |
| Ready | 5862202642 | 02:27:55 | 064f930 |
| Clearance (root; unwired parity candidate). No review claim was posted | 5862289757 | 02:38:30 | |
| Assigned to the next assembly | 5863060753 | 03:57:28 | |

A cleared candidate waited 79:00 without an assembly. A separate follow-on lane (seams, 91f90d7 → 05840ea) is not counted here.

#### 5. Opus findings: `quadratus/finding_state.py`

| Event | Comment | Time | SHA |
|---|---|---|---|
| Start | 5862054109 | 02:10:51 | |
| Ready | 5862195606 | 02:27:02 | 22eed61 |
| Review claim (Sol efficiency, acting as reviewer) | 5862273790 | 02:36:29 | |
| Verdict: 3 counterexamples | 5862287503 | 02:38:12 | |
| Correction | 5862314723 | 02:41:19 | a7a9cd6 |
| Re-reviewer assigned | 5862453854 | 02:56:29 | |
| Clearance (Codex Sol; scoped) | 5862457343 | 02:56:53 | |
| Follow-on full-text lane ready (a different author, after engine seam 8529b2a) | 5862735609 | 03:25:01 | a66cbb6 |
| Follow-on cleared for staged integration preparation | 5862969439 | 03:46:59 | |

29:51 = 9:27 queue + 1:43 review + 3:07 correction + 15:10 waiting for a re-reviewer + 0:24 re-review.

#### 6. Opus evidence: `tests/test_workflow_evidence_boundary.py`

| Event | Comment | Time | SHA |
|---|---|---|---|
| Start | 5862052645 | 02:10:41 | |
| Ready | 5862216513 | 02:29:39 | 5b110be |
| Review claim (Sol acceptance, acting as reviewer) | 5862274297 | 02:36:32 | |
| Verdict: I001 plus E2 disposition | 5862290045 | 02:38:32 | |
| Root ruling on E2 | 5862294492 | 02:39:07 | |
| Correction | 5862309978 | 02:40:48 | 006bdbe |
| Re-reviewer assigned | 5862453854 | 02:56:29 | |
| Clearance, relayed by root. The reviewer withheld its own post | 5862570855 | 03:08:56 | |
| Imported into the assembly, with E2 aligned to bbb4956 | 5862900978 | 03:39:42 | a9d0a93 |

39:17 = 6:53 queue + 2:00 review + 2:16 correction + 15:41 waiting for a re-reviewer + 12:27 from assignment to the relayed clearance. When the reviewer actually finished is unobserved.

## Current assembly

| Event | Comment | Time |
|---|---|---|
| Fresh Sol offline-assembly owner assigned (base 7d294ce) | 5862821110 | 03:32:30 |
| Candidate ready: acceptance 87610b1, wheel ea8dcd5, J30 225f46f, J31 7aacdd6, evidence 006bdbe | 5862900978 | 03:39:42 |
| Independent Sol assembly reviewer assigned | 5862969439 | 03:46:59 |
| Cleared within composition/test scope (the reviewer withheld its own post; root relays) | 5863060753 | 03:57:28 |

- **Timings.** Assign → ready 7:12. Ready → reviewer assigned 7:17. Ready → clearance 17:46.
- **Scope of the clearance.** Composition and tests only. The frozen-image wheel test is still skipped, and the host wheel probe passed separately. There is no `Session` wiring, no adoption and no live run.
- **Next assembly.** A second assembly (completion 064f930, seams 05840ea, projection a66cbb6, scorecard 4297e22) was assigned in 5863060753. It had not been posted when this was written.

## Authoring, queue and integration

Every ready → clearance interval above breaks into three kinds of time:

| Kind | What it covers | Observed range |
|---|---|---|
| **Authoring** | Start → first ready, and verdict → correction when the same author corrects | First ready 3:34–18:58 (Sol 3:34–5:25, Opus 16:11–18:58). Corrections 2:16–9:24 |
| **Review queue** | Ready → review start, and correction → re-reviewer assigned | 1:04–9:26 first time. 15:10 and 15:41 for the two re-reviews that waited for rolling assignment |
| **Review** | Review start → verdict | 1:31–5:15 where both ends were posted |
| **Integration** | Clearance → the candidate appears in an assembly → assembly clearance | Clearance → assembly ready: acceptance 71:25, packaging 59:36, evidence 30:46. Assembly ready → clearance: 17:46. Completion not assembled after 79:00 |

- **Integration dominates.** Every assembled lane spent longer waiting for the assembly than it spent in review and correction combined. That was a sequencing choice, not slowness: root started assembly at 03:32:30 ("prioritizing a reviewed integrated tree over more standalone candidates", 5862821110).
- **Why Opus lanes took longer to author.** Opus lanes took 3–5× longer to reach first ready than Sol lanes. All three hit the 40-turn bound and were resumed (5862193380). Their first candidates also carried more (65, 30 and 19 whole-Session or shadow tests, against 5–9 for the Sol tools). This is not a model comparison: the tasks differ.

## Coordination volume

131 PR #25 comments were posted between 02:07:21Z (the first lane start) and 03:57:28Z (assembly clearance). Author is read from the body prefix, not the GitHub login, because the export has no login field.

| Author (by prefix) | Comments |
|---|---|
| Opus 5.5 workers | 41 |
| Sol workers (including "Codex Sol" and "Codex (Sol …)") | 32 |
| Claude (incumbent) | 28 |
| Codex root | 23 |
| Grok | 7 |

- The scorecard lane alone accounts for eight author posts, eight review posts (claims and verdicts) and nine root routing or ruling posts.
- The cost of reading this volume (tokens, operator attention) is not recorded anywhere and is not estimated here.

## Missing observations

- **Review start was not posted** for completion 064f930 (root), the acceptance recheck of 87610b1 (root), the scorecard's final review of 4297e22 (root), and the scorecard 53d8dbe review (the reviewer was named in 5862821110 with no claim). Queue and review time are combined for those.
- **Two reviewer results were withheld and relayed by root:** the evidence recheck of 006bdbe (5862570855) and the assembly review of a9d0a93 (5863060753). The true review completion time is unobserved; the relay time is an upper bound.
- **Scorecard review 5862590704** was first posted as a literal file path and repaired by root (5862699144). Its `created_at` is the original post; the edit time is not in the export.
- **Opus turn-bound resumption.** When each Opus session hit its 40-turn bound and was resumed is not posted. Only the root note (02:26:44) exists.
- **The Sol efficiency start precedes the launch comment** by 81 seconds, so for that lane "start" is its own post, not the assignment.
- **Effort is not measured.** Session activity, tokens, cost and operator time are all unrecorded. The intervals are elapsed time between posts.
- **Grok lanes 8 and 9** were separate user-started sessions. Grok's first acknowledgment (5862064205) was one session covering three lanes, and root notes this is not three independent workers (5862205507). Grok is outside the six but produced one scorecard verdict (5862390835).

## No baseline

- No run built the same scope serially, one session at a time, and recorded the same events. So there is no speedup figure, and none should be derived from these tables.
- The incumbent's own pre-parallel cycle on 332a407/9eabf69 is context only: ready 01:49:38 (5861882581), verdict 01:53:16 (5861911237), correction ready 02:01:32 (5861979462), cleared 02:03:42 (5861996123), about 14 minutes. It was a different kind of work (an engine change by the incumbent, reviewed by root) and is not comparable.
- A matched comparison would need the same task list run both ways, the same event definitions, and effort and cost recorded alongside elapsed time.

## Actionable bottlenecks

1. **Assemble continuously, not at a checkpoint.** Cleared candidates waited 31–79 minutes for an assembly that took 7:12 to build and 17:46 to clear. Rebasing each cleared candidate onto a standing assembly branch when it clears would remove most of the largest wait.
2. **The first reviewer owns the recheck by default.** The findings and evidence corrections waited 15:10 and 15:41 for a re-reviewer, and both passed the two-window trigger of 5862103165 with no reassessment posted before 02:56:29. Rechecks that stayed with the same reviewer (packaging, acceptance) cleared 1:09–1:49 after the correction.
3. **Freeze the scorecard's acceptance criteria.** Each round drew a different reviewer (Sol packaging twice, Codex Sol, Grok, then three fresh Sol or root reviewers), and each found new adversarial holes. After round 2, a fixed list of negative controls agreed in PR #25 would bound the remaining rounds. Root's consolidation assignment (5862453854) was the right move, just late.
4. **Put ruff in the frozen image, or require host ruff before handoff.** An I001 appeared in the first verdict for packaging, acceptance and evidence, and again for completion seams 91f90d7 (5862605364). Several authors reported ruff as unavailable (5862309978, 5862461217, 5862594124).
5. **A handoff must be a pushed SHA.** Local-only SHAs blocked reviewers: packaging 8a83795 (3:22), scorecard b5d6054 (root pushed it), seams 05840ea (5862804114 → root push 5862821110), and c868bf2 for the E1 audit (5862840133).
6. **Reviewers post their own results.** Two withheld results left the review boundaries unobservable. Root's clarification (5862570855) that PR #25's older head does not block posting addresses one cause.
7. **Check claims before reviewing.** The acceptance review was claimed twice, 17 seconds apart. Root's rule that its ownership list is authoritative (5862453854) came 40 minutes later.

Snapshot: PR #25 comments through 5863060753 (2026-09-28T03:57:28Z). Later events are not included.
