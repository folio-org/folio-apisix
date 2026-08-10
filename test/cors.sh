#!/usr/bin/env bash
#
# CORS smoke tests: verify origin matching for the folio CORS rule across
# wildcard, single-regex and several-regex configurations, each with a passing
# and a failing origin.
#
# Origins are configured as allow_origins_by_regex patterns (the shape the
# entrypoint produces from CORS_ORIGINS). A matching origin is echoed back in
# Access-Control-Allow-Origin; a non-matching origin gets no such header.
# The ".*" wildcard is restored at the end.
#
# Runnable standalone: bash test/cors.sh
#
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=test/lib.sh
source "${SCRIPT_DIR}/lib.sh"

ROUTE="${ADMIN}/routes/smoke-cors"
RULE="${ADMIN}/global_rules/cors"
CORS_METHODS="GET,PUT,POST,DELETE,PATCH"

echo "== CORS smoke tests =="
# CORS is applied asynchronously after the gateway is up.
wait_for "${RULE}" "CORS global rule" || exit 1

# A route to exercise the proxy path.
curl -s -o /dev/null -X PUT "${ADMIN_HDR[@]}" "${ROUTE}" \
  -d '{"uri":"/get","upstream":{"type":"roundrobin","nodes":{"echo:8080":1}}}'

# set_cors <regex...> — configure allow_origins_by_regex with the given patterns.
set_cors() {
  jq -nc --arg m "${CORS_METHODS}" --args \
    '{plugins:{cors:{allow_origins_by_regex:$ARGS.positional,allow_methods:$m,allow_headers:"*",allow_credential:false,max_age:5}}}' \
    "$@" | curl -s -o /dev/null -X PUT "${ADMIN_HDR[@]}" "${RULE}" -d @-
  sleep 2  # let the workers pick up the new global rule
}

# Access-Control-Allow-Origin returned for a given request Origin.
acao()  { curl -s -i -H "Origin: $1" "${PROXY}/get" | grep -i '^access-control-allow-origin:' | tr -d '\r' | awk '{print $2}'; }
allow() { local v; v="$(acao "$1")"; [ "${v}" = "$1" ] && pass "allow $1" || fail "expected allow $1 (ACAO='${v:-<none>}')"; }
block() { local v; v="$(acao "$1")"; [ -z "${v}" ]     && pass "block $1" || fail "expected block $1 (ACAO='${v}')"; }

# set_cors_exact <origin> — configure allow_origins (exact string) + allow_credential:true.
# APISIX does not support allow_credential:true with allow_origins_by_regex.
set_cors_exact() {
  jq -nc --arg m "${CORS_METHODS}" --arg o "$1" \
    '{plugins:{cors:{allow_origins:$o,allow_methods:$m,allow_headers:"**",allow_credential:true,max_age:5}}}' \
    | curl -s -o /dev/null -X PUT "${ADMIN_HDR[@]}" "${RULE}" -d @-
  sleep 2
}

# Access-Control-Allow-Credentials returned for an OPTIONS preflight from a given origin.
acac()      { curl -s -i -X OPTIONS -H "Origin: $1" -H "Access-Control-Request-Method: GET" "${PROXY}/get" \
              | grep -i '^access-control-allow-credentials:' | tr -d '\r' | awk '{print $2}'; }
cred_ok()   { local v; v="$(acac "$1")"; [ "${v}" = "true" ] && pass "credentials $1" \
              || fail "expected credentials $1 (ACAC='${v:-<none>}')"; }
cred_none() { local v; v="$(acac "$1")"; [ -z "${v}" ]       && pass "no-cred $1" \
              || fail "expected no-cred $1 (ACAC='${v}')"; }

echo "0. Wildcard (.* — any origin)"
set_cors '.*'
allow "https://anything.example.com"
allow "http://plain.local"

echo "1. Single origin (^https://good\\.example\\.org\$)"
set_cors '^https://good\.example\.org$'
allow "https://good.example.org"           # exact match -> pass
block "https://good.example.org.evil.com"  # suffix attack -> fail
block "https://bad.example.org"            # different host -> fail

echo "2. Regex (^https://.*\\.folio\\.org\$)"
set_cors '^https://.*\.folio\.org$'
allow "https://team.folio.org"             # matches -> pass
allow "https://a.b.folio.org"              # nested subdomain -> pass
block "https://team.folio.org.evil.com"    # anchored -> fail
block "https://notfolio.org"               # -> fail

echo "3. Several regex values"
set_cors '^https://exact\.example\.com$' '^https://.*\.re\.example\.com$'
allow "https://exact.example.com"          # first pattern -> pass
allow "https://svc.re.example.com"         # second pattern -> pass
block "https://exact.example.com.evil.com" # -> fail
block "https://svc.re.example.org"         # wrong tld -> fail
block "https://unlisted.com"               # -> fail

echo "4. Credentials (allow_credential: true, exact origin)"
set_cors_exact 'https://good.example.org'
cred_ok   "https://good.example.org"  # matching origin -> Access-Control-Allow-Credentials: true
cred_none "https://bad.example.org"   # non-matching origin -> header absent

# Restore the wildcard default and clean up.
set_cors '.*'
curl -s -o /dev/null -X DELETE "${ADMIN_HDR[@]}" "${ROUTE}"

finish "CORS"
