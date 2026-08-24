import unittest

from tests.support.fakes import (
    FakeNetworkResponse,
    FakeRequestId,
    FakeTab,
)


class XhrCollectorTests(unittest.TestCase):
    def setUp(self):
        from app.infra.xhr_collector import XhrCollector

        self.XhrCollector = XhrCollector
        self.target = "https://example.com/"
        self.collector = XhrCollector(self.target)

    def _drive_json(self, tab, request_id, url, body, mime="application/json"):
        rid = FakeRequestId(request_id)
        tab.bodies[str(rid)] = (body, False)
        self.collector._on_response(
            rid,
            FakeNetworkResponse(url, 200, mime, {"content-type": mime}),
            None,
        )
        self.collector._on_finished(type("E", (), {"request_id": rid})())

    def test_install_enables_network_and_registers_handlers(self):
        tab = FakeTab()
        self.collector.install(tab)
        self.assertTrue(tab.network_enabled)
        self.assertEqual(
            tab.response_handler.__func__, self.collector._on_response.__func__
        )
        self.assertIs(tab.response_handler.__self__, self.collector)
        self.assertEqual(
            tab.finished_handler.__func__, self.collector._on_finished.__func__
        )
        self.assertIs(tab.finished_handler.__self__, self.collector)

    def test_captures_json_subresource(self):
        tab = FakeTab()
        self.collector.install(tab)
        self._drive_json(
            tab, "1", "https://api.example.com/feed", '{"items":[{"title":"A"}]}'
        )
        results = self.collector.harvest(tab)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://api.example.com/feed")
        self.assertEqual(results[0]["status_code"], 200)
        self.assertEqual(results[0]["headers"], {"content-type": "application/json"})
        self.assertIn("items", results[0]["body"])

    def test_skips_non_json_mime(self):
        tab = FakeTab()
        self.collector.install(tab)
        self._drive_json(
            tab,
            "1",
            "https://cdn.example.com/app.js",
            "console.log(1)",
            mime="application/javascript",
        )
        self.assertEqual(self.collector.harvest(tab), [])

    def test_skips_main_document(self):
        tab = FakeTab()
        self.collector.install(tab)
        rid = FakeRequestId("doc")
        tab.bodies[str(rid)] = ('{"nope":true}', False)
        self.collector._on_response(
            rid,
            FakeNetworkResponse(self.target, 200, "application/json"),
            None,
        )
        self.collector._on_finished(type("E", (), {"request_id": rid})())
        self.assertEqual(self.collector.harvest(tab), [])

    def test_skips_empty_body(self):
        tab = FakeTab()
        self.collector.install(tab)
        self._drive_json(tab, "1", "https://api.example.com/empty", "")
        self.assertEqual(self.collector.harvest(tab), [])

    def test_enforces_max_responses_cap(self):
        tab = FakeTab()
        self.collector.install(tab)
        for i in range(self.XhrCollector.MAX_RESPONSES + 5):
            self._drive_json(
                tab, str(i), f"https://api.example.com/i/{i}", f'{{"i":{i}}}'
            )
        results = self.collector.harvest(tab)
        self.assertEqual(len(results), self.XhrCollector.MAX_RESPONSES)

    def test_enforces_max_body_bytes_cap(self):
        tab = FakeTab()
        self.collector.install(tab)
        oversized = "x" * (self.XhrCollector.MAX_BODY_BYTES + 1)
        self._drive_json(tab, "1", "https://api.example.com/big", oversized)
        self.assertEqual(self.collector.harvest(tab), [])

    def test_enforces_aggregate_bytes_cap(self):
        tab = FakeTab()
        self.collector.install(tab)
        # Five near-max bodies would exceed 2 MB aggregate; stop once budget trips.
        chunk = "y" * self.XhrCollector.MAX_BODY_BYTES
        for i in range(5):
            self._drive_json(tab, str(i), f"https://api.example.com/chunk/{i}", chunk)
        results = self.collector.harvest(tab)
        total = sum(len(entry["body"].encode("utf-8")) for entry in results)
        self.assertLessEqual(total, self.XhrCollector.MAX_AGGREGATE_BYTES)
        self.assertEqual(len(results), 4)
        self.assertLess(len(results), 5)

    def test_headers_allowlist_keeps_only_content_type(self):
        tab = FakeTab()
        self.collector.install(tab)
        rid = FakeRequestId("hdr")
        tab.bodies[str(rid)] = ('{"ok":true}', False)
        self.collector._on_response(
            rid,
            FakeNetworkResponse(
                "https://api.example.com/secure",
                200,
                "application/json",
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Set-Cookie": "session=secret",
                    "X-Request-Id": "abc",
                },
            ),
            None,
        )
        self.collector._on_finished(type("E", (), {"request_id": rid})())
        results = self.collector.harvest(tab)
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["headers"],
            {"content-type": "application/json; charset=utf-8"},
        )
        self.assertNotIn("Set-Cookie", results[0]["headers"])
        self.assertNotIn("set-cookie", results[0]["headers"])

    def test_reset_clears_pending_ready_and_collected(self):
        tab = FakeTab()
        self.collector.install(tab)
        self._drive_json(
            tab, "1", "https://api.example.com/old", '{"from":"failed-attempt"}'
        )
        first = self.collector.harvest(tab)
        self.assertEqual(len(first), 1)

        self.collector.reset()
        self.assertEqual(self.collector.results(), [])

        self._drive_json(
            tab, "2", "https://api.example.com/new", '{"from":"success-attempt"}'
        )
        second = self.collector.harvest(tab)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["body"], '{"from":"success-attempt"}')
        self.assertNotIn("failed-attempt", second[0]["body"])
