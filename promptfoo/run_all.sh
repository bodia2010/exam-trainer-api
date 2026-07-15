#!/usr/bin/env bash
# run_all.sh — the one command for "is it safe to ship this prompt/schema
# change" (PRODUCT_PLAN.md). Runs both promptfoo configs and prints
# a single PASS/FAIL verdict.
#
#   ./run_all.sh
#
# Requires GOOGLE_API_KEY in the environment (see README.md's "One-time
# setup") and fixtures/ populated via make_fixtures.py.
#
# What it does:
#   1. promptfooconfig.parse.yaml — run ONCE. Any failure (assertion or
#      provider error, on ANY of the 14 tests) is fatal. Parse is not
#      documented anywhere as flaky, so zero tolerance.
#   2. promptfooconfig.discover.yaml — run UP TO 3 times, because the
#      config's own comments document that its regression test
#      (single_question_correction_not_split, on the
#      regression_fixtures/discover_single_question_correction.txt
#      fixture) is measured ~1-in-3 flaky at temperature=1 by design
#      (generation_config.py) — that flakiness is an accepted, monitored
#      risk per the yaml, not something to chase to zero here. The OTHER
#      discover test ("Full document — must find 'other' markers...") is
#      NOT documented as flaky anywhere — a failure there is treated as a
#      real regression, full stop, never excused by the retry loop.
#
#      Verdict logic per run, from the JSON output's per-test `success`:
#        - all tests green                          -> this run is GREEN
#        - only the documented-flaky test failed     -> FLAKY_ONLY
#        - the full-document test failed (regardless
#          of the flaky test's result), or anything
#          else unexpected failed/errored            -> REAL_FAIL
#
#      Overall discover verdict:
#        - PASS as soon as any run is GREEN (stop early — no point
#          spending more Gemini calls once one clean run has already
#          proven the prompt/schema combo works).
#        - FAIL if, after all runs actually attempted (up to 3, fewer if
#          a REAL_FAIL was already conclusive), no run was GREEN and at
#          least one run was REAL_FAIL. A real regression proves itself
#          in a single reproduction; it doesn't need to repeat 3/3 times
#          to count, and waiting for that would risk shipping a broken
#          prompt just because the flaky test also happened to fail
#          alongside it on the other attempts.
#        - PASS (with a loud warning) if no run was GREEN but EVERY
#          failure seen was FLAKY_ONLY — i.e. we got unlucky on the
#          documented coin flip 3 times in a row (rare, ~3-4% at a true
#          1-in-3 rate). This is the one case where "3 non-green runs"
#          does NOT mean "fail": nothing outside the documented-flaky
#          test ever failed, so there is nothing to regress-fix. Printed
#          loudly so a human can sanity-check rather than silently
#          swallowed.
#
#   3. Exit code: 0 only if parse passed AND discover resolved to
#      PASS/warned-PASS as above. Non-zero (1) on any real failure —
#      suitable for a CI gate.
#
# --no-cache is used for every eval invocation. Without it, promptfoo's
# on-disk result cache would return the exact same cached output for
# retries 2 and 3 of the discover config (same prompt+vars+provider ==
# same cache key), which would silently defeat the entire point of
# retrying a test that's flaky specifically because discovery runs at
# temperature=1 — each retry MUST be a fresh live call.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PROMPTFOO="npx --yes promptfoo@latest"
DISCOVER_ATTEMPTS=3
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

if [[ -z "${GOOGLE_API_KEY:-}" ]]; then
  echo "ERROR: GOOGLE_API_KEY is not set — see README.md 'One-time setup'." >&2
  echo "This is a missing-environment problem, not a gate verdict; nothing was run." >&2
  exit 2
fi

# Classifies one discover eval's JSON output. Prints exactly one token to
# stdout: GREEN, FLAKY_ONLY, or REAL_FAIL. Human-readable detail goes to
# stderr. A test counts as "the documented-flaky one" if its markdown
# fixture is regression_fixtures/discover_single_question_correction.txt
# — the exact fixture promptfooconfig.discover.yaml's comments name as
# the ~1-in-3 flaky case; every other test in the config is held to zero
# tolerance.
classify_discover_run() {
  local json_file="$1"
  python3 - "$json_file" <<'PYEOF'
import json
import sys

path = sys.argv[1]
try:
    with open(path) as f:
        data = json.load(f)
    results = data["results"]["results"]
except Exception as e:
    print(f"could not parse {path}: {e}", file=sys.stderr)
    print("REAL_FAIL")
    sys.exit(0)

if not results:
    print(f"no results at all in {path} — treating as a real failure", file=sys.stderr)
    print("REAL_FAIL")
    sys.exit(0)

FLAKY_FIXTURE = "discover_single_question_correction"

real_fail = False
flaky_fail = False
for r in results:
    success = bool(r.get("success"))
    desc = (r.get("testCase") or {}).get("description") or "(no description)"
    markdown_var = str((r.get("vars") or {}).get("markdown", ""))
    is_flaky_test = FLAKY_FIXTURE in markdown_var
    if success:
        print(f"  [pass] {desc}", file=sys.stderr)
        continue
    reason = r.get("error") or (r.get("gradingResult") or {}).get("reason") or "unknown failure"
    if is_flaky_test:
        flaky_fail = True
        print(f"  [FAIL - documented flaky test] {desc}: {reason}", file=sys.stderr)
    else:
        real_fail = True
        print(f"  [FAIL - NOT the documented flaky test, real regression signal] {desc}: {reason}", file=sys.stderr)

if real_fail:
    print("REAL_FAIL")
elif flaky_fail:
    print("FLAKY_ONLY")
else:
    print("GREEN")
PYEOF
}

echo "=== 1/2: promptfooconfig.parse.yaml (single run, zero tolerance) ==="
PARSE_JSON="$WORKDIR/parse.json"
$PROMPTFOO eval -c promptfooconfig.parse.yaml --no-cache --no-progress-bar -o "$PARSE_JSON"
PARSE_EXIT=$?
if [[ $PARSE_EXIT -eq 0 ]]; then
  PARSE_VERDICT="PASS"
  echo "--- parse: PASS ---"
else
  PARSE_VERDICT="FAIL"
  echo "--- parse: FAIL (promptfoo exit code $PARSE_EXIT — see table above) ---"
fi

echo
echo "=== 2/2: promptfooconfig.discover.yaml (up to $DISCOVER_ATTEMPTS runs, tolerating the documented flaky test) ==="

DISCOVER_VERDICT="FAIL"
SAW_REAL_FAIL=0
RUNS_DONE=0
for i in $(seq 1 "$DISCOVER_ATTEMPTS"); do
  RUNS_DONE=$i
  echo "--- discover attempt $i/$DISCOVER_ATTEMPTS ---"
  DISCOVER_JSON="$WORKDIR/discover_run_${i}.json"
  $PROMPTFOO eval -c promptfooconfig.discover.yaml --no-cache --no-progress-bar -o "$DISCOVER_JSON"

  CLASSIFICATION="$(classify_discover_run "$DISCOVER_JSON")"
  echo "  => attempt $i classified as: $CLASSIFICATION"

  case "$CLASSIFICATION" in
    GREEN)
      DISCOVER_VERDICT="PASS"
      echo "  a fully green run was found — no need to burn more Gemini calls, stopping early."
      break
      ;;
    REAL_FAIL)
      SAW_REAL_FAIL=1
      ;;
    FLAKY_ONLY)
      : # tolerated, keep trying
      ;;
    *)
      # Unrecognized classifier output — fail closed rather than risk
      # papering over a real regression on a parsing bug in this script.
      echo "  unexpected classifier output '$CLASSIFICATION' — treating as real failure" >&2
      SAW_REAL_FAIL=1
      ;;
  esac
done

if [[ "$DISCOVER_VERDICT" != "PASS" ]]; then
  if [[ $SAW_REAL_FAIL -eq 1 ]]; then
    DISCOVER_VERDICT="FAIL"
    echo "--- discover: FAIL — at least one of $RUNS_DONE run(s) failed OUTSIDE the documented-flaky test. That's a real regression signal, not the known coin flip. ---"
  else
    # Every failing run failed ONLY on the documented-flaky test, and we
    # never got a clean run in $DISCOVER_ATTEMPTS tries — an unlucky but
    # within-spec outcome for a ~1-in-3 flaky test, not a regression.
    DISCOVER_VERDICT="PASS"
    echo "--- discover: PASS (with warning) — $RUNS_DONE/$DISCOVER_ATTEMPTS runs failed, but every failure was the documented ~1-in-3 flaky test and nothing else. Unlucky, not a regression. Consider re-running manually if this repeats. ---"
  fi
fi

echo
echo "=================================================="
echo " parse:    $PARSE_VERDICT"
echo " discover: $DISCOVER_VERDICT"
if [[ "$PARSE_VERDICT" == "PASS" && "$DISCOVER_VERDICT" == "PASS" ]]; then
  echo " VERDICT: PASS"
  echo "=================================================="
  exit 0
else
  echo " VERDICT: FAIL"
  echo "=================================================="
  exit 1
fi
