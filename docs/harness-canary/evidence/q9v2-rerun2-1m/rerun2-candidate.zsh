set -e -o pipefail
S="/private/tmp/q9v2.nxS6sx"; cd "$S"; PY="$S/venv/bin/python"; L="$S/launcher-1m-v2"
nokeys() { env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
               -u XAI_API_KEY -u GROK_API_KEY -u GROK_DEPLOYMENT_KEY \
               -u GEMINI_API_KEY -u GOOGLE_API_KEY "$@"; }
date -u +"setup start: %Y-%m-%dT%H:%M:%SZ"
(cd "$S/candidate2" && nokeys env PYTHONPATH="$S/candidate2" "$PY" "$L/docs/harness-canary/run_fixture.py" --preflight --project "$S/trial3" > "$S/preflight-rerun2c.log" 2>&1)
cp "$S/trial3/.quadratus/preflight.json" "$S/preflight-prebatch-rerun2c.json"
$PY - <<'PY'
import json, datetime
from pathlib import Path
S = Path("/private/tmp/q9v2.nxS6sx")
r = json.loads((S / "allowance-rerun2.json").read_text())
r.update({"batch_id": "q9v2-rerun2-candidate-1m-2026-09-23", "runs": ["candidate"],
  "max_reported_tokens_batch": 1000000,
  "source": r["source"] + "; candidate only, after the baseline's 1,701,844-token overshoot left batch q9v2-rerun2-1m unable to admit it",
  "recorded_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "preflight_report": str(S / "preflight-prebatch-rerun2c.json")})
(S / "allowance-rerun2c.json").write_text(json.dumps(r, indent=2) + "\n"); print("allowance-rerun2c.json written")
PY
echo "setup ok; $(tail -1 "$S/preflight-rerun2c.log")"
set +e
GR="$PY -m pytest -q -p no:cacheprovider $S/trial3-instrument/test_contract.py"
date -u +"candidate start: %Y-%m-%dT%H:%M:%SZ"
nokeys "$PY" "$S/candidate2/tools/acceptance/series.py" run \
  --version candidate --runtime "$S/candidate2" --fixture "$S/trial3" --count 1 \
  --allowance-record "$S/allowance-rerun2c.json" --out "$S/evidence3" \
  --launcher "$L/docs/harness-canary/run_fixture.py" \
  --python "$PY" --wall-seconds 900 \
  --grader-command "$GR" 2>&1 | tee "$S/evidence3-candidate.log" | tail -6
echo "series exit for candidate: $?"
date -u +"candidate end: %Y-%m-%dT%H:%M:%SZ"
