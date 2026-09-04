#!/usr/bin/env bash
# Run every XTag test suite. From the repo root:  bash tests/run.sh
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
tot=0; fail=0
for t in tests/test_*.py; do
  out=$(timeout 600 python3 "$t" 2>&1)
  line=$(echo "$out" | grep -E "^  [0-9]+ passed")
  n=$(echo "$line" | grep -oE "^  [0-9]+" | tr -d ' ')
  f=$(echo "$line" | grep -oE "[0-9]+ failed" | grep -oE "^[0-9]+")
  tot=$((tot+${n:-0})); fail=$((fail+${f:-0}))
  printf "  %-30s %s\n" "$(basename "$t")" "${line:-NO RESULT — see output below}"
  [ -z "$line" ] && echo "$out" | tail -20
done
if command -v node >/dev/null; then
  line=$(node tests/test_sse_parser.mjs 2>&1 | grep -E "^  [0-9]+ passed")
  n=$(echo "$line" | grep -oE "^  [0-9]+" | tr -d ' ')
  f=$(echo "$line" | grep -oE "[0-9]+ failed" | grep -oE "^[0-9]+")
  tot=$((tot+${n:-0})); fail=$((fail+${f:-0}))
  printf "  %-30s %s\n" "test_sse_parser.mjs" "$line"
else
  echo "  test_sse_parser.mjs            SKIPPED (node not installed)"
fi
echo "  ----------------------------------------------------"
printf "  TOTAL: %d checks, %d failed\n" "$tot" "$fail"
exit $([ "$fail" -eq 0 ] && echo 0 || echo 1)
