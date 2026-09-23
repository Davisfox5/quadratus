S="/private/tmp/q9v2.nxS6sx"; cd "$S"
nokeys() { env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
               -u XAI_API_KEY -u GROK_API_KEY -u GROK_DEPLOYMENT_KEY \
               -u GEMINI_API_KEY -u GOOGLE_API_KEY "$@"; }
set -o pipefail
GR="$S/venv/bin/python -m pytest -q -p no:cacheprovider $S/trial2-instrument/test_contract.py"
date -u +"rerun start: %Y-%m-%dT%H:%M:%SZ"
for V in baseline candidate; do
  if [ "$V" = candidate ] && [ ! -s "$S/evidence2/baseline-runs.txt" ]; then
    echo "STOP: the baseline never launched; not starting the candidate"; break; fi
  RT="$S/$V"; [ "$V" = candidate ] && RT="$S/candidate2"
  nokeys "$S/venv/bin/python" "$S/candidate2/tools/acceptance/series.py" run \
    --version "$V" --runtime "$RT" --fixture "$S/trial2" --count 1 \
    --allowance-record "$S/allowance-rerun.json" --out "$S/evidence2" \
    --launcher "$S/launcher-1m-v2/docs/harness-canary/run_fixture.py" \
    --python "$S/venv/bin/python" --wall-seconds 900 \
    --grader-command "$GR" 2>&1 | tee "$S/evidence2-$V.log" | tail -6
  RC=$?; echo "series exit for $V: $RC"
done
date -u +"rerun end: %Y-%m-%dT%H:%M:%SZ"
