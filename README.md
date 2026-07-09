# APISIX

## Table of Contents

- [Introduction](#introduction)
- [Version](#version)
- [Environment Variables](#environment-variables)
- [Ports](#ports)
- [CORS configuration](#cors-configuration)
- [Declarative resources](#declarative-resources)
- [Local development](#local-development)
- [Tests](#tests)

## Introduction

`folio-apisix` is a FOLIO-customized [Apache APISIX](https://apisix.apache.org/) API gateway image. It builds on the
upstream `apache/apisix` image and adds:

- a FOLIO default `config.yaml` (etcd-backed, Admin API enabled),
- a startup routine that configures the running gateway through the Admin API (CORS plus any declarative resources you
  ship),
- a local `docker-compose` stack (etcd + gateway + an echo backend) and smoke tests for development.

Configuration is applied at container start by [`entrypoint.sh`](entrypoint.sh): the official APISIX entrypoint runs the
gateway as PID 1, and in parallel — once the Admin API is up — FOLIO configuration is PUT to the Admin API and verified.
If that configuration fails, the container exits (rather than serving a mis-configured gateway), so a bad config
surfaces as a restart/crash-loop.

## Version

The major and minor version of folio-apisix matches the major and minor version of the apisix container it is based on.

The patch version of folio-apisix starts at 0 and gets incremented for each release.

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `APISIX_NODE_LISTEN` | `9080` | Proxy (data-plane) listen port. |
| `APISIX_ADMIN_API_PORT` | `9180` | Admin API listen port. |
| `APISIX_ADMIN_KEY` | _required_ | Admin API key with the `admin` role. A development value is provided by `docker-compose.yaml`; set your own in real deployments. |
| `APISIX_VIEWER_KEY` | `4054f7cf07e344346cd3f287985e76a2` | Admin API key with the read-only `viewer` role. |
| `CORS_ORIGINS` | _empty_ (`.*`) | Space-separated regex patterns for allowed CORS origins (see below). |

## Ports

| Port | Purpose |
| --- | --- |
| `9080` | Proxy (HTTP data plane) |
| `9443` | Proxy (HTTPS data plane) |
| `9180` | Admin API |
| `9091` | Prometheus metrics |
| `9092` | Control API |

## CORS configuration

CORS is applied as an APISIX `global_rules` entry named `cors`. The rule body lives in
[`config/cors.json`](config/cors.json) (methods, headers, credentials, `max_age`); only the allowed origins come from
the environment.

`CORS_ORIGINS` is a space-separated list of **regex patterns** matched against the request `Origin` header (APISIX
`allow_origins_by_regex`):

- Unset or `*` means any origin, i.e. the `.*` pattern.
- Each value is a raw regex, so a specific origin is given as an anchored pattern, e.g. `^https://app\.demo\.org$`.

Examples:

```sh
# any origin (default)
CORS_ORIGINS=

# a single origin
CORS_ORIGINS='^https://app\.demo\.org$'

# several patterns
CORS_ORIGINS='^https://.*\.folio\.org$ ^https://app\.demo\.org$'
```

A matching origin is echoed back in `Access-Control-Allow-Origin`; a non-matching origin receives no such header.
Credentials are disabled, so `allow_headers` may remain `*`. To change methods, headers, or `max_age`, edit
`config/cors.json`.

> **Why all origins are regex.** The APISIX `cors` plugin ignores the exact `allow_origins` field entirely once
> `allow_origins_by_regex` is set — the two are not combined into a union. To keep a single, predictable matching path,
> every configured origin (including the `*` / unset default, which becomes `.*`) is expressed as an
> `allow_origins_by_regex` pattern; `allow_origins` is not used.

## Declarative resources

Any other resources are shipped as files under [`config/resources/`](config/resources/) as `resources/<type>/<id>.json`
and are PUT to `/apisix/admin/<type>/<id>` at startup (idempotent). See
[`config/resources/README.md`](config/resources/README.md). PUTs only touch the resources they name, so anything
registered at runtime is left untouched.

> **Why the Admin API and not ADC.** [ADC](https://github.com/api7/adc) (the declarative-config CLI) was evaluated for
> applying configuration but dropped. When syncing the CORS `global_rule`, ADC intermittently reported success while
> storing an empty `cors` plugin, leaving the gateway effectively wide open; the same payload applied with a direct
> Admin API `PUT` was reliable every time. To keep configuration deterministic and verifiable, all configuration —
> CORS and declarative resources alike — is applied through idempotent Admin API PUTs, and ADC is not installed in the
> image.

## Local development

```sh
docker compose up -d --build
```

This starts etcd, the gateway, and an echo backend on the ports listed above. The Admin API is reachable on
`localhost:9180` (send the `X-API-KEY` header) and the proxy on `localhost:9080`.

Set `CORS_ORIGINS` before starting to exercise restricted CORS:

```sh
CORS_ORIGINS='^https://.*\.folio\.org$' docker compose up -d --build
```

## Tests

Smoke tests live in [`test/`](test) and run against a running stack:

```sh
docker compose up -d --build
bash test/test.sh
```

`test/test.sh` runs every suite; each is also runnable on its own (`bash test/basic.sh`, `bash test/cors.sh`).
`basic.sh` checks Admin API auth and proxy routing; `cors.sh` verifies origin matching (wildcard, single regex, several
regex) with passing and failing origins.
