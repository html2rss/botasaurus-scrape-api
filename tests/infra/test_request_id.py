import unittest
import uuid
from unittest.mock import patch

from app.infra.request_id import resolve_request_id

VALID_UUID = "550e8400-e29b-41d4-a716-446655440000"
FALLBACK_UUID = "11111111-2222-4333-8444-555555555555"


class ResolveRequestIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = [
            {
                "name": "valid uuid passthrough",
                "inbound": VALID_UUID,
                "expected_id": VALID_UUID,
            },
            {
                "name": "valid token with underscore and dot",
                "inbound": "req_123.test",
                "expected_id": "req_123.test",
            },
            {
                "name": "strips surrounding whitespace",
                "inbound": f"  {VALID_UUID}  ",
                "expected_id": VALID_UUID,
            },
            {
                "name": "absent inbound generates fallback",
                "inbound": None,
                "expected_id": FALLBACK_UUID,
            },
            {
                "name": "empty string generates fallback",
                "inbound": "",
                "expected_id": FALLBACK_UUID,
            },
            {
                "name": "whitespace-only generates fallback",
                "inbound": "   ",
                "expected_id": FALLBACK_UUID,
            },
            {
                "name": "too long generates fallback",
                "inbound": "a" * 129,
                "expected_id": FALLBACK_UUID,
            },
            {
                "name": "slash is path-unsafe",
                "inbound": "foo/bar",
                "expected_id": FALLBACK_UUID,
            },
            {
                "name": "backslash is path-unsafe",
                "inbound": "foo\\bar",
                "expected_id": FALLBACK_UUID,
            },
            {
                "name": "dotdot is path-unsafe",
                "inbound": "..",
                "expected_id": FALLBACK_UUID,
            },
            {
                "name": "embedded dotdot is path-unsafe",
                "inbound": "foo..bar",
                "expected_id": FALLBACK_UUID,
            },
            {
                "name": "null byte is path-unsafe",
                "inbound": "foo\x00bar",
                "expected_id": FALLBACK_UUID,
            },
            {
                "name": "non-token characters generate fallback",
                "inbound": "foo@bar",
                "expected_id": FALLBACK_UUID,
            },
        ]

    def test_resolve_request_id_table(self):
        for case in self.cases:
            with self.subTest(case=case["name"]):
                with patch(
                    "app.infra.request_id.uuid.uuid4",
                    return_value=uuid.UUID(FALLBACK_UUID),
                ):
                    request_id = resolve_request_id(case["inbound"], host="example.com")

                self.assertEqual(request_id, case["expected_id"])

    def test_fallback_logs_reason_without_rejected_value(self):
        with (
            patch(
                "app.infra.request_id.uuid.uuid4", return_value=uuid.UUID(FALLBACK_UUID)
            ),
            self.assertLogs("botasaurus_scrape_api", level="INFO") as captured,
        ):
            resolve_request_id("bad/id", host="example.com")

        log_text = "\n".join(captured.output)
        self.assertIn("request_id_fallback host=example.com reason=invalid", log_text)
        self.assertNotIn("bad/id", log_text)

    def test_absent_fallback_logs_absent_reason(self):
        with (
            patch(
                "app.infra.request_id.uuid.uuid4", return_value=uuid.UUID(FALLBACK_UUID)
            ),
            self.assertLogs("botasaurus_scrape_api", level="INFO") as captured,
        ):
            resolve_request_id(None, host="example.com")

        log_text = "\n".join(captured.output)
        self.assertIn("request_id_fallback host=example.com reason=absent", log_text)


if __name__ == "__main__":
    unittest.main()
