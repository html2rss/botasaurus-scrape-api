# tests/test_ops_telemetry.py
import unittest
from unittest.mock import MagicMock, patch

from app.infra.ops_telemetry import (
    emit_terminal_telemetry,
    is_service_side_failure,
    report_terminal_outcome,
)
from app.schemas.enums import ErrorCategory, ExecutionTier, NavigationMode, TimeoutPhase
from app.schemas.response import ScrapeDiagnostics, ScrapeError


def _scrape_error(
    *,
    url: str = "https://example.com/path?secret=1",
    category: ErrorCategory = ErrorCategory.NAVIGATION_ERROR,
    request_id: str = "req-123",
    strategy_used: NavigationMode | None = NavigationMode.GET,
    execution_tier: ExecutionTier | None = ExecutionTier.BROWSER_DRIVER,
    render_ms: int = 1500,
    attempts: int = 0,
    timeout_phase: TimeoutPhase | None = None,
) -> ScrapeError:
    return ScrapeError(
        url=url,
        error="failure",
        error_category=category,
        diagnostics=ScrapeDiagnostics(
            request_id=request_id,
            attempts=attempts,
            strategy_used=strategy_used,
            timeout_phase=timeout_phase,
            render_ms=render_ms,
            execution_tier=execution_tier,
        ),
    )


class OpsTelemetryTests(unittest.TestCase):
    def test_report_terminal_outcome_noop_when_sentry_not_ready(self):
        result = _scrape_error(category=ErrorCategory.NAVIGATION_ERROR)
        with (
            patch("app.infra.sentry.sentry_is_ready", return_value=False),
            patch("sentry_sdk.capture_message") as mock_capture,
        ):
            report_terminal_outcome(result, http_status=502)
            mock_capture.assert_not_called()

    def test_report_terminal_outcome_emits_p0_navigation_error(self):
        result = _scrape_error(category=ErrorCategory.NAVIGATION_ERROR)
        mock_scope = MagicMock()
        with (
            patch("app.infra.sentry.sentry_is_ready", return_value=True),
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
                {"request_id": "req-123", "attempts": 0},
            )
            mock_scope.set_tag.assert_any_call("http_status", "502")

    def test_report_terminal_outcome_emits_p0_timeout(self):
        result = _scrape_error(
            category=ErrorCategory.TIMEOUT, timeout_phase=TimeoutPhase.QUEUE
        )
        mock_scope = MagicMock()
        with (
            patch("app.infra.sentry.sentry_is_ready", return_value=True),
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
        result = _scrape_error(
            category=ErrorCategory.TIMEOUT, timeout_phase=TimeoutPhase.BOOT
        )
        mock_scope = MagicMock()
        with (
            patch("app.infra.sentry.sentry_is_ready", return_value=True),
            patch("sentry_sdk.new_scope") as mock_new_scope,
            patch("sentry_sdk.capture_message") as mock_capture,
        ):
            mock_new_scope.return_value.__enter__.return_value = mock_scope
            report_terminal_outcome(result, http_status=504)

            (message,) = mock_capture.call_args.args
            self.assertIn("timeout/boot", message)
            mock_scope.set_tag.assert_any_call("timeout_phase", "boot")
            self.assertEqual(
                mock_scope.fingerprint,
                ["botasaurus-scrape-api", "timeout", "example.com"],
            )
            mock_scope.set_context.assert_called_once_with(
                "scrape",
                {
                    "request_id": "req-123",
                    "attempts": 0,
                    "timeout_phase": "boot",
                },
            )

    def test_report_terminal_outcome_skips_challenge_block(self):
        result = _scrape_error(category=ErrorCategory.CHALLENGE_BLOCK)
        with (
            patch("app.infra.sentry.sentry_is_ready", return_value=True),
            patch("sentry_sdk.capture_message") as mock_capture,
        ):
            report_terminal_outcome(result, http_status=502)
            mock_capture.assert_not_called()

    def test_service_side_failure_classification(self):
        self.assertTrue(
            is_service_side_failure(
                _scrape_error(
                    category=ErrorCategory.TIMEOUT, timeout_phase=TimeoutPhase.QUEUE
                )
            )
        )
        self.assertTrue(
            is_service_side_failure(
                _scrape_error(category=ErrorCategory.NAVIGATION_ERROR)
            )
        )
        # Target-site failures are product outcomes (OpenTelemetry), not Sentry Issues.
        self.assertFalse(
            is_service_side_failure(
                _scrape_error(
                    category=ErrorCategory.TIMEOUT, timeout_phase=TimeoutPhase.WORK
                )
            )
        )
        self.assertFalse(
            is_service_side_failure(
                _scrape_error(category=ErrorCategory.NAVIGATION_ERROR, attempts=1)
            )
        )

    def test_emit_terminal_telemetry_skips_target_site_failures(self):
        result = _scrape_error(category=ErrorCategory.NAVIGATION_ERROR, attempts=2)
        with (
            patch("app.infra.sentry.sentry_is_ready", return_value=True),
            patch("sentry_sdk.capture_message") as mock_capture,
        ):
            emit_terminal_telemetry(result, http_status=502)
            mock_capture.assert_not_called()

    def test_emit_terminal_telemetry_routes_service_failures_to_issues(self):
        result = _scrape_error(category=ErrorCategory.NAVIGATION_ERROR)
        with patch("app.infra.ops_telemetry.report_terminal_outcome") as mock_issue:
            emit_terminal_telemetry(result, http_status=502)
            mock_issue.assert_called_once_with(result, http_status=502, warm_hit=None)

    def test_report_terminal_outcome_tags_warm_hit_when_provided(self):
        result = _scrape_error(
            category=ErrorCategory.TIMEOUT, timeout_phase=TimeoutPhase.BOOT
        )
        mock_scope = MagicMock()
        with (
            patch("app.infra.sentry.sentry_is_ready", return_value=True),
            patch("sentry_sdk.new_scope") as mock_new_scope,
            patch("sentry_sdk.capture_message"),
        ):
            mock_new_scope.return_value.__enter__.return_value = mock_scope
            report_terminal_outcome(result, http_status=504, warm_hit=True)
            mock_scope.set_tag.assert_any_call("warm_hit", "true")


if __name__ == "__main__":
    unittest.main()
