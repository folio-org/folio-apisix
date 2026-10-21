#!/usr/bin/env python3
"""Unit tests for migrate-route.py (run: python3 -m unittest discover -s migration -v).

The Java library folio-integration-apisix is NOT called here. migrate-route.py re-implements its id, label, uri,
methods and vars rules in Python; "mirrors the apisix-integration ..." tests assert the output equals what that
library produces (expected values taken from ApisixRouteFactory / ApisixGatewayService).
"""
import hashlib
import importlib.util
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROVIDED_DIR = os.path.join(HERE, "test-data", "provided")  # Kong entities fed to the script
EXPECTED_DIR = os.path.join(HERE, "test-data", "expected")  # APISIX entities the script must produce
_spec = importlib.util.spec_from_file_location("migrate_route", os.path.join(HERE, "migrate-route.py"))
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)

TENANT_ANY = 'http.headers.x_okapi_tenant ~ r#".*"#'


def route(expression, name="a" * 40, tags=("mod-x-1.0.0", "x-1.0"), priority=1, service="svc-1"):
    return {"id": "kong-uuid", "name": name, "expression": expression, "tags": list(tags), "priority": priority,
            "service": {"id": service}}


def service(**overrides):
    base = {"id": "svc-1", "name": "mod-x-1.0.0", "host": "mod-x", "port": 8081, "protocol": "http", "path": None,
            "retries": 5, "connect_timeout": 60000, "read_timeout": 360000, "write_timeout": 60000}
    base.update(overrides)
    return base


class TranslateExpressionTest(unittest.TestCase):
    def test_static_path(self):
        r = mr.kong_expression_to_apisix_match(f'(http.path == "/users" && http.method == "GET" && {TENANT_ANY})')
        self.assertEqual({"uri": "/users", "methods": ["GET"], "vars": [["http_x_okapi_tenant", "~~", ".*"]]}, r)

    def test_pattern_path_uses_prefix_wildcard_and_regex_var(self):
        r = mr.kong_expression_to_apisix_match('(http.path ~ "^/users/([^/]+)/roles$" && http.method == "GET")')
        self.assertEqual("/users/*", r["uri"])
        self.assertEqual([["uri", "~~", "^/users/([^/]+)/roles$"]], r["vars"])

    def test_regex_prefix_derivation(self):
        self.assertEqual("/a/b*", mr.kong_path_regex_to_apisix_uri("^/a/b(.*)$"))
        self.assertEqual("/*", mr.kong_path_regex_to_apisix_uri("^(.*)$"))
        self.assertEqual("/*", mr.kong_path_regex_to_apisix_uri("^/([^/]+)$"))
        self.assertEqual("/a*", mr.kong_path_regex_to_apisix_uri("^/ab*$"))
        self.assertEqual("/*", mr.kong_path_regex_to_apisix_uri("/a/(.*)$"))
        self.assertEqual("/*", mr.kong_path_regex_to_apisix_uri("^/a|/b$"))

    def test_multiple_methods_become_methods_list(self):
        r = mr.kong_expression_to_apisix_match('(http.path == "/x" && (http.method == "GET" || http.method == "POST"))')
        self.assertEqual(["GET", "POST"], r["methods"])
        self.assertEqual([], r["vars"])

    def test_all_methods(self):
        methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]
        alts = " || ".join(f'http.method == "{m}"' for m in methods)
        self.assertEqual(methods, mr.kong_expression_to_apisix_match(f'(http.path == "/x" && ({alts}))')["methods"])

    def test_tenant_list_becomes_in_condition(self):
        r = mr.kong_expression_to_apisix_match('(http.path == "/x" && http.method == "GET" && '
                                    '(http.headers.x_okapi_tenant == "t1" || http.headers.x_okapi_tenant == "t2"))')
        self.assertEqual([["http_x_okapi_tenant", "in", ["t1", "t2"]]], r["vars"])

    def test_single_tenant_is_still_a_list(self):
        r = mr.kong_expression_to_apisix_match('(http.path == "/x" && http.method == "GET" && '
                                    '(http.headers.x_okapi_tenant == "t1"))')
        self.assertEqual([["http_x_okapi_tenant", "in", ["t1"]]], r["vars"])

    def test_module_id_header_keeps_order_before_tenant(self):
        r = mr.kong_expression_to_apisix_match(f'(http.path == "/x" && http.method == "GET" && '
                                    f'http.headers.x_okapi_module_id == "mod-a-1.0.0" && {TENANT_ANY})')
        self.assertEqual([["http_x_okapi_module_id", "==", "mod-a-1.0.0"], ["http_x_okapi_tenant", "~~", ".*"]],
                         r["vars"])

    def test_method_only_expression_defaults_uri(self):
        r = mr.kong_expression_to_apisix_match('http.method == "OPTIONS"')
        self.assertEqual({"uri": "/*", "methods": ["OPTIONS"], "vars": []}, r)

    def test_not_equals_and_quoted_escapes(self):
        r = mr.kong_expression_to_apisix_match(r'(http.path == "/x" && http.headers.x_a != "b\"c")')
        self.assertEqual([["http_x_a", "~=", 'b"c']], r["vars"])

    def test_unsupported_expressions_fail(self):
        for bad in ('http.path == "/x" || http.method == "GET"',
                    '(http.path == "/x" && http.method == "A") || http.method == "B"',
                    '(http.path == "/x" && (http.method == "A" || http.headers.x_a == "b"))',
                    '(http.path == "/x" && (http.method != "A" || http.method != "B"))',
                    '!(http.path == "/x")',
                    'net.protocol == "http"',
                    '(http.path == "/x"',
                    'http.path == "/x" garbage'):
            with self.subTest(bad), self.assertRaises(mr.TranslationError):
                mr.kong_expression_to_apisix_match(bad)


class KongServiceToApisixServiceTest(unittest.TestCase):
    def test_service_payload_mirrors_apisix_integration_service(self):
        p = mr.kong_service_to_apisix_service(service())
        self.assertEqual({
            "id": "mod-x-1.0.0", "name": "mod-x-1.0.0",
            "upstream": {"type": "roundrobin", "scheme": "http", "pass_host": "pass", "nodes": {"mod-x:8081": 1},
                         "retries": 5, "timeout": {"connect": 60.0, "send": 60.0, "read": 360.0}}}, p)

    def test_long_name_is_hashed(self):
        name = "m" * 70
        self.assertEqual(hashlib.sha1(name.encode()).hexdigest(), mr.kong_service_to_apisix_service(service(name=name))["id"])

    def test_unnamed_service_falls_back_to_kong_id(self):
        self.assertEqual("svc-1", mr.kong_service_to_apisix_service(service(name=None))["id"])

    def test_path_becomes_proxy_rewrite(self):
        p = mr.kong_service_to_apisix_service(service(path="/api/"))
        self.assertEqual({"proxy-rewrite": {"regex_uri": ["^(.*)", "/api$1"]}}, p["plugins"])
        self.assertEqual({"proxy-rewrite": {"regex_uri": ["^(.*)", "/api$1"]}},
                         mr.kong_service_to_apisix_service(service(path="/api"))["plugins"])

    def test_root_or_empty_path_has_no_plugin(self):
        for path in (None, "", "/"):
            self.assertNotIn("plugins", mr.kong_service_to_apisix_service(service(path=path)))

    def test_partial_timeouts_fill_with_default_and_none_omits(self):
        p = mr.kong_service_to_apisix_service(service(connect_timeout=None, write_timeout=None, read_timeout=1500))
        self.assertEqual({"connect": 60.0, "send": 60.0, "read": 1.5}, p["upstream"]["timeout"])
        none = mr.kong_service_to_apisix_service(service(connect_timeout=None, write_timeout=None, read_timeout=None))
        self.assertNotIn("timeout", none["upstream"])

    def test_https_default_port_and_unsupported_protocol(self):
        self.assertEqual({"mod-x:443": 1}, mr.kong_service_to_apisix_service(service(protocol="https", port=None))["upstream"]["nodes"])
        with self.assertRaises(mr.TranslationError):
            mr.kong_service_to_apisix_service(service(protocol="grpc"))

    def test_invalid_id_fails(self):
        with self.assertRaises(mr.TranslationError):
            mr.kong_service_to_apisix_service(service(name="bad name"))


class KongRouteToApisixRouteTest(unittest.TestCase):
    def test_static_route_payload_mirrors_apisix_integration_route(self):
        p = mr.kong_route_to_apisix_route(route(f'(http.path == "/users" && http.method == "GET" && {TENANT_ANY})'), service())
        self.assertEqual({
            "id": "a" * 40, "name": "a" * 40, "uri": "/users", "status": 1, "priority": 1, "methods": ["GET"],
            "vars": [["http_x_okapi_tenant", "~~", ".*"]],
            "labels": {"module": "mod-x-1.0.0", "interface": "x-1.0"}}, p)
        self.assertNotIn("uris", p)

    def test_platform_scope_label(self):
        p = mr.kong_route_to_apisix_route(route('(http.path == "/x" && http.method == "GET")',
                                     tags=("mod-x-1.0.0", "x-1.0", mr.KONG_PLATFORM_SCOPE_TAG)), service())
        self.assertEqual({"module": "mod-x-1.0.0", "interface": "x-1.0", "application-scope": "platform"},
                         p["labels"])

    def test_module_label_from_service_name(self):
        p = mr.kong_route_to_apisix_route(route('http.method == "GET"', tags=("x-1.0", "mod-x-1.0.0")), service())
        self.assertEqual("mod-x-1.0.0", p["labels"]["module"])
        self.assertEqual("x-1.0", p["labels"]["interface"])

    def test_route_requires_expression_and_name(self):
        with self.assertRaises(mr.TranslationError):
            mr.kong_route_to_apisix_route(dict(route("x"), expression=None), service())
        with self.assertRaises(mr.TranslationError):
            mr.kong_route_to_apisix_route(dict(route('http.method == "GET"'), name=None), service())

    def test_priority_is_passed_through(self):
        self.assertEqual(0, mr.kong_route_to_apisix_route(route('http.method == "GET"', priority=0), service())["priority"])


def _load_list(path, key):
    with open(path, encoding="utf-8") as f:
        return json.load(f)[key]


def _without_times(entity):
    return {k: v for k, v in entity.items() if k not in ("create_time", "update_time")}


class SampleDataTest(unittest.TestCase):
    """Kong route + service (test-data/provided/) must translate to the entries that folio-integration-apisix
    created (test-data/expected/, copied from exist-routes-apisix / exist-services-apisix)."""

    ROUTE_NAME = "ee5f4fa36b89e930c5cf74c7d275ec4396f45d43"

    def setUp(self):
        routes = _load_list(os.path.join(PROVIDED_DIR, "kong-route.json"), "data")
        services = _load_list(os.path.join(PROVIDED_DIR, "kong-service.json"), "data")
        self.assertEqual(1, len(routes))
        self.assertEqual(1, len(services))
        self.kong_route, self.kong_service = routes[0], services[0]
        self.assertEqual(self.kong_service["id"], self.kong_route["service"]["id"])

        apisix_routes = _load_list(os.path.join(EXPECTED_DIR, "apisix-route.json"), "list")
        apisix_services = _load_list(os.path.join(EXPECTED_DIR, "apisix-service.json"), "list")
        self.assertEqual(1, len(apisix_routes))
        self.assertEqual(1, len(apisix_services))
        self.expected_route = _without_times(apisix_routes[0]["value"])
        self.expected_service = _without_times(apisix_services[0]["value"])
        self.assertEqual(self.kong_route["name"], self.expected_route["id"])
        self.assertEqual(mr.kong_service_to_apisix_service_id(self.kong_service), self.expected_service["id"])
        self.assertEqual(self.expected_service["id"], self.expected_route["service_id"])

    def test_route_equals_existing_apisix_route(self):
        actual = mr.kong_route_to_apisix_route(self.kong_route, self.kong_service)
        actual["service_id"] = mr.kong_service_to_apisix_service_id(self.kong_service)
        self.assertEqual(self.expected_route, actual)

    def test_migration_creates_apisix_route_with_kong_route_name_as_id_and_name(self):
        apisix = MigratorTest.FakeApisix()
        report = mr.Migrator(MigratorTest.FakeKong([self.kong_service], [self.kong_route]), apisix).run()
        self.assertFalse(report.has_failures)

        route_id = self.kong_route["name"]  # the sha1 hash folio-integration-apisix uses as route id and name
        self.assertIn(("routes", route_id), apisix.puts)  # PUT /apisix/admin/routes/<route id>
        self.assertNotIn(("routes", self.kong_route["id"]), apisix.puts)  # not keyed by the Kong uuid
        created = apisix.store[("routes", route_id)]
        self.assertEqual(route_id, created["id"])
        self.assertEqual(route_id, created["name"])
        self.assertEqual(self.expected_route["id"], created["id"])
        self.assertEqual(self.expected_route["name"], created["name"])

    def test_service_equals_existing_apisix_service_with_kong_retries_and_timeouts(self):
        expected = json.loads(json.dumps(self.expected_service))
        k = self.kong_service
        expected["upstream"]["retries"] = k["retries"]
        expected["upstream"]["timeout"] = {"connect": k["connect_timeout"] / 1000.0,
                                           "send": k["write_timeout"] / 1000.0,
                                           "read": k["read_timeout"] / 1000.0}
        self.assertEqual(expected, mr.kong_service_to_apisix_service(k))

    def test_migrated_route_points_at_migrated_service(self):
        apisix = MigratorTest.FakeApisix()
        report = mr.Migrator(MigratorTest.FakeKong([self.kong_service], [self.kong_route]), apisix).run()
        self.assertFalse(report.has_failures)
        stored_service = apisix.store[("services", self.expected_service["id"])]
        stored_route = apisix.store[("routes", self.expected_route["id"])]
        self.assertEqual(stored_service["id"], stored_route["service_id"])


class MigratorTest(unittest.TestCase):
    class FakeKong:
        def __init__(self, services, routes):
            self.data = {"/services": services, "/routes": routes}

        def list(self, path):
            return self.data[path]

    class FakeApisix:
        def __init__(self):
            self.store, self.puts = {}, []

        def preflight(self):
            pass

        def get(self, entity_type, eid):
            return self.store.get((entity_type, eid))

        def put(self, entity_type, eid, payload):
            self.store[(entity_type, eid)] = payload
            self.puts.append((entity_type, eid))

    def run_migration(self, services, routes, apisix=None, **kwargs):
        apisix = apisix or self.FakeApisix()
        report = mr.Migrator(self.FakeKong(services, routes), apisix, **kwargs).run()
        return report, apisix

    def test_entities_are_keyed_by_apisix_integration_ids_and_rerun_is_idempotent(self):
        routes = [route('(http.path == "/x" && http.method == "GET")', service="svc-1")]
        report, apisix = self.run_migration([service()], routes)
        self.assertEqual({("services", "mod-x-1.0.0"), ("routes", "a" * 40)}, set(apisix.store))
        self.assertEqual("mod-x-1.0.0", apisix.store[("routes", "a" * 40)]["service_id"])
        self.assertFalse(report.has_failures)

        report, _ = self.run_migration([service()], routes, apisix=apisix)
        self.assertEqual(2, len(apisix.puts))
        self.assertEqual(1, report.counts["routes"]["skipped"])
        self.assertEqual(1, report.counts["services"]["skipped"])

    def test_failed_service_fails_dependent_routes(self):
        routes = [route('http.method == "GET"', service="svc-1")]
        report, apisix = self.run_migration([service(protocol="grpc")], routes)
        self.assertEqual(1, report.counts["services"]["failed"])
        self.assertEqual(1, report.counts["routes"]["failed"])
        self.assertEqual({}, apisix.store)

    def test_skipped_routes_exclude_their_exclusive_services(self):
        routes = [route('http.method == "OPTIONS"', name="CORS", service="svc-1")]
        report, apisix = self.run_migration([service()], routes, skip_routes=["CORS"])
        self.assertEqual(1, report.counts["services"]["excluded"])
        self.assertEqual({}, apisix.store)

    def test_dry_run_writes_nothing(self):
        report, apisix = self.run_migration([service()], [route('http.method == "GET"')], dry_run=True)
        self.assertEqual([], apisix.puts)
        self.assertEqual(1, report.counts["routes"]["migrated"])


if __name__ == "__main__":
    unittest.main()
