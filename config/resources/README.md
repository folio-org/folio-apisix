# Declarative Admin API resources

Files here are applied to the running gateway at startup via the APISIX Admin
API (idempotent PUTs), by `entrypoint.sh`.

## Layout

```
resources/<type>/<id>.json
```

Each file is `PUT` to `/apisix/admin/<type>/<id>`, where:

- `<type>` is the Admin API resource type (`routes`, `services`, `upstreams`,
  `consumers`, `global_rules`, `plugin_metadata`, `ssls`, ...).
- `<id>` (the file name without `.json`) is the resource key — an id, or a
  consumer username / plugin name where that type is keyed by name.

Example — a route to a backend service:

```
resources/routes/example.json
```
```json
{
  "uri": "/example/*",
  "upstream": { "type": "roundrobin", "nodes": { "example-svc:8080": 1 } }
}
```

## Notes

- PUTs only affect the resources they name; anything registered at runtime is
  left untouched. Removing a file here does not delete the resource from etcd.
- CORS is **not** configured here — it is generated from `CORS_ORIGINS` and
  applied by the entrypoint so it always wins.
- To reference environment variables inside a file, add them to
  `FOLIO_SUBST_VARS` in `entrypoint.sh` (they are expanded with `envsubst`).
