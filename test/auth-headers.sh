#!/usr/bin/env bash
#
# auth-headers-manager smoke tests: verify cookie-to-header promotion,
# cookie stripping, and error cases for invalid/mismatched tokens.
#
# The echo backend (mendhak/http-https-echo) returns a JSON body whose
# "headers" object reflects what the upstream actually received.
#
# Runnable standalone: bash test/auth-headers.sh
#
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=test/lib.sh
source "${SCRIPT_DIR}/lib.sh"

RULE="${ADMIN}/global_rules/auth-headers-manager"
ROUTE="${ADMIN}/routes/smoke-auth-headers"

echo "== auth-headers-manager smoke tests =="
wait_for "${RULE}" "auth-headers-manager global rule" || exit 1

# Route to the echo backend for inspecting upstream-visible headers.
curl -s -o /dev/null -X PUT "${ADMIN_HDR[@]}" "${ROUTE}" \
  -d '{"uri":"/get","upstream":{"type":"roundrobin","nodes":{"echo:8080":1}}}'
sleep 1

# upstream_body <curl-args...> — GET /get through the proxy; returns the echo JSON body.
upstream_body() { curl -s "${PROXY}/get" "$@"; }

echo "1. Cookie promoted to X-Okapi-Token; cookie stripped from upstream"
body=$(upstream_body -H 'Cookie: folioAccessToken=abc.def.ghi')
echo "${body}" | grep -qi '"x-okapi-token"' \
  && pass "X-Okapi-Token set from cookie" \
  || fail "X-Okapi-Token not found in upstream request"
echo "${body}" | grep -qi '"cookie"' \
  && fail "Cookie header was forwarded to upstream (should be stripped)" \
  || pass "Cookie header stripped from upstream"

echo "2. X-Okapi-Token passthrough (no cookie)"
body=$(upstream_body -H 'X-Okapi-Token: abc.def.ghi')
echo "${body}" | grep -qi '"x-okapi-token"' \
  && pass "X-Okapi-Token passed through unchanged" \
  || fail "X-Okapi-Token missing from upstream request"

echo "3. Invalid Authorization format -> 404"
code=$(curl -s -o /dev/null -w '%{http_code}' "${PROXY}/get" \
  -H 'Authorization: Token abc.def.ghi')
[ "${code}" = "404" ] \
  && pass "invalid Authorization format returns 404" \
  || fail "invalid Authorization format: expected 404, got ${code}"

echo "4. Authorization Bearer vs X-Okapi-Token mismatch -> 404"
code=$(curl -s -o /dev/null -w '%{http_code}' "${PROXY}/get" \
  -H 'Authorization: Bearer token-a' \
  -H 'X-Okapi-Token: token-b')
[ "${code}" = "404" ] \
  && pass "Bearer/X-Okapi-Token mismatch returns 404" \
  || fail "Bearer/X-Okapi-Token mismatch: expected 404, got ${code}"

echo "5. Cookie token vs X-Okapi-Token mismatch -> 404"
code=$(curl -s -o /dev/null -w '%{http_code}' "${PROXY}/get" \
  -H 'Cookie: folioAccessToken=token-a' \
  -H 'X-Okapi-Token: token-b')
[ "${code}" = "404" ] \
  && pass "cookie/X-Okapi-Token mismatch returns 404" \
  || fail "cookie/X-Okapi-Token mismatch: expected 404, got ${code}"

# Cleanup
curl -s -o /dev/null -X DELETE "${ADMIN_HDR[@]}" "${ROUTE}"

finish "auth-headers-manager"
