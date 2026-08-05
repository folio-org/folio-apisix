#!/usr/bin/env bash
#
# folio-apisix smoke-test suite.
#
# Runs every test suite and aggregates the results. Assumes the compose stack
# is already up (see .github/workflows/test.yml):
#   docker compose up -d --build && bash test/test.sh
#
# Each suite is also runnable independently, e.g. `bash test/cors.sh`.
#
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SUITES=(basic cors response-headers auth-headers)
failed=0

for suite in "${SUITES[@]}"; do
  echo "################################################################"
  echo "# ${suite}"
  echo "################################################################"
  if bash "${SCRIPT_DIR}/${suite}.sh"; then
    echo "==> ${suite}: OK"
  else
    echo "==> ${suite}: FAILED"
    failed=$((failed + 1))
  fi
  echo
done

echo "================================================================"
if [ "${failed}" -eq 0 ]; then
  echo "All suites passed."
  exit 0
fi
echo "${failed} suite(s) failed."
exit 1
