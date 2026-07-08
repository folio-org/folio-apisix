#!/usr/bin/env bash
set -e

FOLIO_CONFIG_PATH="/opt/apisix/folio-config"
ADMIN_API_KEY="${APISIX_ADMIN_KEY}"
ADMIN_API_PORT="${APISIX_ADMIN_API_PORT:-9180}"

# Initialize APISIX config and start as daemon for the configuration phase
apisix start

# Wait for Admin API to become ready
until curl -s -o /dev/null -f \
  -H "X-API-KEY: ${ADMIN_API_KEY}" \
  "http://localhost:${ADMIN_API_PORT}/apisix/admin/routes"; do
  echo "Waiting for APISIX Admin API..."
  sleep 1
done

echo "APISIX initialization..."

export ADC_BACKEND=apisix
export ADC_SERVER="http://localhost:${ADMIN_API_PORT}"
export ADC_TOKEN="${ADMIN_API_KEY}"

# Convert CORS_ORIGINS (space-separated URLs/regexes) to comma-separated for the APISIX CORS plugin.
# When allow_credential is true, * is not accepted by browsers, so we need explicit origins.
echo "CORS setup: CORS_ORIGINS='${CORS_ORIGINS:-}'"
if [ -n "${CORS_ORIGINS:-}" ]; then
  echo "CORS setup: CORS_ORIGINS is set — building allow_origins from provided values..."
  apisix_cors_origins=$(echo "${CORS_ORIGINS}" | tr ' ' ',')
  allow_credential=true
else
  echo "CORS setup: CORS_ORIGINS is unset — using default wildcard origin"
  apisix_cors_origins="*"
  allow_credential=false
fi
export ADC_CORS_ORIGINS="${apisix_cors_origins}"
export ADC_ALLOW_CREDENTIAL="${allow_credential}"
echo "CORS setup: ADC_CORS_ORIGINS=${ADC_CORS_ORIGINS}"

# Substitute env vars into the CORS template and sync via ADC
envsubst '${ADC_CORS_ORIGINS} ${ADC_ALLOW_CREDENTIAL}' \
  < "${FOLIO_CONFIG_PATH}/cors.yaml" \
  > /tmp/folio-cors.yaml
adc sync -f /tmp/folio-cors.yaml

echo "APISIX initialization finished successfully!"

# Stop the initialization instance
apisix stop

# Hand off to the official docker-entrypoint.sh which re-runs init and starts
# OpenResty in foreground (daemon off) as the container main process
exec /docker-entrypoint.sh "$@"
