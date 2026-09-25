set -e -o pipefail
S="/private/tmp/q9v2.nxS6sx"; cd "$S"; PY="$S/venv/bin/python"
CAND=d011a5c62a443bc6aef4eb4e81e0fad96873ecba; L="$S/launcher-1m-v2"
nokeys() { env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
               -u XAI_API_KEY -u GROK_API_KEY -u GROK_DEPLOYMENT_KEY \
               -u GEMINI_API_KEY -u GOOGLE_API_KEY "$@"; }
date -u +"setup start: %Y-%m-%dT%H:%M:%SZ"
echo "grok: $(/opt/homebrew/bin/grok models 2>&1 | head -1)"
[ "$(git -C "$S/candidate2" rev-parse HEAD)" = "$CAND" ]; [ "$(git -C "$S/baseline" rev-parse HEAD)" = 5d70d312868a3452c6aec9fd8461fd12fcdc31cb ]
$PY "$S/fixture/docs/harness-canary/fixture-v2/control/prepare.py" "$S/trial3" > "$S/manifest3.json"
[ "$(shasum -a 256 "$S/trial3-instrument/test_contract.py" | cut -d' ' -f1)" = 80f1cd5af0112c3f8a786429e38bd4d8648acdb332a87eab8baf27a0138eb9a9 ]
(cd "$S/candidate2" && nokeys env PYTHONPATH="$S/candidate2" "$PY" "$L/docs/harness-canary/run_fixture.py" --preflight --project "$S/trial3" > "$S/preflight-rerun2.log" 2>&1)
cp "$S/trial3/.quadratus/preflight.json" "$S/preflight-prebatch-rerun2.json"
$PY - <<'PY'
import json, datetime
from pathlib import Path
S = Path("/private/tmp/q9v2.nxS6sx")
r = json.loads((S / "allowance-rerun.json").read_text())
r.update({"batch_id": "q9v2-rerun2-1m-2026-09-23",
  "source": r["source"] + "; re-run after the grok CLI was signed into davisfox5@gmail.com (the first rerun hit the free-tier limit on davison@dfconsulting.tech)",
  "recorded_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "preflight_report": str(S / "preflight-prebatch-rerun2.json")})
(S / "allowance-rerun2.json").write_text(json.dumps(r, indent=2) + "\n"); print("allowance-rerun2.json written")
PY
echo "setup ok; preflight: $(tail -1 "$S/preflight-rerun2.log")"
set +e
GR="$S/venv/bin/python -m pytest -q -p no:cacheprovider $S/trial3-instrument/test_contract.py"
date -u +"runs start: %Y-%m-%dT%H:%M:%SZ"
for V in baseline candidate; do
  if [ "$V" = candidate ] && [ ! -s "$S/evidence3/baseline-runs.txt" ]; then
    echo "STOP: the baseline never launched; not starting the candidate"; break; fi
  RT="$S/$V"; [ "$V" = candidate ] && RT="$S/candidate2"
  nokeys "$PY" "$S/candidate2/tools/acceptance/series.py" run \
    --version "$V" --runtime "$RT" --fixture "$S/trial3" --count 1 \
    --allowance-record "$S/allowance-rerun2.json" --out "$S/evidence3" \
    --launcher "$L/docs/harness-canary/run_fixture.py" \
    --python "$PY" --wall-seconds 900 \
    --grader-command "$GR" 2>&1 | tee "$S/evidence3-$V.log" | tail -6
  echo "series exit for $V: $?"
done
date -u +"runs end: %Y-%m-%dT%H:%M:%SZ"
