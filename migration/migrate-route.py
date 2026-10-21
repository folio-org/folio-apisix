#!/usr/bin/env python3
"""Migrate Kong services and routes to Apache APISIX.

Reads from the Kong Admin API and writes to the APISIX Admin API (idempotent PUTs). Order: services -> routes.
Entities are shaped exactly like the ones folio-integration-apisix creates, so the library can manage them
afterwards: service id = Kong service name (module id; sha1 when longer than 64 chars), route id = Kong route
name (sha1 of the route name), routes carry `module` / `interface` labels.
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
import hashlib
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
ENTITY_TYPES = ("services", "routes")
DEFAULT_SKIP_ROUTES = "version,CORS"
SUPPORTED_KONG_SERVICE_PROTOCOLS = ("http", "https")
APISIX_MAX_ID_LENGTH = 64
APISIX_DEFAULT_TIMEOUT_SECONDS = 60.0
KONG_PLATFORM_SCOPE_TAG = "application-scope:platform"
KONG_TENANT_FIELD = "http.headers.x_okapi_tenant"
_APISIX_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
_APISIX_LABEL_VALUE_RE = re.compile(r"^\S{1,256}$")


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

    def get(self, entity_type, eid):
        try:
            body = self.request("GET", f"/apisix/admin/{entity_type}/{urllib.parse.quote(eid, safe='')}")
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        return body["value"] if isinstance(body, dict) and isinstance(body.get("value"), dict) else body

    def put(self, entity_type, eid, payload):
        self.request("PUT", f"/apisix/admin/{entity_type}/{urllib.parse.quote(eid, safe='')}", payload)


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------

# One condition: <field> <op> <string>, where the string is "..." or a raw r#"..."#.
_CONDITION_RE = re.compile(
    r'\s*([A-Za-z][A-Za-z0-9_.\-]*)\s*(==|!=|~)\s*(?:r(#*)"(.*?)"\3|"((?:[^"\\]|\\.)*)")')
_KONG_TO_APISIX_OPERATORS = {"==": "==", "!=": "~=", "~": "~~"}
_REGEX_META = frozenset("([{\\.*+?|$)")


def _kong_display_name(kong_entity):
    return kong_entity.get("name") or kong_entity.get("id")


def kong_field_to_apisix_var(kong_field):
    if kong_field == "http.path":
        return "uri"  # `request_uri` would include the query string
    if kong_field == "http.method":
        return "request_method"
    if kong_field == "http.host":
        return "host"
    if kong_field.startswith("http.headers."):
        return "http_" + kong_field[len("http.headers."):].lower().replace("-", "_")
    raise TranslationError(f"unsupported field '{kong_field}'")


class _KongExpressionParser:
    """Kong expression -> list of clauses; clauses are AND-ed, a clause is a list of OR-ed conditions.

    A condition is (field, operator, value). Only `&&` of conditions and parenthesised `||` groups of single
    conditions are accepted (that is all folio-integration-kong produces); anything else is rejected.
    """

    def __init__(self, text):
        self.text, self.pos = text, 0

    def parse(self):
        clauses = self._or()
        self._skip_ws()
        if self.pos != len(self.text):
            self._fail()
        return clauses

    def _fail(self):
        raise TranslationError(f"unsupported expression near {self.text[self.pos:self.pos + 40]!r}")

    def _skip_ws(self):
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _eat(self, token):
        self._skip_ws()
        if self.text.startswith(token, self.pos):
            self.pos += len(token)
            return True
        return False

    def _or(self):
        alternatives = [self._and()]
        while self._eat("||"):
            alternatives.append(self._and())
        if len(alternatives) == 1:
            return alternatives[0]
        group = []
        for alternative in alternatives:
            if len(alternative) != 1:  # `a && b || c` has no faithful APISIX form
                raise TranslationError("unsupported expression: '&&' mixed with '||' outside parentheses")
            group.extend(alternative[0])
        return [group]

    def _and(self):
        clauses = self._term()
        while self._eat("&&"):
            clauses = clauses + self._term()
        return clauses

    def _term(self):
        if self._eat("("):
            clauses = self._or()
            if not self._eat(")"):
                self._fail()
            return clauses
        return [[self._condition()]]

    def _condition(self):
        self._skip_ws()
        m = _CONDITION_RE.match(self.text, self.pos)
        if not m:
            self._fail()
        kong_field, kong_operator, _, raw_value, quoted_value = m.groups()
        self.pos = m.end()
        kong_value = raw_value if raw_value is not None else re.sub(r'\\(["\\])', r"\1", quoted_value)
        return kong_field, kong_operator, kong_value


def kong_path_regex_to_apisix_uri(kong_path_regex):
    """Longest literal prefix of an anchored Kong path regex as an APISIX prefix-wildcard `uri` (`/*` when none).

    Mirrors ApisixRouteFactory.buildUri: the regex kept in `vars` narrows what the wildcard matches.
    """
    if not kong_path_regex.startswith("^") or "|" in kong_path_regex:
        return "/*"
    body = kong_path_regex[1:]
    literal_end = next((i for i, ch in enumerate(body) if ch in _REGEX_META), len(body))
    literal_prefix = body[:literal_end]
    if literal_end < len(body) and body[literal_end] in "*+?{":
        literal_prefix = literal_prefix[:-1]  # the quantifier applies to the last literal character
    return literal_prefix + "*" if literal_prefix.startswith("/") else "/*"


def kong_expression_to_apisix_match(kong_expression):
    """Kong route expression -> {"uri", "methods", "vars"} in the shape ApisixRouteFactory produces.

    `http.path == X` -> exact `uri`; `http.path ~ R` -> prefix-wildcard `uri` + `["uri","~~",R]`;
    `http.method == M` (or an `||` group of them) -> `methods`; `||` groups of `==` on one field -> `in`
    (the tenant list ApisixRouteTenantService manages). Anything else raises TranslationError, so a route is
    never created with a looser match than the original.
    """
    apisix_uri, apisix_methods, apisix_vars = None, None, []
    for kong_clause in _KongExpressionParser(kong_expression.strip()).parse():
        kong_field, kong_operator, kong_value = kong_clause[0]
        if len(kong_clause) > 1:
            if any(c[0] != kong_field or c[1] != "==" for c in kong_clause):
                raise TranslationError("unsupported '||' group: only '==' alternatives on a single field")
            kong_values = list(dict.fromkeys(c[2] for c in kong_clause))
            if kong_field == "http.method" and apisix_methods is None:
                apisix_methods = kong_values
            else:
                apisix_vars.append([kong_field_to_apisix_var(kong_field), "in", kong_values])
        elif (kong_field == "http.path" and kong_operator == "==" and apisix_uri is None
              and kong_value.startswith("/") and "*" not in kong_value):
            apisix_uri = kong_value
        elif kong_field == "http.path" and kong_operator == "~" and apisix_uri is None:
            apisix_uri = kong_path_regex_to_apisix_uri(kong_value)
            apisix_vars.append(["uri", _KONG_TO_APISIX_OPERATORS[kong_operator], kong_value])
        elif kong_field == "http.method" and kong_operator == "==" and apisix_methods is None:
            apisix_methods = [kong_value]
        elif kong_field == KONG_TENANT_FIELD and kong_operator == "==":
            apisix_vars.append([kong_field_to_apisix_var(kong_field), "in", [kong_value]])
        else:
            apisix_vars.append([kong_field_to_apisix_var(kong_field), _KONG_TO_APISIX_OPERATORS[kong_operator],
                                kong_value])
    return {"uri": apisix_uri or "/*", "methods": apisix_methods, "vars": apisix_vars}


def _require_valid_apisix_id(value, what):
    if not _APISIX_ID_RE.match(value):
        raise TranslationError(f"{what} '{value}' is not a valid APISIX id (1-64 chars of [a-zA-Z0-9_.-])")
    return value


def kong_service_to_apisix_service_id(kong_service):
    """Same rule as ApisixGatewayService.serviceIdOf: the module id, or its sha1 when longer than 64 chars."""
    kong_name = kong_service.get("name")
    if not kong_name:
        return _require_valid_apisix_id(kong_service["id"], "service id")
    if len(kong_name) <= APISIX_MAX_ID_LENGTH:
        return _require_valid_apisix_id(kong_name, "service name")
    return _require_valid_apisix_id(hashlib.sha1(kong_name.encode("utf-8")).hexdigest(), "service name")


def kong_service_to_apisix_service(kong_service):
    kong_protocol = kong_service.get("protocol") or "http"
    if kong_protocol not in SUPPORTED_KONG_SERVICE_PROTOCOLS:
        raise TranslationError(f"unsupported protocol '{kong_protocol}'")
    if not kong_service.get("host"):
        raise TranslationError("service has no host")
    kong_port = kong_service.get("port") or (443 if kong_protocol == "https" else 80)
    apisix_upstream = {"type": "roundrobin", "scheme": kong_protocol,
                       "pass_host": "pass",  # as folio-integration-apisix: the client's Host header is forwarded
                       "nodes": {f"{kong_service['host']}:{kong_port}": 1}}
    if kong_service.get("retries") is not None:
        apisix_upstream["retries"] = kong_service["retries"]
    kong_timeouts_ms = {apisix_key: kong_service.get(kong_key) for kong_key, apisix_key in
                        (("connect_timeout", "connect"), ("write_timeout", "send"), ("read_timeout", "read"))}
    if any(ms is not None for ms in kong_timeouts_ms.values()):
        # APISIX requires all three fields (in seconds) once `timeout` is present
        apisix_upstream["timeout"] = {key: APISIX_DEFAULT_TIMEOUT_SECONDS if ms is None else ms / 1000.0
                                      for key, ms in kong_timeouts_ms.items()}
    apisix_service = {"id": kong_service_to_apisix_service_id(kong_service),
                      "name": _kong_display_name(kong_service), "upstream": apisix_upstream}
    kong_path = kong_service.get("path")
    if kong_path not in (None, "", "/"):
        # APISIX nodes cannot carry a path; prefix it with a service-level proxy-rewrite
        apisix_path_prefix = kong_path[:-1] if kong_path.endswith("/") else kong_path
        apisix_service["plugins"] = {"proxy-rewrite": {"regex_uri": ["^(.*)", apisix_path_prefix + "$1"]}}
    return apisix_service


def kong_route_to_apisix_labels(kong_route, kong_service):
    """Kong tags [moduleId, interfaceId, "application-scope:platform"?] -> APISIX labels."""
    kong_tags = kong_route.get("tags") or []
    kong_module_and_interface_tags = [t for t in kong_tags if t != KONG_PLATFORM_SCOPE_TAG]
    module_id = (kong_service or {}).get("name") or (
        kong_module_and_interface_tags[0] if kong_module_and_interface_tags else None)
    interface_id = next((t for t in kong_module_and_interface_tags if t != module_id), None)
    apisix_labels = {k: v for k, v in (("module", module_id), ("interface", interface_id)) if v}
    if KONG_PLATFORM_SCOPE_TAG in kong_tags:
        apisix_labels["application-scope"] = "platform"
    for key, value in apisix_labels.items():
        if not _APISIX_LABEL_VALUE_RE.match(value):
            raise TranslationError(f"label '{key}' value {value!r} is not valid in APISIX")
    return apisix_labels


def kong_route_to_apisix_route(kong_route, kong_service=None):
    if not kong_route.get("expression"):
        raise TranslationError("only expression routes are supported")
    if not kong_route.get("name"):
        raise TranslationError("route has no name (the name is the APISIX route id used by folio-integration-apisix)")
    apisix_match = kong_expression_to_apisix_match(kong_route["expression"])
    apisix_route_id = _require_valid_apisix_id(kong_route["name"], "route name")
    apisix_route = {"id": apisix_route_id, "name": apisix_route_id, "uri": apisix_match["uri"], "status": 1}
    if isinstance(kong_route.get("priority"), int):
        apisix_route["priority"] = kong_route["priority"]
    if apisix_match["methods"]:
        apisix_route["methods"] = apisix_match["methods"]
    if apisix_match["vars"]:
        apisix_route["vars"] = apisix_match["vars"]
    apisix_labels = kong_route_to_apisix_labels(kong_route, kong_service)
    if apisix_labels:
        apisix_route["labels"] = apisix_labels
    return apisix_route


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
        self.counts = {k: dict.fromkeys(self.OUTCOMES, 0) for k in ENTITY_TYPES}
        self.failures = []

    def record(self, entity_type, outcome):
        self.counts[entity_type][outcome] += 1

    def fail(self, entity_type, kong_entity, reason):
        self.record(entity_type, "failed")
        self.failures.append((entity_type, _kong_display_name(kong_entity), kong_entity.get("id"), reason))
        LOG.error("%s '%s' (%s) failed: %s", entity_type, _kong_display_name(kong_entity), kong_entity.get("id"), reason)

    @property
    def has_failures(self):
        return bool(self.failures)

    def log_summary(self, dry_run):
        LOG.info("Migration summary%s", " (DRY RUN - nothing was written; 'migrated' = would be created)" if dry_run else "")
        for entity_type in ENTITY_TYPES:
            c = self.counts[entity_type]
            LOG.info("  %-9s migrated=%d updated=%d skipped(already exists)=%d failed=%d excluded(by config)=%d",
                     entity_type + ":", c["migrated"], c["updated"], c["skipped"], c["failed"], c["excluded"])
        if self.failures:
            LOG.info("Failed entities (%d):", len(self.failures))
            for entity_type, name, eid, reason in self.failures:
                LOG.info("  %s '%s' (%s): %s", entity_type, name, eid, reason)


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
        self.failed = {k: set() for k in ENTITY_TYPES}
        self.kong_services_by_id = {}

    def run(self):
        try:
            self.apisix.preflight()
        except ApiError as e:
            raise FatalError(f"APISIX Admin API is not usable: {e}")
        try:
            kong_services = self.kong.list("/services")
            kong_routes = self.kong.list("/routes")
        except ApiError as e:
            raise FatalError(f"cannot read from Kong Admin API: {e}")
        LOG.info("Read from Kong: %d services, %d routes", len(kong_services), len(kong_routes))
        self.kong_services_by_id = {s["id"]: s for s in kong_services}

        # Services used only by skipped routes are not migrated either.
        used = lambda rs: {r["service"]["id"] for r in rs if r.get("service")}
        skipped_kong_routes = [r for r in kong_routes if r.get("name") in self.skip_routes]
        excluded_kong_service_ids = used(skipped_kong_routes) - used(
            r for r in kong_routes if r.get("name") not in self.skip_routes)

        LOG.info("Migrating services...")
        for kong_service in kong_services:
            if kong_service["id"] in excluded_kong_service_ids:
                self.report.record("services", "excluded")
            else:
                self._migrate("services", kong_service,
                              lambda s=kong_service: kong_service_to_apisix_service(s))

        LOG.info("Migrating routes...")
        for kong_route in kong_routes:
            if kong_route.get("name") in self.skip_routes:
                LOG.info("Skipping route '%s' (configured skip list)", kong_route.get("name"))
                self.report.record("routes", "excluded")
            else:
                self._migrate("routes", kong_route, lambda r=kong_route: self._build_apisix_route(r))

        self.report.log_summary(self.dry_run)
        return self.report

    def _build_apisix_route(self, kong_route):
        kong_service_id = (kong_route.get("service") or {}).get("id")
        if not kong_service_id:
            raise TranslationError("route has no service")
        if kong_service_id in self.failed["services"]:
            raise TranslationError(
                f"dependency failed: service '{self._service_name(kong_service_id)}' could not be migrated")
        kong_service = self.kong_services_by_id.get(kong_service_id)
        if kong_service is None:
            raise TranslationError(f"service {kong_service_id} was not returned by Kong")
        apisix_route = kong_route_to_apisix_route(kong_route, kong_service)
        apisix_route["service_id"] = kong_service_to_apisix_service_id(kong_service)
        return apisix_route

    def _service_name(self, kong_service_id):
        kong_service = self.kong_services_by_id.get(kong_service_id)
        return _kong_display_name(kong_service) if kong_service else kong_service_id

    def _migrate(self, entity_type, kong_entity, build_apisix_entity):
        try:
            self._apply(entity_type, kong_entity, build_apisix_entity())
        except (TranslationError, ApiError) as e:
            self.failed[entity_type].add(kong_entity["id"])
            self.report.fail(entity_type, kong_entity, str(e))
            self._emit(entity_type, kong_entity, "error", reason=str(e))

    def _apply(self, entity_type, kong_entity, apisix_entity):
        existing = self.apisix.get(entity_type, apisix_entity["id"])
        changed = None if existing is None else diff_paths(apisix_entity, existing)
        action = "create" if existing is None else "update" if changed else "unchanged"
        LOG.debug("%s '%s': %s", entity_type, _kong_display_name(kong_entity), action)
        if self.dry_run:
            self._emit(entity_type, kong_entity, action, apisix_id=apisix_entity["id"], changed=changed)
        elif action != "unchanged":
            self.apisix.put(entity_type, apisix_entity["id"], apisix_entity)
        self.report.record(entity_type, {"create": "migrated", "update": "updated", "unchanged": "skipped"}[action])

    def _emit(self, entity_type, kong_entity, action, apisix_id=None, **extra):
        if not self.dry_run:
            return
        record = {"type": entity_type, "id": apisix_id or kong_entity.get("id"), "kong_id": kong_entity.get("id"),
                  "name": _kong_display_name(kong_entity), "action": action}
        if entity_type == "routes":
            record["service"] = self._service_name((kong_entity.get("service") or {}).get("id"))
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
