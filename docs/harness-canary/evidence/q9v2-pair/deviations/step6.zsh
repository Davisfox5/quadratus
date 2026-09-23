S="/private/tmp/q9v2.nxS6sx"
cd "$S"
nokeys() { env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
               -u XAI_API_KEY -u GROK_API_KEY -u GROK_DEPLOYMENT_KEY \
               -u GEMINI_API_KEY -u GOOGLE_API_KEY "$@"; }
date -u +"step 6 start: %Y-%m-%dT%H:%M:%SZ"
set -o pipefail
GR="$S/venv/bin/python -m pytest -q -p no:cacheprovider $S/trial-instrument/test_contract.py"
for V in baseline candidate; do
  nokeys "$S/venv/bin/python" "$S/candidate/tools/acceptance/series.py" run \
    --version "$V" --runtime "$S/$V" --fixture "$S/trial" --count 1 \
    --allowance-record "$S/allowance.json" --out "$S/evidence" \
    --launcher "$S/candidate/docs/harness-canary/run_fixture.py" \
    --python "$S/venv/bin/python" --wall-seconds 900 \
    --grader-command "$GR" 2>&1 | tee "$S/evidence-$V.log" | tail -40
  RC=$?; echo "series exit for $V: $RC"
  if [ "$RC" -ne 0 ]; then echo "STOP: $V run refused or failed (exit $RC)"; break; fi
done
date -u +"step 6 end: %Y-%m-%dT%H:%M:%SZ"
