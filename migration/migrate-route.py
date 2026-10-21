#!/usr/bin/env python3
"""Migrate Kong services and routes to Apache APISIX.

Reads from the Kong Admin API and writes to the APISIX Admin API (idempotent
PUTs keyed by the Kong entity id). Order: services -> routes.
Plugins, consumers and Kong upstreams are intentionally out of scope.

Configuration (flags; the environment variables are only defaults):
  --kong-url / KONG_ADMIN_URL        default http://localhost:8001
  --apisix-url / APISIX_ADMIN_URL    default http://localhost:9180
  --apisix-key / APISIX_ADMIN_KEY    required
  --skip-routes / KONG_SKIP_ROUTES   comma-separated route names not to migrate (default: version,CORS)

Exit codes: 0 success, 1 at least one entity failed, 2 fatal (config/connectivity).
Only the Python standard library is used.
"""
import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("kong2apisix")

PAGE_SIZE = 500
HTTP_TIMEOUT = 30
KINDS = ("services", "routes")
DEFAULT_SKIP_ROUTES = "version,CORS"
SERVICE_PROTOCOLS = ("http", "https")


class TranslationError(Exception):
    """A Kong entity cannot be expressed in APISIX."""


class FatalError(Exception):
    """Configuration or connectivity problem; the run cannot continue."""


class ApiError(Exception):
    def __init__(self, status, detail, method, url):
        self.status = status
        super().__init__(f"{method} {url} failed (HTTP {status or 'n/a'}): {detail}")


# --------------------------------------------------------------------------
# HTTP clients
# --------------------------------------------------------------------------

class ApiClient:
    def __init__(self, base_url, headers=None):
        self.base = base_url.rstrip("/")
        self.headers = dict(headers or {}, Accept="application/json")

    def request(self, method, path, body=None):
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = dict(self.headers, **({"Content-Type": "application/json"} if data else {}))
        LOG.debug("%s %s", method, url)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers),
                                        timeout=HTTP_TIMEOUT) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except OSError as e:
            raise ApiError(None, getattr(e, "reason", e), method, url)
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode("utf-8", "replace")
        LOG.debug("%s %s -> HTTP %s", method, url, status)
        if status >= 400:
            detail = (parsed.get("error_msg") or parsed.get("message")) if isinstance(parsed, dict) else parsed
            raise ApiError(status, detail or parsed, method, url)
        return parsed


class KongClient(ApiClient):
    def list(self, path):
        """Every entity of a collection, following Kong's `next` pagination."""
        items, url = [], f"{path}?size={PAGE_SIZE}"
        while url:
            data = self.request("GET", url) or {}
            items.extend(data.get("data", []))
            url = data.get("next")
            if url and "size=" not in url:
                url += f"{'&' if '?' in url else '?'}size={PAGE_SIZE}"
        return items


class ApisixClient(ApiClient):
    def preflight(self):
        self.request("GET", "/apisix/admin/routes?page_size=1")

    def get(self, kind, eid):
        try:
            body = self.request("GET", f"/apisix/admin/{kind}/{urllib.parse.quote(eid, safe='')}")
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        return body["value"] if isinstance(body, dict) and isinstance(body.get("value"), dict) else body

    def put(self, kind, eid, payload):
        self.request("PUT", f"/apisix/admin/{kind}/{urllib.parse.quote(eid, safe='')}", payload)


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------

# One condition: <field> <op> <string>, where the string is "..." or a raw r#"..."#.
_CONDITION_RE = re.compile(
    r'\s*(http\.[A-Za-z0-9_.\-]+)\s*(==|!=|~)\s*(?:r(#*)"(.*?)"\3|"((?:[^"\\]|\\.)*)")\s*')
_OPERATORS = {"==": "==", "!=": "~=", "~": "~~"}


def _display(entity):
    return entity.get("name") or entity.get("id")


def _map_field(field):
    if field == "http.path":
        return "uri"  # `request_uri` would include the query string
    if field == "http.method":
        return "request_method"
    if field == "http.host":
        return "host"
    if field.startswith("http.headers."):
        return "http_" + field[len("http.headers."):].lower().replace("-", "_")
    raise TranslationError(f"unsupported field '{field}'")


def translate_expression(expression):
    """Kong route expression -> APISIX `vars`.

    Supports `&&`-joined conditions (optionally wrapped in one pair of parentheses) using ==, != and ~.
    Anything else raises TranslationError, so a route is never created with a looser match than the original.
    """
    expr = expression.strip()
    wrapped = expr.startswith("(")
    pos = 1 if wrapped else 0
    variables = []
    while True:
        m = _CONDITION_RE.match(expr, pos)
        if not m:
            raise TranslationError(f"unsupported expression near {expr[pos:pos + 40]!r}")
        field, op, _, raw, quoted = m.groups()
        value = raw if raw is not None else re.sub(r'\\(["\\])', r"\1", quoted)
        variables.append([_map_field(field), _OPERATORS[op], value])
        pos = m.end()
        if not expr.startswith("&&", pos):
            break
        pos += 2
    if expr[pos:] != (")" if wrapped else ""):
        raise TranslationError(f"unsupported expression near {expr[pos:pos + 40]!r}")
    return variables


def translate_service(service):
    protocol = service.get("protocol") or "http"
    if protocol not in SERVICE_PROTOCOLS:
        raise TranslationError(f"unsupported protocol '{protocol}'")
    if not service.get("host"):
        raise TranslationError("service has no host")
    if service.get("path") not in (None, "", "/"):
        raise TranslationError(f"service path prefix '{service['path']}' is not supported")
    port = service.get("port") or (443 if protocol == "https" else 80)
    upstream = {"type": "roundrobin", "scheme": protocol,
                "pass_host": "node",  # Kong's default: send the upstream host, not the client's
                "nodes": {f"{service['host']}:{port}": 1}}
    if service.get("retries") is not None:
        upstream["retries"] = service["retries"]
    timeout = {dst: service[src] / 1000.0
               for src, dst in (("connect_timeout", "connect"), ("write_timeout", "send"), ("read_timeout", "read"))
               if service.get(src) is not None}
    if timeout:
        upstream["timeout"] = timeout
    return {"id": service["id"], "name": _display(service), "upstream": upstream}


def translate_route(route):
    if not route.get("expression"):
        raise TranslationError("only expression routes are supported")
    payload = {"id": route["id"], "name": _display(route), "uris": ["/*"],
               "vars": translate_expression(route["expression"])}
    if isinstance(route.get("priority"), int):
        payload["priority"] = route["priority"]
    return payload


def diff_paths(desired, existing, path=""):
    """Paths where `desired` is not contained in `existing` (APISIX-added defaults are ignored)."""
    if isinstance(desired, dict):
        if not isinstance(existing, dict):
            return [path or "/"]
        out = []
        for key, value in desired.items():
            sub = f"{path}.{key}" if path else key
            out.extend([sub] if key not in existing else diff_paths(value, existing[key], sub))
        return out
    return [] if desired == existing else [path]


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

class Report:
    OUTCOMES = ("migrated", "updated", "skipped", "failed", "excluded")

    def __init__(self):
        self.counts = {k: dict.fromkeys(self.OUTCOMES, 0) for k in KINDS}
        self.failures = []

    def record(self, kind, outcome):
        self.counts[kind][outcome] += 1

    def fail(self, kind, entity, reason):
        self.record(kind, "failed")
        self.failures.append((kind, _display(entity), entity.get("id"), reason))
        LOG.error("%s '%s' (%s) failed: %s", kind, _display(entity), entity.get("id"), reason)

    @property
    def has_failures(self):
        return bool(self.failures)

    def log_summary(self, dry_run):
        LOG.info("Migration summary%s", " (DRY RUN - nothing was written; 'migrated' = would be created)" if dry_run else "")
        for kind in KINDS:
            c = self.counts[kind]
            LOG.info("  %-9s migrated=%d updated=%d skipped(already exists)=%d failed=%d excluded(by config)=%d",
                     kind + ":", c["migrated"], c["updated"], c["skipped"], c["failed"], c["excluded"])
        if self.failures:
            LOG.info("Failed entities (%d):", len(self.failures))
            for kind, name, eid, reason in self.failures:
                LOG.info("  %s '%s' (%s): %s", kind, name, eid, reason)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

class Migrator:
    def __init__(self, kong, apisix, dry_run=False, skip_routes=()):
        self.kong = kong
        self.apisix = apisix
        self.dry_run = dry_run
        self.skip_routes = set(skip_routes)
        self.report = Report()
        self.failed = {k: set() for k in KINDS}
        self.services_by_id = {}

    def run(self):
        try:
            self.apisix.preflight()
        except ApiError as e:
            raise FatalError(f"APISIX Admin API is not usable: {e}")
        try:
            services = self.kong.list("/services")
            routes = self.kong.list("/routes")
        except ApiError as e:
            raise FatalError(f"cannot read from Kong Admin API: {e}")
        LOG.info("Read from Kong: %d services, %d routes", len(services), len(routes))
        self.services_by_id = {s["id"]: s for s in services}

        # Services used only by skipped routes are not migrated either.
        used = lambda rs: {r["service"]["id"] for r in rs if r.get("service")}
        skipped = [r for r in routes if r.get("name") in self.skip_routes]
        excluded = used(skipped) - used(r for r in routes if r.get("name") not in self.skip_routes)

        LOG.info("Migrating services...")
        for s in services:
            if s["id"] in excluded:
                self.report.record("services", "excluded")
            else:
                self._migrate("services", s, lambda s=s: translate_service(s))

        LOG.info("Migrating routes...")
        for r in routes:
            if r.get("name") in self.skip_routes:
                LOG.info("Skipping route '%s' (configured skip list)", r.get("name"))
                self.report.record("routes", "excluded")
            else:
                self._migrate("routes", r, lambda r=r: self._route(r))

        self.report.log_summary(self.dry_run)
        return self.report

    def _route(self, route):
        service_id = (route.get("service") or {}).get("id")
        if not service_id:
            raise TranslationError("route has no service")
        if service_id in self.failed["services"]:
            raise TranslationError(f"dependency failed: service '{self._service_name(service_id)}' could not be migrated")
        payload = translate_route(route)
        payload["service_id"] = service_id
        return payload

    def _service_name(self, service_id):
        service = self.services_by_id.get(service_id)
        return _display(service) if service else service_id

    def _migrate(self, kind, entity, build):
        try:
            self._apply(kind, entity, build())
        except (TranslationError, ApiError) as e:
            self.failed[kind].add(entity["id"])
            self.report.fail(kind, entity, str(e))
            self._emit(kind, entity, "error", reason=str(e))

    def _apply(self, kind, entity, payload):
        existing = self.apisix.get(kind, entity["id"])
        changed = None if existing is None else diff_paths(payload, existing)
        action = "create" if existing is None else "update" if changed else "unchanged"
        LOG.debug("%s '%s': %s", kind, _display(entity), action)
        if self.dry_run:
            self._emit(kind, entity, action, changed=changed)
        elif action != "unchanged":
            self.apisix.put(kind, entity["id"], payload)
        self.report.record(kind, {"create": "migrated", "update": "updated", "unchanged": "skipped"}[action])

    def _emit(self, kind, entity, action, **extra):
        if not self.dry_run:
            return
        record = {"kind": kind, "id": entity.get("id"), "name": _display(entity), "action": action}
        if kind == "routes":
            record["service"] = self._service_name((entity.get("service") or {}).get("id"))
        record.update({k: v for k, v in extra.items() if v is not None})
        print(json.dumps(record, sort_keys=True), flush=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Migrate Kong services and routes to APISIX.")
    p.add_argument("--kong-url", default=os.environ.get("KONG_ADMIN_URL", "http://localhost:8001"))
    p.add_argument("--apisix-url", default=os.environ.get("APISIX_ADMIN_URL", "http://localhost:9180"))
    p.add_argument("--apisix-key", default=os.environ.get("APISIX_ADMIN_KEY"),
                   help="APISIX Admin API key (required; falls back to $APISIX_ADMIN_KEY)")
    p.add_argument("--skip-routes", default=os.environ.get("KONG_SKIP_ROUTES", DEFAULT_SKIP_ROUTES),
                   help="comma-separated Kong route names to leave out (default: %(default)s; empty = none)")
    p.add_argument("--dry-run", action="store_true",
                   help="read and translate only; print the structured diff (JSON lines) and write nothing")
    p.add_argument("-v", "--verbose", action="store_true", help="log every API call (DEBUG)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    if not args.apisix_key:
        LOG.error("APISIX Admin API key is not set (use --apisix-key)")
        return 2
    skip = [n.strip() for n in args.skip_routes.split(",") if n.strip()]
    migrator = Migrator(KongClient(args.kong_url), ApisixClient(args.apisix_url, {"X-API-KEY": args.apisix_key}),
                        dry_run=args.dry_run, skip_routes=skip)
    try:
        report = migrator.run()
    except FatalError as e:
        LOG.error("%s", e)
        return 2
    return 1 if report.has_failures else 0


if __name__ == "__main__":
    sys.exit(main())
