# APISIX

## Table of Contents

- [Introduction](#introduction)
- [Version](#version)
- [Base image](#base-image)
- [Environment Variables](#environment-variables)
- [Ports](#ports)
- [CORS configuration](#cors-configuration)
  - [Origin restriction and credentials — two modes](#origin-restriction-and-credentials--two-modes)
  - [Allowed request headers](#allowed-request-headers)
- [Declarative resources](#declarative-resources)
- [Configured Plugins](#configured-plugins)
- [Local development](#local-development)
- [Tests](#tests)

## Introduction

`folio-apisix` is a FOLIO-customized [Apache APISIX](https://apisix.apache.org/) API gateway image. It builds on the
[Docker Hardened Image](https://docs.docker.com/dhi/) build of APISIX (`dhi.io/apisix`) and adds:

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

## Base image

The image is built on [Docker Hardened Images](https://docs.docker.com/dhi/) (DHI) rather than the community
`apache/apisix` image:

- **Runtime:** `dhi.io/apisix:<version>-debian` — the minimal, security-hardened image the gateway runs in.
- **Builder:** `dhi.io/apisix:<version>-debian-dev` — the matching companion with a shell, `apt`, and `root`.

DHI runtime images are deliberately stripped down, which imposes a few constraints:

- **Non-root.** The image runs as `UID 65532`; there is no `root` user, so `USER root` and root-only steps do not work.
- **No package manager.** `apt`/`apt-get` are absent, so packages cannot be installed into the runtime image directly.
- **No general-purpose tooling.** `curl`, `jq`, `envsubst`, `grep`, etc. are not present in the runtime image.
- **Authenticated pulls.** `dhi.io` images require registry credentials (a Docker Hardened Images subscription).

`entrypoint.sh` still needs `curl`, `jq`, and `envsubst` at runtime to configure the gateway through the Admin API.
Since the runtime image cannot install them, the [`Dockerfile`](Dockerfile) uses a multi-stage build to bring them in:

1. The `-dev` builder stage (which has `root` and `apt`) installs `curl`, `jq`, and `gettext-base`.
2. [`docker/collect-tools.sh`](docker/collect-tools.sh) copies each tool plus its shared libraries into a staging root,
   handling Debian usr-merge symlinks (`/lib` → `/usr/lib`) and `SONAME` symlink chains (`libcurl.so.4` → `.so.4.x`).
3. The runtime stage copies that staging root in, making the tools available to the non-root user.

To add another runtime tool, install it in the builder's `apt-get install` line and add it to the `collect-tools.sh`
arguments — it is then copied into the runtime image together with its shared libraries.

> **CI note.** Because `dhi.io` requires authentication, the CI build must be able to pull both the `-debian` and
> `-debian-dev` tags. Configure `dhi.io` registry credentials in the pipeline, otherwise the build fails at `FROM`.

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `APISIX_NODE_LISTEN` | `9080` | Proxy (data-plane) listen port. |
| `APISIX_ADMIN_API_PORT` | `9180` | Admin API listen port. |
| `APISIX_ADMIN_KEY` | _required_ | Admin API key with the `admin` role. A development value is provided by `docker-compose.yaml`; set your own in real deployments. |
| `APISIX_VIEWER_KEY` | `4054f7cf07e344346cd3f287985e76a2` | Admin API key with the read-only `viewer` role. |
| `CORS_ORIGINS` | _empty_ (`.*`) | Space-separated regex patterns for allowed CORS origins (see below). |
| `CORS_ALLOW_ORIGINS_EXACT` | `**` | `allow_origins` value passed to the CORS plugin. `**` allows all origins forcefully (see security warning below). Use comma-separated exact URLs to restrict. |
| `CORS_ALLOW_HEADERS` | `**` | `allow_headers` value. `**` allows all request headers forcefully (see security warning below). Use a comma-separated list to restrict. |
| `CORS_ALLOW_CREDENTIAL` | `true` | `allow_credential` value. When `true` the CORS spec forbids `*` for origins/headers — use `**` (forceful) or explicit values. |

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
[`config/cors.json`](config/cors.json) (methods, `max_age`); origins, headers, and credentials
are controlled via environment variables.

### Origin restriction and credentials — two modes

The APISIX `cors` plugin exposes two origin-matching fields, and which one is active depends on
whether credentials are enabled:

| `CORS_ALLOW_CREDENTIAL` | Active origin variable | How origins are matched |
| --- | --- | --- |
| `true` (default) | `CORS_ALLOW_ORIGINS_EXACT` | Comma-separated exact URLs passed to `allow_origins` |
| `false` | `CORS_ORIGINS` | Space-separated regex patterns passed to `allow_origins_by_regex` |

The other variable is ignored in each mode.

**Mode 1 — credentials enabled (default)**

Use `CORS_ALLOW_ORIGINS_EXACT` to list allowed origins as comma-separated exact URLs.
The default `**` allows all origins (see security warning below).

```sh
# Allow specific origins with credentials (production recommendation)
CORS_ALLOW_CREDENTIAL=true \
  CORS_ALLOW_ORIGINS_EXACT='https://app.folio.org,https://staging.folio.org' \
  docker compose up -d --build
```

**Mode 2 — credentials disabled**

Use `CORS_ORIGINS` to list allowed origins as space-separated regex patterns. Unset means `.*`
(any origin). Each value is a raw regex.

```sh
# Single origin — anchored regex
CORS_ALLOW_CREDENTIAL=false \
  CORS_ORIGINS='^https://app\.demo\.org$' \
  docker compose up -d --build

# Subdomain wildcard
CORS_ALLOW_CREDENTIAL=false \
  CORS_ORIGINS='^https://.*\.folio\.org$' \
  docker compose up -d --build

# Multiple patterns
CORS_ALLOW_CREDENTIAL=false \
  CORS_ORIGINS='^https://.*\.folio\.org$ ^https://app\.demo\.org$' \
  docker compose up -d --build
```

### Allowed request headers

`CORS_ALLOW_HEADERS` is independent of the credential/origin mode. It defaults to `**`
(all headers allowed). Restrict it to a specific list when needed:

```sh
CORS_ALLOW_HEADERS='authorization,content-type,x-okapi-token,x-okapi-tenant' \
  docker compose up -d --build
```

To change allowed methods or `max_age`, edit `config/cors.json` directly.

> **Security warning — `**` and CSRF.** Using `**` for `allow_origins` (via
> `CORS_ALLOW_ORIGINS_EXACT=**`) allows credentials from any origin, making the gateway
> vulnerable to Cross-Site Request Forgery (CSRF). Using `**` for `allow_headers` exposes all
> request headers to cross-origin requests. In production, always set explicit values for both.

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

## Configured Plugins

All plugins are applied at startup via declarative Admin API PUTs — no manual configuration is required.
The table below lists every active plugin, where it is applied, and its purpose.

| Plugin | Scope | Purpose |
| --- | --- | --- |
| [`cors`](#cors-plugin) | `global_rules` | CORS preflight headers |
| [`response-rewrite`](#response-rewrite) | `global_rules` | Cache-Control and HSTS headers |
| [`auth-headers-manager`](#auth-headers-manager) | `global_rules` | Cookie-to-header token promotion |
| [`version-info`](#version-info) | Route `GET /version` | Version endpoint with body sanitization |

---

### cors plugin

**Scope:** `global_rules` — applied to every request  
**Config source:** `config/cors.json` (rendered at startup by `entrypoint.sh`)

Handles browser CORS preflight and response headers. Credentials are enabled by default so
FOLIO's cookie-based auth flow (`folioAccessToken`) works cross-origin. Allowed origins, headers,
and the credential flag are all controlled through environment variables; see
[CORS configuration](#cors-configuration) for full details and examples.

---

### response-rewrite

**Scope:** `global_rules` — applied to every response  
**Config source:** `config/resources/global_rules/response-headers.json`

Injects four response headers on every proxied response, overriding any upstream value:

| Header | Value |
| --- | --- |
| `Cache-Control` | `private, no-cache, no-store, max-age=0` |
| `Pragma` | `no-cache` |
| `Expires` | `0` |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains; preload` |

No environment variables — values are hardcoded in the JSON config file.

---

### auth-headers-manager

**Scope:** `global_rules` — applied to every request  
**Config source:** `config/resources/global_rules/auth-headers-manager.json`  
**Plugin source:** `plugins/auth-headers-manager.lua`

Stripes (the FOLIO frontend) stores the session token in a `folioAccessToken` browser cookie.
Backend services require the token in an HTTP header. This plugin performs that translation:

1. Resolves the effective token from `Authorization: Bearer …`, `X-Okapi-Token`, or the
   `folioAccessToken` cookie (in that priority order). If the same token appears in two sources,
   the values must match; a mismatch returns HTTP 404.
2. When the token came from the cookie and `set_okapi_header: true`: clears `Authorization` and
   sets `X-Okapi-Token` to the token value (Okapi-based deployments).
3. When `clean_access_token_cookie: true`: strips `folioAccessToken` from the `Cookie` header
   forwarded upstream.

**Deployed configuration** (from `config/resources/global_rules/auth-headers-manager.json`):

| Parameter | Value | Description |
| --- | --- | --- |
| `set_okapi_header` | `true` | Promote cookie token to `X-Okapi-Token`. |
| `set_authorization_header` | `false` | Do not promote cookie token to `Authorization: Bearer`. |
| `clean_access_token_cookie` | `true` | Strip `folioAccessToken` from the forwarded `Cookie` header. |

No environment variables — configuration is hardcoded in the JSON resource file. To switch to
Eureka-based deployments (which use `Authorization: Bearer` instead of `X-Okapi-Token`), set
`set_okapi_header: false` and `set_authorization_header: true` in that file.

---

### version-info

**Scope:** Route `GET /version`  
**Config source:** `config/resources/routes/version.json`  
**Plugin source:** `plugins/version-info.lua`

Exposes a public `GET /version` endpoint. The plugin responds directly (without proxying) with
the APISIX version as a JSON object:

```json
{ "version": "<apisix-version>" }
```

The `hostname` field is intentionally omitted to prevent leaking internal server identity.
Security headers (`Cache-Control`, `Pragma`, `Expires`, `Strict-Transport-Security`) are set to
the same values as the global `response-rewrite` rule. Non-GET requests to `/version` are not
matched by this route and receive the gateway's default 404 response.

No configuration knobs or environment variables.

---

## Local development

```sh
docker compose up -d --build
```

This starts etcd, the gateway, and an echo backend on the ports listed above. The Admin API is reachable on
`localhost:9180` (send the `X-API-KEY` header) and the proxy on `localhost:9080`.

Set `CORS_ORIGINS` and disable credentials before starting to exercise restricted CORS:

```sh
CORS_ALLOW_CREDENTIAL=false CORS_ORIGINS='^https://.*\.folio\.org$' docker compose up -d --build
```

## Tests

Smoke tests live in [`test/`](test) and run against a running stack:

```sh
docker compose up -d --build
bash test/test.sh
```

`test/test.sh` runs every suite; each is also runnable on its own (`bash test/basic.sh`, `bash test/cors.sh`).
Each suite can also be run individually:

| Suite | What it tests |
| --- | --- |
| `basic.sh` | Admin API authentication and basic proxy routing |
| `cors.sh` | CORS origin matching (wildcard, single regex, multiple regex, credentials) |
| `response-headers.sh` | Cache-Control, Pragma, Expires, and HSTS headers on every response |
| `auth-headers.sh` | Cookie-to-header promotion, cookie stripping, and mismatch error cases |
| `version.sh` | `GET /version` returns version JSON with security headers; hostname absent; POST rejected |
