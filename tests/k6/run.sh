#!/usr/bin/env bash
set -euo pipefail

SCENARIOS_DIR="$(dirname "$0")/scenarios"

usage() {
  echo "Usage: $0 <suite>"
  echo ""
  echo "Suites:"
  echo "  smoke     - All smoke tests (~2m each)"
  echo "  load      - All load tests (~12-23m each)"
  echo "  stress    - Redirect stress test (~25m)"
  echo "  spike     - Redirect spike test (~18m)"
  echo "  soak      - Redirect soak test (~1h)"
  echo "  redirect  - All redirect tests (smoke + load + stress + spike)"
  echo "  api       - API smoke + load"
  echo "  all       - Everything (takes a while)"
  echo ""
  echo "Or run individual scenarios:"
  echo "  $0 scenarios/redirect-smoke.js"
  exit 1
}

run() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Running: $1"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  k6 run "$SCENARIOS_DIR/$1"
}

[ $# -eq 0 ] && usage

case "$1" in
  smoke)
    run redirect-smoke.js
    run api-smoke.js
    run auth-smoke.js
    run legacy-smoke.js
    ;;
  load)
    run redirect-load.js
    run api-load.js
    run legacy-load.js
    ;;
  stress)
    run redirect-stress.js
    ;;
  spike)
    run redirect-spike.js
    ;;
  soak)
    run redirect-soak.js
    ;;
  redirect)
    run redirect-smoke.js
    run redirect-load.js
    run redirect-stress.js
    run redirect-spike.js
    ;;
  api)
    run api-smoke.js
    run api-load.js
    ;;
  all)
    run redirect-smoke.js
    run api-smoke.js
    run auth-smoke.js
    run legacy-smoke.js
    run redirect-load.js
    run api-load.js
    run legacy-load.js
    run redirect-stress.js
    run redirect-spike.js
    run mixed-realistic.js
    ;;
  *)
    # Run a specific scenario file
    if [ -f "$SCENARIOS_DIR/$1" ]; then
      run "$1"
    elif [ -f "$1" ]; then
      k6 run "$1"
    else
      echo "Unknown suite or file: $1"
      usage
    fi
    ;;
esac

echo ""
echo "Done."
