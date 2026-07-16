#!/usr/bin/env bash
# Cost-aware Promptfoo release gate.
#
# Parse is the least expensive gate, but it still makes paid calls. A mode must
# therefore always be chosen explicitly; an accidental bare invocation fails
# before checking credentials or starting Promptfoo.
#
# Usage:
#   ./run_all.sh --parse-only
#   ./run_all.sh --discover-only         # exactly one full discovery pass
#   ./run_all.sh --full-release          # parse, then one discovery pass
#   ./run_all.sh --full-release --dry-run
#
# A failed parse stops --full-release before discovery, so a known-bad parse
# never burns the expensive discovery call. Discovery is never automatically
# retried: a small regression fixture must not cause the whole 646 KB document
# fixture to be submitted again. Any failed discovery test fails closed and
# can be investigated/rerun deliberately.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MODE=""
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: ./run_all.sh [MODE] [--dry-run]

Modes (choose at most one):
  --parse-only      Run the 18-test parse gate only.
  --discover-only   Run the complete discovery config exactly once.
  --full-release    Run parse, then discovery exactly once if parse passes.

Options:
  --dry-run         Print the exact eval command(s); no API key or calls needed.
  -h, --help        Show this help.

Live modes require GOOGLE_API_KEY. Exit status is 0 for a passing selected
gate, 1 for a gate failure, and 2 for invalid CLI/missing environment.
EOF
}

mode_was_set=0
set_mode() {
  local requested="$1"
  local option="$2"
  if [[ $mode_was_set -eq 1 ]]; then
    echo "ERROR: choose only one gate mode (duplicate/conflicting $option)." >&2
    usage >&2
    exit 2
  fi
  MODE="$requested"
  mode_was_set=1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --parse-only)
      set_mode "parse" "$1"
      ;;
    --discover-only)
      set_mode "discover" "$1"
      ;;
    --full-release)
      set_mode "full" "$1"
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ $mode_was_set -ne 1 ]]; then
  echo "ERROR: choose a gate mode explicitly; no paid default is selected." >&2
  usage >&2
  exit 2
fi

# Test/automation seam: production uses npx exactly as before, while focused
# tests can point at a local fake executable without installing Promptfoo or
# making a network request. This is intentionally a command array assembled
# once; no eval is used, so shell metacharacters in the environment are not
# executed.
if [[ -n "${PROMPTFOO_COMMAND:-}" ]]; then
  read -r -a PROMPTFOO <<<"$PROMPTFOO_COMMAND"
else
  PROMPTFOO=(npx --yes promptfoo@0.121.19)
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

print_command() {
  printf 'DRY-RUN:'
  printf ' %q' "$@"
  printf '\n'
}

run_eval() {
  local config="$1"
  local output="$2"
  local command=(
    "${PROMPTFOO[@]}" eval -c "$config" --no-cache --no-progress-bar -o "$output"
  )
  if [[ $DRY_RUN -eq 1 ]]; then
    print_command "${command[@]}"
    return 0
  fi
  "${command[@]}"
}

# Returns one token on stdout. Human-readable evidence goes to stderr.
# TARGETED_FAIL identifies the historically troublesome small synthetic
# fixture, but it is still a failure: callers may rerun that fixture manually,
# never the entire discovery config automatically. Any other malformed or
# failed result is REAL_FAIL and fails closed.
classify_discover_run() {
  local json_file="$1"
  python3 - "$json_file" <<'PYEOF'
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        results = json.load(f)["results"]["results"]
except Exception as e:
    print(f"could not parse {path}: {e}", file=sys.stderr)
    print("REAL_FAIL")
    raise SystemExit(0)

expected_paths = {
    "fixtures/discover_input.txt",
    "regression_fixtures/discover_single_question_correction.txt",
    "regression_fixtures/discover_split_slash_variant_number.txt",
}
actual_paths = [
    str((result.get("vars") or {}).get("markdown_path", ""))
    for result in results
]
if len(results) != len(expected_paths) or set(actual_paths) != expected_paths:
    print(
        "discovery result set mismatch: "
        f"expected exactly {sorted(expected_paths)!r}, got {actual_paths!r}",
        file=sys.stderr,
    )
    print("REAL_FAIL")
    raise SystemExit(0)

target_fixture = "discover_single_question_correction"
failed = []
for result in results:
    if result.get("success") is True:
        continue
    description = (result.get("testCase") or {}).get("description") or "(no description)"
    markdown_path = str((result.get("vars") or {}).get("markdown_path", ""))
    reason = result.get("error") or (result.get("gradingResult") or {}).get("reason") or "unknown failure"
    failed.append((description, markdown_path, reason))

if not failed:
    print("GREEN")
elif all(target_fixture in markdown_path for _, markdown_path, _ in failed):
    for description, _, reason in failed:
        print(f"  [FAIL - targeted regression] {description}: {reason}", file=sys.stderr)
    print("TARGETED_FAIL")
else:
    for description, _, reason in failed:
        print(f"  [FAIL - release blocker] {description}: {reason}", file=sys.stderr)
    print("REAL_FAIL")
PYEOF
}

parse_run_is_green() {
  local json_file="$1"
  python3 - "$json_file" <<'PYEOF'
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        results = json.load(f)["results"]["results"]
except Exception as e:
    print(f"could not parse {path}: {e}", file=sys.stderr)
    raise SystemExit(1)

if not isinstance(results, list) or len(results) != 18:
    count = len(results) if isinstance(results, list) else "not a list"
    print(f"parse result count mismatch: expected exactly 18, got {count}", file=sys.stderr)
    raise SystemExit(1)

failed = [index + 1 for index, result in enumerate(results) if result.get("success") is not True]
if failed:
    print(f"parse unsuccessful result(s): {failed}", file=sys.stderr)
    raise SystemExit(1)
PYEOF
}

run_parse_gate() {
  local output="$WORKDIR/parse.json"
  echo "=== parse: promptfooconfig.parse.yaml (single run, zero tolerance) ==="
  local status=0
  run_eval promptfooconfig.parse.yaml "$output" || status=$?
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "--- parse: DRY-RUN ---"
    return 0
  fi
  local json_green=0
  parse_run_is_green "$output" || json_green=$?
  if [[ $status -eq 0 && $json_green -eq 0 ]]; then
    echo "--- parse: PASS ---"
    return 0
  fi
  echo "--- parse: FAIL (promptfoo exit code $status, JSON validation $json_green) ---"
  return 1
}

run_discover_gate() {
  local output="$WORKDIR/discover.json"
  echo "=== discover: promptfooconfig.discover.yaml (one full pass, no automatic retry) ==="
  local eval_status=0
  run_eval promptfooconfig.discover.yaml "$output" || eval_status=$?
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "--- discover: DRY-RUN ---"
    return 0
  fi

  local classification
  classification="$(classify_discover_run "$output")"
  echo "  => discovery classified as: $classification"

  # A non-zero provider/eval exit is always a failure even if a malformed
  # tool happened to write an apparently green JSON file.
  if [[ $eval_status -ne 0 && "$classification" == "GREEN" ]]; then
    classification="REAL_FAIL"
    echo "  promptfoo exited $eval_status despite green-looking JSON; failing closed." >&2
  fi

  case "$classification" in
    GREEN)
      echo "--- discover: PASS ---"
      return 0
      ;;
    TARGETED_FAIL)
      echo "--- discover: FAIL — only the small targeted regression failed; the full config will NOT be retried automatically. ---"
      return 1
      ;;
    REAL_FAIL|*)
      echo "--- discover: FAIL — release blocker; no automatic retry. ---"
      return 1
      ;;
  esac
}

if [[ $DRY_RUN -ne 1 && -z "${GOOGLE_API_KEY:-}" ]]; then
  echo "ERROR: GOOGLE_API_KEY is not set — nothing was run." >&2
  exit 2
fi

PARSE_VERDICT="SKIPPED"
DISCOVER_VERDICT="SKIPPED"

case "$MODE" in
  parse)
    if run_parse_gate; then
      if [[ $DRY_RUN -eq 1 ]]; then
        PARSE_VERDICT="PLANNED"
      else
        PARSE_VERDICT="PASS"
      fi
    else
      PARSE_VERDICT="FAIL"
    fi
    ;;
  discover)
    if run_discover_gate; then
      if [[ $DRY_RUN -eq 1 ]]; then
        DISCOVER_VERDICT="PLANNED"
      else
        DISCOVER_VERDICT="PASS"
      fi
    else
      DISCOVER_VERDICT="FAIL"
    fi
    ;;
  full)
    if run_parse_gate; then
      if [[ $DRY_RUN -eq 1 ]]; then
        PARSE_VERDICT="PLANNED"
      else
        PARSE_VERDICT="PASS"
      fi
    else
      PARSE_VERDICT="FAIL"
      echo "--- full release: stopping before paid discovery because parse failed. ---"
    fi
    if [[ "$PARSE_VERDICT" == "PASS" || "$PARSE_VERDICT" == "PLANNED" ]]; then
      if run_discover_gate; then
        if [[ $DRY_RUN -eq 1 ]]; then
          DISCOVER_VERDICT="PLANNED"
        else
          DISCOVER_VERDICT="PASS"
        fi
      else
        DISCOVER_VERDICT="FAIL"
      fi
    fi
    ;;
esac

echo
echo "=================================================="
echo " mode:     $MODE"
echo " parse:    $PARSE_VERDICT"
echo " discover: $DISCOVER_VERDICT"
if [[ $DRY_RUN -eq 1 ]]; then
  echo " VERDICT: DRY-RUN (no API calls)"
  echo "=================================================="
  exit 0
fi
if [[ "$PARSE_VERDICT" != "FAIL" && "$DISCOVER_VERDICT" != "FAIL" ]]; then
  echo " VERDICT: PASS"
  echo "=================================================="
  exit 0
fi
echo " VERDICT: FAIL"
echo "=================================================="
exit 1
