import unittest

from app.config import get_settings
from app.engine import (
    ScraperEngine,
)
from app.schemas.enums import (
    ExecutionTier,
)
from app.schemas.response import ScrapeDiagnostics, ScrapeSuccess


class RequestIdContractTests(unittest.TestCase):
    INBOUND_ID = "550e8400-e29b-41d4-a716-446655440000"

    def test_honored_request_id_on_200(self):
        from tests.support.http import test_client

        def fake_execute(payload, _deadline=None, *, request_id=None, progress=None):
            return ScrapeSuccess(
                url=str(payload.url),
                html="<html></html>",
                diagnostics=ScrapeDiagnostics(
                    request_id=request_id,
                    attempts=1,
                    render_ms=1,
                    execution_tier=ExecutionTier.HTTP_REQUEST,
                ),
            )

        with test_client(execute_side_effect=fake_execute) as client:
            response = client.post(
                "/scrape",
                json={"url": "https://example.com"},
                headers={"X-Request-Id": self.INBOUND_ID},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["diagnostics"]["request_id"], self.INBOUND_ID)

    def test_honored_request_id_on_400(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            response = client.post(
                "/scrape",
                json={"url": "https://this-host-does-not-exist-12345.invalid/"},
                headers={"X-Request-Id": self.INBOUND_ID},
            )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["diagnostics"]["request_id"], self.INBOUND_ID)
        self.assertEqual(body["error_category"], "validation")

    def test_honored_request_id_on_422(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            response = client.post(
                "/scrape",
                json={
                    "url": "https://example.com",
                    "window_size": [1920],
                },
                headers={"X-Request-Id": self.INBOUND_ID},
            )

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["diagnostics"]["request_id"], self.INBOUND_ID)
        self.assertEqual(body["error_category"], "validation")

    def test_request_id_collision_returns_502(self):
        from tests.support.http import test_client

        engine = ScraperEngine(settings=get_settings())
        engine.register_request_id(self.INBOUND_ID)
        try:
            with test_client(engine=engine) as client:
                response = client.post(
                    "/scrape",
                    json={"url": "https://example.com"},
                    headers={"X-Request-Id": self.INBOUND_ID},
                )
        finally:
            engine.unregister_request_id(self.INBOUND_ID)

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["diagnostics"]["request_id"], self.INBOUND_ID)
        self.assertEqual(body["error_category"], "navigation_error")


class SchemaValidationHttpTests(unittest.TestCase):
    def test_schema_422_returns_scrape_envelope(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with (
            self.assertLogs("botasaurus_scrape_api", level="INFO") as captured,
            TestClient(app) as client,
        ):
            response = client.post(
                "/scrape",
                json={
                    "url": "https://example.com",
                    "window_size": [1920],
                },
            )

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertNotIn("detail", body)
        self.assertEqual(body["url"], "https://example.com")
        self.assertTrue(body["error"])
        self.assertIn("window_size", body["error"])
        self.assertEqual(body["error_category"], "validation")
        self.assertNotIn("html", body)
        self.assertTrue(body["diagnostics"]["request_id"])
        log_text = "\n".join(captured.output)
        self.assertIn("request_schema_422", log_text)
        self.assertIn("host=example.com", log_text)
        self.assertIn("field=window_size", log_text)

    def test_scrape_clamps_wait_timeout_instead_of_422(self):
        from tests.support.http import test_client

        captured: dict[str, int] = {}

        def fake_execute(payload, _deadline=None, *, request_id=None, progress=None):
            captured["wait"] = payload.wait_timeout_seconds
            return ScrapeSuccess(
                url=str(payload.url),
                html="<html></html>",
                diagnostics=ScrapeDiagnostics(
                    request_id=request_id or "req-wait-clamp",
                    attempts=1,
                    render_ms=1,
                    execution_tier=ExecutionTier.HTTP_REQUEST,
                ),
            )

        with test_client(execute_side_effect=fake_execute) as client:
            response = client.post(
                "/scrape",
                json={
                    "url": "https://example.com",
                    "wait_timeout_seconds": 35,
                },
            )

        self.assertNotEqual(response.status_code, 422)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["wait"], get_settings().scrape_work_timeout_seconds)
        self.assertEqual(captured["wait"], 30)


def _schema_ref_names(node: object) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and "/schemas/" in ref:
            names.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            names |= _schema_ref_names(value)
    elif isinstance(node, list):
        for item in node:
            names |= _schema_ref_names(item)
    return names


class OpenApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.schema = app.openapi()

    def test_scrape_documents_x_request_id_header(self):
        scrape = self.schema["paths"]["/scrape"]["post"]
        parameters = scrape.get("parameters") or []
        header_params = {
            param["name"]: param for param in parameters if param.get("in") == "header"
        }
        self.assertIn("X-Request-Id", header_params)
        self.assertFalse(header_params["X-Request-Id"].get("required", True))

    def test_documents_health_and_scrape_paths(self):
        paths = self.schema["paths"]
        self.assertIn("/health", paths)
        self.assertIn("get", paths["/health"])
        self.assertIn("/scrape", paths)
        self.assertIn("post", paths["/scrape"])

    def test_operation_ids_and_tags(self):
        health = self.schema["paths"]["/health"]["get"]
        scrape = self.schema["paths"]["/scrape"]["post"]
        self.assertEqual(health["operationId"], "get-health")
        self.assertEqual(scrape["operationId"], "scrape-url")
        self.assertEqual(health["tags"], ["health"])
        self.assertEqual(scrape["tags"], ["scrape"])
        tag_names = {tag["name"] for tag in self.schema["tags"]}
        self.assertEqual(tag_names, {"health", "scrape"})
        for tag in self.schema["tags"]:
            self.assertTrue(tag.get("description"))

    def test_info_servers_and_version(self):
        info = self.schema["info"]
        self.assertEqual(info["title"], "Botasaurus Scrape API")
        self.assertEqual(info["version"], "2.0.0")
        self.assertTrue(info.get("description"))
        self.assertEqual(info["contact"]["name"], "html2rss")
        self.assertEqual(
            info["contact"]["url"],
            "https://github.com/html2rss/botasaurus-scrape-api/issues",
        )
        self.assertNotIn("email", info["contact"])
        self.assertEqual(info["license"]["name"], "MIT")
        self.assertTrue(info["license"].get("url"))
        servers = self.schema["servers"]
        self.assertEqual(servers[0]["url"], "http://localhost:4010")
        self.assertEqual(servers[0]["description"], "Local Docker (make serve)")

    def test_scrape_documents_contract_status_codes(self):
        responses = self.schema["paths"]["/scrape"]["post"]["responses"]
        for status in ("200", "400", "403", "422", "502", "504"):
            self.assertIn(status, responses)
            self.assertTrue(responses[status].get("description"))

    def test_scrape_error_statuses_use_scrape_envelope_not_fastapi_detail(self):
        responses = self.schema["paths"]["/scrape"]["post"]["responses"]
        success_refs = _schema_ref_names(responses["200"])
        self.assertIn("ScrapeSuccess", success_refs)
        self.assertNotIn("ScrapeResponse", success_refs)
        for status in ("400", "403", "422", "502", "504"):
            with self.subTest(status=status):
                refs = _schema_ref_names(responses[status])
                self.assertIn("ScrapeError", refs)
                self.assertNotIn("ScrapeResponse", refs)
                self.assertNotIn("HTTPValidationError", refs)
                self.assertNotIn("ValidationError", refs)

    def test_wait_timeout_seconds_openapi_does_not_advertise_range_as_422(self):
        props = self.schema["components"]["schemas"]["ScrapeRequest"]["properties"]
        wait_schema = props["wait_timeout_seconds"]
        self.assertNotIn("minimum", wait_schema)
        self.assertNotIn("maximum", wait_schema)
        description = wait_schema.get("description") or ""
        self.assertIn("clamped", description)

    def test_window_size_openapi_is_object(self):
        props = self.schema["components"]["schemas"]["ScrapeRequest"]["properties"]
        window_schema = props["window_size"]
        refs = _schema_ref_names(window_schema)
        self.assertIn("WindowSize", refs)
        size_schema = self.schema["components"]["schemas"]["WindowSize"]
        size_props = size_schema["properties"]
        self.assertIn("width", size_props)
        self.assertIn("height", size_props)
        self.assertNotIn("minItems", window_schema)
        self.assertNotIn("maxItems", window_schema)
        self.assertNotIn("scroll_to_bottom", props)

    def test_health_schema_includes_status_fields(self):
        health_200 = self.schema["paths"]["/health"]["get"]["responses"]["200"]
        refs = _schema_ref_names(health_200)
        self.assertIn("HealthResponse", refs)
        health_schema = self.schema["components"]["schemas"]["HealthResponse"]
        properties = health_schema["properties"]
        self.assertIn("status", properties)
        self.assertIn("service", properties)
        self.assertIn("botasaurus_version", properties)
        status_schema = properties["status"]
        self.assertTrue(
            status_schema.get("const") == "ok"
            or status_schema.get("enum") == ["ok"]
            or "ok" in (status_schema.get("examples") or [])
        )

    def test_xhr_responses_use_xhr_response_model(self):
        scrape_schema = self.schema["components"]["schemas"]["ScrapeSuccess"]
        refs = _schema_ref_names(scrape_schema["properties"]["xhr_responses"])
        self.assertIn("XhrResponse", refs)
        xhr_schema = self.schema["components"]["schemas"]["XhrResponse"]
        properties = xhr_schema["properties"]
        for field in ("url", "status_code", "headers", "body"):
            self.assertIn(field, properties)
        self.assertIn("diagnostics", scrape_schema["properties"])
        self.assertNotIn("ScrapeResponse", self.schema["components"]["schemas"])


if __name__ == "__main__":
    unittest.main()
