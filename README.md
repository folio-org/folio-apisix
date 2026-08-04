# APISIX

## Table of Contents

- [Introduction](#introduction)
- [Version](#version)
- [Base image](#base-image)
- [Environment Variables](#environment-variables)
- [Ports](#ports)
- [CORS configuration](#cors-configuration)
- [Declarative resources](#declarative-resources)
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

To configure credentials and restrict origins or headers, use the following variables:

```sh
# Disable credentials — CORS_ORIGINS regex is fully in control of access
CORS_ALLOW_CREDENTIAL=false CORS_ORIGINS='^https://.*\.folio\.org$' docker compose up -d --build

# Enable credentials for specific origins only (comma-separated exact URLs)
CORS_ALLOW_CREDENTIAL=true \
  CORS_ALLOW_ORIGINS_EXACT='https://app.folio.org,https://staging.folio.org' \
  docker compose up -d --build

# Restrict allowed request headers instead of allowing all (** default)
CORS_ALLOW_HEADERS='authorization,content-type,x-okapi-token,x-okapi-tenant' \
  docker compose up -d --build
```

Credentials are enabled by default (`allow_credential: true`); `allow_origins` and `allow_headers` default to `**`
(forceful allow-all — see security warning below). To change methods or `max_age`, edit `config/cors.json`.

> **Limitation — `CORS_ORIGINS` only applies when credentials are disabled.**
> The APISIX `cors` plugin treats `allow_origins_by_regex` (the field populated by `CORS_ORIGINS`) as a
> no-credentials feature. When `CORS_ALLOW_CREDENTIAL=true` (the default), `CORS_ALLOW_ORIGINS_EXACT` is
> used to control origin access.

> **Security warning — `**` and CSRF.** Using `**` for `allow_origins` allows credentials from any origin, which
> makes the gateway vulnerable to Cross-Site Request Forgery (CSRF). Using `**` for `allow_headers` exposes all
> request headers to cross-origin requests, which can leak sensitive data. Before using `**` for either field,
> ensure this aligns with your deployment's security requirements. In production, prefer explicit comma-separated
> origin URLs via `CORS_ALLOW_ORIGINS_EXACT` and an explicit header list via `CORS_ALLOW_HEADERS`.

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
`basic.sh` checks Admin API auth and proxy routing; `cors.sh` verifies origin matching (wildcard, single regex, several
regex) with passing and failing origins.
