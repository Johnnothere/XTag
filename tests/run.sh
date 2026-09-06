#!/usr/bin/env bash
# Run every XTag test suite. From the repo root:  bash tests/run.sh
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"

# `timeout` is GNU coreutils and is NOT present on macOS, where this repo is
# developed. Without this, every suite ran as "timeout: command not found",
# produced no output, and was silently counted as zero checks — the runner
# reported a clean pass over a suite it had never executed, which is the worst
# possible failure mode for a test runner.
if command -v timeout >/dev/null 2>&1;  then TO="timeout 600"
elif command -v gtimeout >/dev/null 2>&1; then TO="gtimeout 600"
else TO=""; echo "  (no timeout command available — running suites unbounded)"; fi

tot=0; fail=0; missing=0
for t in tests/test_*.py; do
  out=$($TO python3 "$t" 2>&1)
  line=$(echo "$out" | grep -E "^  [0-9]+ passed")
  n=$(echo "$line" | grep -oE "^  [0-9]+" | tr -d ' ')
  f=$(echo "$line" | grep -oE "[0-9]+ failed" | grep -oE "^[0-9]+")
  tot=$((tot+${n:-0})); fail=$((fail+${f:-0}))
  if [ -z "$line" ]; then
    missing=$((missing+1))
    printf "  %-30s NO RESULT — output follows\n" "$(basename "$t")"
    echo "$out" | tail -25 | sed 's/^/      /'
  else
    printf "  %-30s %s\n" "$(basename "$t")" "$line"
  fi
done

if command -v node >/dev/null 2>&1; then
  for m in tests/test_*.mjs; do
    line=$(node "$m" 2>&1 | grep -E "^  [0-9]+ passed")
    n=$(echo "$line" | grep -oE "^  [0-9]+" | tr -d ' ')
    f=$(echo "$line" | grep -oE "[0-9]+ failed" | grep -oE "^[0-9]+")
    tot=$((tot+${n:-0})); fail=$((fail+${f:-0}))
    if [ -z "$line" ]; then
      missing=$((missing+1))
      printf "  %-30s NO RESULT\n" "$(basename "$m")"
    else
      printf "  %-30s %s\n" "$(basename "$m")" "$line"
    fi
  done
else
  echo "  (node not installed — .mjs suites skipped)"
fi

echo "  ----------------------------------------------------"
printf "  TOTAL: %d checks, %d failed" "$tot" "$fail"
[ "$missing" -gt 0 ] && printf ", %d SUITE(S) PRODUCED NO RESULT" "$missing"
echo
# A suite that did not run is a failure, not a pass. Exiting 0 here is how the
# missing-timeout bug went unnoticed in the first place.
[ "$fail" -eq 0 ] && [ "$missing" -eq 0 ]
