#!/usr/bin/env bash
#
# Shared configuration and helpers for the folio-apisix smoke tests.
# Sourced by the individual suites (basic.sh, cors.sh); not run directly.
#
# Connection settings default to the docker-compose stack and can be overridden
# via the same environment variables the container uses.
#
HOST="${APISIX_HOST:-localhost}"
ADMIN_PORT="${APISIX_ADMIN_API_PORT:-9180}"
PROXY_PORT="${APISIX_NODE_LISTEN:-9080}"
ADMIN_KEY="${APISIX_ADMIN_KEY:-edd1c9f034335f136f87ad84b625c8f1}"

ADMIN="http://${HOST}:${ADMIN_PORT}/apisix/admin"
PROXY="http://${HOST}:${PROXY_PORT}"
ADMIN_HDR=(-H "X-API-KEY: ${ADMIN_KEY}")

WAIT_TIMEOUT="${WAIT_TIMEOUT:-60}"

FAILURES=0
pass() { echo "  PASS: $*"; }
fail() { echo "  FAIL: $*"; FAILURES=$((FAILURES + 1)); }

# wait_for <url> [description] — poll an Admin API URL until it returns 2xx,
# so a suite can run standalone right after `docker compose up -d`.
wait_for() {
  local url="$1" desc="${2:-$1}" i
  for ((i = 0; i < WAIT_TIMEOUT; i++)); do
    if curl -sf -o /dev/null "${ADMIN_HDR[@]}" "${url}"; then
      return 0
    fi
    sleep 1
  done
  echo "  FAIL: timed out after ${WAIT_TIMEOUT}s waiting for ${desc}"
  return 1
}

# finish <suite-name> — print a summary and exit with a suite-appropriate code.
finish() {
  echo
  if [ "${FAILURES}" -eq 0 ]; then
    echo "${1:-Suite}: all tests passed."
    exit 0
  fi
  echo "${1:-Suite}: ${FAILURES} test(s) failed."
  exit 1
}
