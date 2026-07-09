#!/usr/bin/env bash
#
# Basic smoke tests: Admin API availability / auth and proxy routing.
# Runnable standalone: bash test/basic.sh
#
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=test/lib.sh
source "${SCRIPT_DIR}/lib.sh"

echo "== Basic smoke tests =="
wait_for "${ADMIN}/routes" "Admin API" || exit 1

# 1. Admin API reachable with the admin key
code=$(curl -s -o /dev/null -w '%{http_code}' "${ADMIN_HDR[@]}" "${ADMIN}/routes")
[ "${code}" = "200" ] && pass "Admin API responds 200 with key" \
  || fail "Admin API returned ${code} with key"

# 2. Admin API rejects requests without the key
code=$(curl -s -o /dev/null -w '%{http_code}' "${ADMIN}/routes")
[ "${code}" = "401" ] && pass "Admin API rejects request without key (401)" \
  || fail "Admin API without key returned ${code}, expected 401"

# 3. A route proxies to the echo backend and returns 200
route_code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "${ADMIN_HDR[@]}" \
  "${ADMIN}/routes/smoke-basic" \
  -d '{"uri":"/get","upstream":{"type":"roundrobin","nodes":{"echo:8080":1}}}')
{ [ "${route_code}" = "200" ] || [ "${route_code}" = "201" ]; } \
  && pass "Created smoke-basic route (${route_code})" \
  || fail "Failed to create route (${route_code})"

proxy_code=$(curl -s -o /dev/null -w '%{http_code}' "${PROXY}/get")
[ "${proxy_code}" = "200" ] && pass "Proxied request returns 200" \
  || fail "Proxied request returned ${proxy_code}, expected 200"

# Cleanup
curl -s -o /dev/null -X DELETE "${ADMIN_HDR[@]}" "${ADMIN}/routes/smoke-basic"

finish "Basic"
