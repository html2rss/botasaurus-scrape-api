"""Unit tests for envelope construction and HTML UTF-8 normalization."""

from __future__ import annotations

import unittest

from app.engine.envelope import (
    build_diagnostics,
    build_error,
    build_success,
    html_document_headers,
    utf8_normalize_html,
)
from app.infra.detector import ChallengeAssessment
from app.schemas.enums import ErrorCategory, ExecutionTier, NavigationMode, TimeoutPhase


class EnvelopeTests(unittest.TestCase):
    def test_utf8_normalize_leaves_correct_unicode_unchanged(self) -> None:
        html = "<html><body><h1>Caffè 日本語</h1></body></html>"
        self.assertEqual(utf8_normalize_html(html), html)

    def test_utf8_normalize_fixes_latin1_mojibake(self) -> None:
        mojibake = "<html><body><h1>CaffÃ¨</h1></body></html>"
        self.assertEqual(
            utf8_normalize_html(mojibake), "<html><body><h1>Caffè</h1></body></html>"
        )

    def test_utf8_normalize_empty_or_ascii(self) -> None:
        self.assertEqual(utf8_normalize_html(""), "")
        ascii_html = "<html><body><h1>Hello World</h1></body></html>"
        self.assertEqual(utf8_normalize_html(ascii_html), ascii_html)

    def test_html_document_headers_injects_utf8_content_type(self) -> None:
        html = "<html><body><h1>Hello</h1></body></html>"
        normalized, headers = html_document_headers(
            html, {"content-type": "application/octet-stream", "x-custom": "1"}
        )
        self.assertEqual(normalized, html)
        self.assertIsNotNone(headers)
        assert headers is not None
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(headers["x-custom"], "1")

    def test_html_document_headers_empty_html_passthrough(self) -> None:
        headers = {"x-test": "val"}
        norm, res_headers = html_document_headers("", headers)
        self.assertEqual(norm, "")
        self.assertEqual(res_headers, headers)

    def test_build_diagnostics_with_challenge_assessment(self) -> None:
        assessment = ChallengeAssessment(
            blocked_detected=True,
            challenge_detected=True,
            detected_marker="cf-turnstile",
        )
        diag = build_diagnostics(
            request_id="req-123",
            attempts=2,
            strategy_used=NavigationMode.GOOGLE_GET_BYPASS,
            render_ms=250,
            execution_tier=ExecutionTier.BROWSER_DRIVER,
            assessment=assessment,
            timeout_phase=None,
        )
        self.assertEqual(diag.request_id, "req-123")
        self.assertEqual(diag.attempts, 2)
        self.assertEqual(diag.strategy_used, NavigationMode.GOOGLE_GET_BYPASS)
        self.assertIsNotNone(diag.challenge)
        assert diag.challenge is not None
        self.assertTrue(diag.challenge.blocked)
        self.assertTrue(diag.challenge.detected)
        self.assertEqual(diag.challenge.marker, "cf-turnstile")

    def test_build_success(self) -> None:
        success = build_success(
            "https://example.com",
            request_id="req-abc",
            html="<html><body><h1>Test</h1></body></html>",
            attempts=1,
            render_ms=100,
            execution_tier=ExecutionTier.HTTP_REQUEST,
        )
        self.assertEqual(success.url, "https://example.com")
        self.assertEqual(success.final_url, "https://example.com")
        self.assertEqual(success.status_code, 200)
        self.assertIsNotNone(success.headers)
        assert success.headers is not None
        self.assertEqual(success.headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(success.diagnostics.request_id, "req-abc")

    def test_build_error(self) -> None:
        err = build_error(
            "https://example.com",
            "Target URL is blocked",
            request_id="req-err",
            error_category=ErrorCategory.VALIDATION,
            timeout_phase=TimeoutPhase.BOOT,
        )
        self.assertEqual(err.url, "https://example.com")
        self.assertEqual(err.error, "Target URL is blocked")
        self.assertEqual(err.error_category, ErrorCategory.VALIDATION)
        self.assertEqual(err.diagnostics.request_id, "req-err")
        self.assertEqual(err.diagnostics.timeout_phase, TimeoutPhase.BOOT)


if __name__ == "__main__":
    unittest.main()
