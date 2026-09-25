set -e -o pipefail
S="/private/tmp/q9v2.nxS6sx"; cd "$S"; PY="$S/venv/bin/python"
CAND=ce0ceefe93423cd7d96bad42f39aff4667874bfb; BASE=5d70d312868a3452c6aec9fd8461fd12fcdc31cb
L="$S/launcher-1m-v3"; OUT="$S/evidence-series"; REC="$S/allowance-series.json"
nokeys() { env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
               -u XAI_API_KEY -u GROK_API_KEY -u GROK_DEPLOYMENT_KEY \
               -u GEMINI_API_KEY -u GOOGLE_API_KEY "$@"; }
say() { echo "$(date -u +%H:%M:%SZ) $*"; }
say "setup: candidate $CAND, baseline $BASE"
git clone -q -b claude/candidate-q9v2 https://github.com/Davisfox5/quadratus "$S/candidate3"
git -C "$S/candidate3" checkout -q "$CAND"; [ "$(git -C "$S/baseline" rev-parse HEAD)" = "$BASE" ]
mkdir -p "$L/docs/harness-canary" "$L/tools/acceptance"
cp "$S/candidate3/tools/acceptance/preflight.py" "$L/tools/acceptance/"
cp "$S/candidate3/docs/harness-canary/run_fixture.py" "$L/docs/harness-canary/"
sed -i '' 's/^LAUNCHER_TOKENS = 500_000$/LAUNCHER_TOKENS = 1_000_000/' "$L/docs/harness-canary/run_fixture.py"
sed -i '' 's/"CLI-only; 24 attempts; 500000 reported-token stop; "/f"CLI-only; 24 attempts; {LAUNCHER_TOKENS} reported-token stop; "/' "$L/docs/harness-canary/run_fixture.py"
diff -u "$S/candidate3/docs/harness-canary/run_fixture.py" "$L/docs/harness-canary/run_fixture.py" > "$L/run_fixture.1m.diff" || true
shasum -a 256 "$L/docs/harness-canary/run_fixture.py" > "$L/run_fixture.1m.sha256"
$PY "$S/fixture/docs/harness-canary/fixture-v2/control/prepare.py" "$S/trial4" > "$S/manifest4.json"
[ "$(shasum -a 256 "$S/trial4-instrument/test_contract.py" | cut -d' ' -f1)" = 80f1cd5af0112c3f8a786429e38bd4d8648acdb332a87eab8baf27a0138eb9a9 ]
say "grok: $(/opt/homebrew/bin/grok models 2>&1 | head -1)"
(cd "$S/candidate3" && nokeys env PYTHONPATH="$S/candidate3" "$PY" "$L/docs/harness-canary/run_fixture.py" --preflight --project "$S/trial4" > "$S/preflight-series.log" 2>&1)
cp "$S/trial4/.quadratus/preflight.json" "$S/preflight-prebatch-series.json"
$PY - <<'PY'
import json, datetime
from pathlib import Path
S = Path("/private/tmp/q9v2.nxS6sx")
r = json.loads((S / "allowance.seed.json").read_text())
r.update({"batch_id": "q9v2-series-5x-2026-09-24",
  "source": "Davis, directly, in the local Claude Code session on the Mac (Claude Opus 5.5), 2026-09-24, approving Codex's step 3 after the review of c587a78 on PR #25",
  "instruction": "Done. Have a look at Codex's review on PR 25, then once everything looks good on steps 1 & 2, proceed to step 3",
  "recorded_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "candidate_sha": "ce0ceefe93423cd7d96bad42f39aff4667874bfb", "runs": ["baseline", "candidate"],
  "runs_per_version": 5, "max_reported_tokens_each": 1000000, "max_reported_tokens_batch": 12000000,
  "preflight_report": str(S / "preflight-prebatch-series.json")})
(S / "allowance-series.json").write_text(json.dumps(r, indent=2) + "\n")
PY
say "setup ok; $(tail -1 "$S/preflight-series.log")"
set +e
GR="$PY -m pytest -q -p no:cacheprovider $S/trial4-instrument/test_contract.py"
for i in 1 2 3 4 5; do
  for V in baseline candidate; do
    RT="$S/baseline"; [ "$V" = candidate ] && RT="$S/candidate3"
    say "START $V $i"
    nokeys "$PY" "$S/candidate3/tools/acceptance/series.py" run \
      --version "$V" --runtime "$RT" --fixture "$S/trial4" --count 1 \
      --allowance-record "$REC" --out "$OUT" --launcher "$L/docs/harness-canary/run_fixture.py" \
      --python "$PY" --wall-seconds 900 --grader-command "$GR" > "$S/series-$V-$i.log" 2>&1
    RC=$?
    if ! grep -q "^$V: " "$S/series-$V-$i.log"; then say "STOP: $V $i was not launched: $(tail -1 "$S/series-$V-$i.log")"; exit 0; fi
    VERDICT=$($PY - "$OUT" "$V" <<'PY'
import json, sys
from pathlib import Path
out, v = Path(sys.argv[1]), sys.argv[2]
run = Path((out / f"{v}-runs.txt").read_text().split()[-1])
res = json.loads((run / "result.json").read_text()) if (run / "result.json").exists() else {}
bud = json.loads((run / "budget.json").read_text()) if (run / "budget.json").exists() else {}
g = (run / "grader.txt").read_text().strip().splitlines()[-1] if (run / "grader.txt").exists() else "no grader"
ver = [json.loads(l) for l in open(run / "invocations.jsonl")] if (run / "invocations.jsonl").exists() else []
ver = [r for r in ver if r.get("invoked") and r.get("role") == "verifier"]
aux = [((r.get("diagnostics") or {}).get("auxiliary_tokens") or 0) for r in ver]
flags = []
if "WindowExhausted" in str(res.get("error")): flags.append("QUOTA")
if v == "candidate" and any(a >= 50000 for a in aux): flags.append("DELEGATION")
print(f"completed={res.get('completed')} tokens={bud.get('reported_tokens')} grader=[{g}] "
      f"verifier_aux={aux or 'no verifier call'} error={str(res.get('error'))[:90]!r} FLAGS={','.join(flags) or 'none'}")
PY
)
    say "END $V $i exit=$RC $VERDICT"
    case "$VERDICT" in *QUOTA*) say "STOP: a vendor quota ran out; counted as an attempted run"; exit 0;; esac
    case "$VERDICT" in *DELEGATION*) say "STOP: candidate verifier shows usage beyond the seat >= 50,000 tokens"; exit 0;; esac
  done
done
say "SERIES COMPLETE"
