# tests/test_ops_telemetry.py
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.ops_telemetry import (
    emit_terminal_telemetry,
    record_challenge_block,
    report_terminal_outcome,
)
from app.schemas import (
    ErrorCategory,
    ExecutionTier,
    NavigationMode,
    ScrapeDiagnostics,
    ScrapeError,
    TimeoutPhase,
)


def _scrape_error(
    *,
    url: str = "https://example.com/path?secret=1",
    category: ErrorCategory = ErrorCategory.NAVIGATION_ERROR,
    request_id: str = "req-123",
    strategy_used: NavigationMode | None = NavigationMode.GET,
    execution_tier: ExecutionTier | None = ExecutionTier.BROWSER_DRIVER,
    render_ms: int = 1500,
) -> ScrapeError:
    return ScrapeError(
        url=url,
        error="failure",
        error_category=category,
        diagnostics=ScrapeDiagnostics(
            request_id=request_id,
            attempts=2,
            strategy_used=strategy_used,
            render_ms=render_ms,
            execution_tier=execution_tier,
        ),
    )


class OpsTelemetryTests(unittest.TestCase):
    def test_report_terminal_outcome_noop_when_sentry_not_ready(self):
        result = _scrape_error(category=ErrorCategory.NAVIGATION_ERROR)
        with (
            patch("app.ops_telemetry.sentry_is_ready", return_value=False),
            patch("sentry_sdk.capture_message") as mock_capture,
        ):
            report_terminal_outcome(result, http_status=502)
            mock_capture.assert_not_called()

    def test_report_terminal_outcome_emits_p0_navigation_error(self):
        result = _scrape_error(category=ErrorCategory.NAVIGATION_ERROR)
        mock_scope = MagicMock()
        with (
            patch("app.ops_telemetry.sentry_is_ready", return_value=True),
            patch("sentry_sdk.new_scope") as mock_new_scope,
            patch("sentry_sdk.capture_message") as mock_capture,
        ):
            mock_new_scope.return_value.__enter__.return_value = mock_scope
            report_terminal_outcome(result, http_status=502)

            mock_capture.assert_called_once()
            (message,) = mock_capture.call_args.args
            self.assertIn("navigation_error", message)
            self.assertEqual(mock_capture.call_args.kwargs["level"], "error")
            self.assertEqual(
                mock_scope.fingerprint,
                ["botasaurus-scrape-api", "navigation_error", "example.com"],
            )
            mock_scope.set_tag.assert_any_call("host", "example.com")
            mock_scope.set_context.assert_called_once_with(
                "scrape",
                {"request_id": "req-123", "attempts": 2},
            )
            mock_scope.set_tag.assert_any_call("http_status", "502")

    def test_report_terminal_outcome_emits_p0_timeout(self):
        result = _scrape_error(category=ErrorCategory.TIMEOUT)
        mock_scope = MagicMock()
        with (
            patch("app.ops_telemetry.sentry_is_ready", return_value=True),
            patch("sentry_sdk.new_scope") as mock_new_scope,
            patch("sentry_sdk.capture_message") as mock_capture,
        ):
            mock_new_scope.return_value.__enter__.return_value = mock_scope
            report_terminal_outcome(result, http_status=504)

            mock_capture.assert_called_once()
            self.assertEqual(
                mock_scope.fingerprint,
                ["botasaurus-scrape-api", "timeout", "example.com"],
            )
            mock_scope.set_tag.assert_any_call("http_status", "504")

    def test_report_terminal_outcome_tags_timeout_phase(self):
        result = _scrape_error(category=ErrorCategory.TIMEOUT)
        result.diagnostics.timeout_phase = TimeoutPhase.BOOT
        mock_scope = MagicMock()
        with (
            patch("app.ops_telemetry.sentry_is_ready", return_value=True),
            patch("sentry_sdk.new_scope") as mock_new_scope,
            patch("sentry_sdk.capture_message") as mock_capture,
        ):
            mock_new_scope.return_value.__enter__.return_value = mock_scope
            report_terminal_outcome(result, http_status=504)

            (message,) = mock_capture.call_args.args
            self.assertIn("timeout/boot", message)
            mock_scope.set_tag.assert_any_call("timeout_phase", "boot")
            mock_scope.set_context.assert_called_once_with(
                "scrape",
                {
                    "request_id": "req-123",
                    "attempts": 2,
                    "timeout_phase": "boot",
                },
            )

    def test_report_terminal_outcome_skips_challenge_block(self):
        result = _scrape_error(category=ErrorCategory.CHALLENGE_BLOCK)
        with (
            patch("app.ops_telemetry.sentry_is_ready", return_value=True),
            patch("sentry_sdk.capture_message") as mock_capture,
        ):
            report_terminal_outcome(result, http_status=502)
            mock_capture.assert_not_called()

    def test_record_challenge_block_increments_metric_only(self):
        result = _scrape_error(
            category=ErrorCategory.CHALLENGE_BLOCK,
            strategy_used=NavigationMode.GOOGLE_GET,
            execution_tier=ExecutionTier.BROWSER_DRIVER,
        )
        with (
            patch("app.ops_telemetry.sentry_is_ready", return_value=True),
            patch("sentry_sdk.metrics.count") as mock_count,
            patch("sentry_sdk.capture_message") as mock_capture,
        ):
            record_challenge_block(result)

            mock_count.assert_called_once_with(
                "scrape.challenge_block",
                1,
                attributes={
                    "host": "example.com",
                    "execution_tier": "browser_driver",
                    "strategy_used": "google_get",
                },
            )
            mock_capture.assert_not_called()

    def test_record_challenge_block_noop_when_sentry_not_ready(self):
        result = _scrape_error(category=ErrorCategory.CHALLENGE_BLOCK)
        with (
            patch("app.ops_telemetry.sentry_is_ready", return_value=False),
            patch("sentry_sdk.metrics.count") as mock_count,
        ):
            record_challenge_block(result)
            mock_count.assert_not_called()

    def test_record_challenge_block_skips_non_challenge_categories(self):
        result = _scrape_error(category=ErrorCategory.NAVIGATION_ERROR)
        with (
            patch("app.ops_telemetry.sentry_is_ready", return_value=True),
            patch("sentry_sdk.metrics.count") as mock_count,
        ):
            record_challenge_block(result)
            mock_count.assert_not_called()

    def test_emit_terminal_telemetry_routes_challenge_block_to_metric(self):
        result = _scrape_error(category=ErrorCategory.CHALLENGE_BLOCK)
        with (
            patch("app.ops_telemetry.record_challenge_block") as mock_metric,
            patch("app.ops_telemetry.report_terminal_outcome") as mock_issue,
        ):
            emit_terminal_telemetry(result, http_status=502)
            mock_metric.assert_called_once_with(result)
            mock_issue.assert_not_called()

    def test_emit_terminal_telemetry_routes_p0_errors_to_issues(self):
        result = _scrape_error(category=ErrorCategory.NAVIGATION_ERROR)
        with (
            patch("app.ops_telemetry.record_challenge_block") as mock_metric,
            patch("app.ops_telemetry.report_terminal_outcome") as mock_issue,
        ):
            emit_terminal_telemetry(result, http_status=502)
            mock_issue.assert_called_once_with(result, http_status=502)
            mock_metric.assert_not_called()


if __name__ == "__main__":
    unittest.main()
