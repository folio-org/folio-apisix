#!/usr/bin/env bash
#
# /version route smoke tests: verify the APISIX control API is proxied correctly,
# the hostname field is stripped from the response body, and security headers are set.
#
# Runnable standalone: bash test/version.sh
#
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=test/lib.sh
source "${SCRIPT_DIR}/lib.sh"

ROUTE="${ADMIN}/routes/version"

echo "== /version route smoke tests =="
wait_for "${ROUTE}" "/version route" || exit 1

echo "1. GET /version returns 200 and body contains version key"
body=$(curl -s "${PROXY}/version")
code=$(curl -s -o /dev/null -w '%{http_code}' "${PROXY}/version")
[ "${code}" = "200" ] \
  && pass "GET /version returns 200" \
  || fail "GET /version: expected 200, got ${code}"
echo "${body}" | grep -q '"version"' \
  && pass "response body contains version key" \
  || fail "response body missing version key: ${body}"

echo "2. hostname is stripped from response body"
echo "${body}" | grep -q '"hostname"' \
  && fail "hostname was not stripped from response body" \
  || pass "hostname stripped from response body"

echo "3. Cache-Control security header is set"
header=$(curl -s -o /dev/null -D - "${PROXY}/version" | grep -i '^cache-control:')
echo "${header}" | grep -q 'private, no-cache, no-store, max-age=0' \
  && pass "Cache-Control header correct" \
  || fail "Cache-Control header wrong or missing: ${header}"

echo "4. Strict-Transport-Security header is set"
header=$(curl -s -o /dev/null -D - "${PROXY}/version" | grep -i '^strict-transport-security:')
echo "${header}" | grep -q 'max-age=31536000; includeSubDomains; preload' \
  && pass "Strict-Transport-Security header correct" \
  || fail "Strict-Transport-Security header wrong or missing: ${header}"

echo "5. POST /version returns 404 (route is GET-only)"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${PROXY}/version")
[ "${code}" = "404" ] \
  && pass "POST /version returns 404" \
  || fail "POST /version: expected 404, got ${code}"

finish "version"
