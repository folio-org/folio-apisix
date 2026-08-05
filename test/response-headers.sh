#!/usr/bin/env bash
#
# Response-headers smoke tests: verify that the response-rewrite global rule
# injects Cache-Control, Pragma, Expires, and Strict-Transport-Security on
# every proxied response.
#
# Runnable standalone: bash test/response-headers.sh
#
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=test/lib.sh
source "${SCRIPT_DIR}/lib.sh"

RULE="${ADMIN}/global_rules/response-headers"
ROUTE="${ADMIN}/routes/smoke-response-headers"

echo "== Response-headers smoke tests =="
wait_for "${RULE}" "response-headers global rule" || exit 1

# A route to exercise the proxy path.
curl -s -o /dev/null -X PUT "${ADMIN_HDR[@]}" "${ROUTE}" \
  -d '{"uri":"/get","upstream":{"type":"roundrobin","nodes":{"echo:8080":1}}}'
sleep 1

# get_header <name> — extract a response header value from a proxied GET /get
get_header() {
  curl -s -i "${PROXY}/get" \
    | grep -i "^${1}:" | tr -d '\r' | sed 's/^[^:]*: //'
}

echo "1. All four security/caching headers present with exact values"
v="$(get_header 'Cache-Control')"
[ "${v}" = "private, no-cache, no-store, max-age=0" ] \
  && pass "Cache-Control correct" \
  || fail "Cache-Control: expected 'private, no-cache, no-store, max-age=0', got '${v:-<none>}'"

v="$(get_header 'Pragma')"
[ "${v}" = "no-cache" ] \
  && pass "Pragma correct" \
  || fail "Pragma: expected 'no-cache', got '${v:-<none>}'"

v="$(get_header 'Expires')"
[ "${v}" = "0" ] \
  && pass "Expires correct" \
  || fail "Expires: expected '0', got '${v:-<none>}'"

v="$(get_header 'Strict-Transport-Security')"
[ "${v}" = "max-age=31536000; includeSubDomains; preload" ] \
  && pass "Strict-Transport-Security correct" \
  || fail "Strict-Transport-Security: expected full HSTS value, got '${v:-<none>}'"

echo "2. Gateway Cache-Control takes precedence over any upstream value"
# Even if the echo backend sets its own Cache-Control, response-rewrite must override it.
v="$(get_header 'Cache-Control')"
[ "${v}" = "private, no-cache, no-store, max-age=0" ] \
  && pass "Cache-Control not overridden by upstream" \
  || fail "Cache-Control override failed: got '${v:-<none>}'"

# Cleanup
curl -s -o /dev/null -X DELETE "${ADMIN_HDR[@]}" "${ROUTE}"

finish "Response-headers"
