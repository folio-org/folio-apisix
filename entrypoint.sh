#!/usr/bin/env bash
#
# folio-apisix container entrypoint.
#
# Model:
#   * The OFFICIAL APISIX entrypoint runs the gateway as PID 1 (foreground
#     OpenResty, correct signal handling / graceful shutdown).
#   * In parallel, once the live Admin API is ready, FOLIO configuration is
#     applied against the running gateway and then verified.
#   * If configuration fails, the container is brought down loudly (SIGTERM to
#     PID 1) so the restart policy surfaces the problem instead of serving a
#     mis-configured gateway. Fail closed, not silently degraded.
#
# All configuration is applied through the APISIX Admin API (idempotent PUTs):
#   * CORS is generated from CORS_ORIGINS and PUT to /global_rules/cors.
#   * Any resource files under ${FOLIO_CONFIG_PATH}/resources/<type>/<id>.json
#     are PUT to /apisix/admin/<type>/<id>. Drop a file in to add config.
# PUTs only touch the resources they name, so anything registered at runtime
# (e.g. module routes) is left untouched.
#
set -euo pipefail

FOLIO_CONFIG_PATH="/opt/apisix/folio-config"
ADMIN_API_KEY="${APISIX_ADMIN_KEY}"
ADMIN_API_PORT="${APISIX_ADMIN_API_PORT:-9180}"
ADMIN_API_BASE="http://localhost:${ADMIN_API_PORT}/apisix/admin"

# CORS policy body (methods, headers, credentials, max_age, default origin).
# Only the allowed origins are injected from CORS_ORIGINS; everything else is
# owned by this file so it can be tuned without touching the entrypoint.
CORS_TEMPLATE="${FOLIO_CONFIG_PATH}/cors.json"

# Variables resource files may reference. When non-empty, envsubst expands ONLY
# these, leaving any other `$...` untouched. Extend as templated vars appear.
FOLIO_SUBST_VARS=''

log()   { echo "[folio-apisix init] $*" >&2; }
fatal() { echo "[folio-apisix init] FATAL: $*" >&2; kill -s TERM 1 2>/dev/null || true; exit 1; }

# Optional templating of a resource file (stdin -> stdout).
render() { if [ -n "${FOLIO_SUBST_VARS}" ]; then envsubst "${FOLIO_SUBST_VARS}"; else cat; fi; }

# PUT a JSON body to an Admin API endpoint, failing loudly on a non-2xx.
admin_put() { # <path> ; body on stdin
  local path="$1" code
  code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT \
    -H "X-API-KEY: ${ADMIN_API_KEY}" "${ADMIN_API_BASE}/${path}" --data-binary @-)
  { [ "${code}" = "200" ] || [ "${code}" = "201" ]; } \
    || fatal "PUT ${path} failed (HTTP ${code})."
}

# --- Wait for the gateway's Admin API (started by the official entrypoint) ---
wait_for_admin() {
  log "Waiting for Admin API on :${ADMIN_API_PORT}..."
  local i
  for i in $(seq 1 60); do
    if curl -sf -o /dev/null -H "X-API-KEY: ${ADMIN_API_KEY}" "${ADMIN_API_BASE}/routes"; then
      log "Admin API is ready."
      return 0
    fi
    sleep 1
  done
  fatal "Admin API did not become ready within 60s."
}

# --- CORS global rule -------------------------------------------------------
# The rule body lives in ${CORS_TEMPLATE}; CORS_ORIGINS supplies only the values
# for its (static) allow_origins_by_regex array, injected via envsubst.
# CORS_ORIGINS is a space-separated list of regex patterns matched against the
# request Origin. Unset or "*" means "any origin" and becomes the ".*" regex.
apply_cors() {
  [ -f "${CORS_TEMPLATE}" ] || fatal "CORS template not found: ${CORS_TEMPLATE}"
  local origins="${CORS_ORIGINS:-*}"
  [ "${origins}" = "*" ] && origins=".*"
  # Split the space-separated patterns into an array WITHOUT pathname expansion:
  # an unquoted expansion would let regex metacharacters (e.g. ".*") glob-match
  # files in the working directory instead of being passed through literally.
  local -a patterns
  IFS=' ' read -r -a patterns <<< "${origins}"
  # JSON-encode each regex value and comma-join for the template's array.
  export CORS_ALLOW_ORIGINS
  CORS_ALLOW_ORIGINS=$(jq -rn --args '[$ARGS.positional[] | @json] | join(", ")' "${patterns[@]}")
  log "CORS: allow_origins_by_regex = [${CORS_ALLOW_ORIGINS}]"
  envsubst '${CORS_ALLOW_ORIGINS}' < "${CORS_TEMPLATE}" | admin_put "global_rules/cors"
}

# --- Static resources: resources/<type>/<id>.json -> PUT /<type>/<id> --------
apply_resources() {
  local base="${FOLIO_CONFIG_PATH}/resources"
  [ -d "${base}" ] || return 0
  shopt -s nullglob
  local f type id
  for f in "${base}"/*/*.json; do
    type="$(basename "$(dirname "${f}")")"
    id="$(basename "${f}" .json)"
    render < "${f}" | admin_put "${type}/${id}"
    log "Applied ${type}/${id}"
  done
  shopt -u nullglob
}

# --- Post-conditions: assert critical resources actually landed -------------
verify_config() {
  curl -sf -o /dev/null -H "X-API-KEY: ${ADMIN_API_KEY}" "${ADMIN_API_BASE}/global_rules/cors" \
    || fatal "CORS global rule missing after configuration — refusing to serve."
  log "Verified: CORS global rule present."
}

configure() {
  set -eE
  trap 'fatal "unexpected error during configuration"' ERR
  wait_for_admin
  apply_resources
  apply_cors
  verify_config
  log "APISIX initialization finished successfully"
}

# ---------------------------------------------------------------------------
# Configure the live gateway in the background so it can run against the Admin
# API that the official entrypoint is about to bring up.
configure &

# Hand PID 1 to the official entrypoint: it initialises etcd/nginx and runs
# OpenResty in the foreground as the container's main process.
exec /docker-entrypoint.sh "$@"
