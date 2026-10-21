# Kong -> APISIX migration

`migrate-route.py` copies **services and routes** from a live Kong Admin API to an APISIX Admin API.
Migrated routes and services are identical to the ones `` creates for the same modules (same
ids, `uri`, `methods`, `vars`, `labels` and upstream), so the library can later update, delete and add tenants to them.
The only difference is the service `retries` and `timeout`, which are copied from Kong, whereas the library leaves them
unset unless configured.

## Configuration

| Variable / flag | Default | Notes |
|---|---|---|
| `KONG_ADMIN_URL` / `--kong-url` | `http://localhost:8001` | |
| `APISIX_ADMIN_URL` / `--apisix-url` | `http://localhost:9180` | |
| `APISIX_ADMIN_KEY` / `--apisix-key` | **required** | pass it with `--apisix-key`; the environment variable is only a fallback |
| `KONG_SKIP_ROUTES` / `--skip-routes` | `version,CORS` | route names not migrated (Kong-internal routes that APISIX provides natively); services used only by skipped routes are skipped too. Empty value = skip nothing |
| `--dry-run` | off | reads and translates only; prints one JSON object per entity (`create` / `update` / `unchanged` / `error`) to stdout with the APISIX `id` and the `kong_id`; route records also include the `service` name |
| `-v` / `--verbose` | off | logs every API call at DEBUG (logs go to stderr, the key is never logged) |

## Runbook

```bash
ARGS="--kong-url http://kong:8001 --apisix-url http://apisix:9180 --apisix-key <admin-key>"

python3 migration/migrate-route.py $ARGS --dry-run > diff.jsonl   # 1. review what would change; nothing is written
python3 migration/migrate-route.py $ARGS 2>&1 | tee migration.log # 2. migrate
python3 migration/migrate-route.py $ARGS                          # 3. re-run: everything "skipped (already exists)"
```

Order is services -> routes. Entities are written with `PUT` using the same ids `folio-integration-apisix` uses, so a
re-run is idempotent: identical entities are skipped, changed ones are updated, and nothing is duplicated. Failures
never abort the run; each is listed in the summary with its name, id and reason.

Exit codes: `0` success, `1` at least one entity failed, `2` fatal (missing `--apisix-key`, APISIX or Kong unreachable).

This is a one-time cutover tool: Kong is the source of truth, so re-running it after tenants were added through
`folio-integration-apisix` overwrites the tenant `vars` of those routes with Kong's state.

## Tests

```bash
python3 -m unittest discover -s migration -v
```

`SampleDataTest` translates the Kong route and service in `migration/test-data/provided/` and compares the result
with the expected entries in `migration/test-data/expected/` (created by `folio-integration-apisix`). The only
expected difference is `retries` and `timeout` on the service, which are copied from Kong.

## Verify after migrating

1. Compare the summary counts with Kong (`routes`, `services`) and `GET /apisix/admin/<type>` totals.
2. Send one request per route type through the gateway (path with and without a query string, right and wrong
   method, with and without `X-Okapi-Tenant`).
3. Re-run the script; the summary must show `migrated=0 updated=0` for every type.
