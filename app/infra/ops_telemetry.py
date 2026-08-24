from __future__ import annotations

from typing import Protocol, cast
from urllib.parse import urlparse

from app.constants import SERVICE_NAME
from app.logging_config import get_logger
from app.schemas.enums import ErrorCategory
from app.schemas.response import ScrapeError

logger = get_logger()


class SentryScope(Protocol):
    """Structural seam for the sentry_sdk scope used by terminal telemetry."""

    fingerprint: list[str] | None

    def set_tag(self, key: str, value: str) -> None: ...

    def set_context(self, key: str, value: dict[str, object]) -> None: ...


_P0_CATEGORIES = frozenset(
    {
        ErrorCategory.NAVIGATION_ERROR,
        ErrorCategory.TIMEOUT,
    }
)


def _hostname(url: str) -> str | None:
    return urlparse(url).hostname


def _issue_fingerprint(error_category: str, host: str | None) -> list[str]:
    return [SERVICE_NAME, error_category, host or "unknown"]


def _apply_scrape_tags(
    scope: SentryScope, result: ScrapeError, *, http_status: int
) -> None:
    host = _hostname(result.url)
    category = result.error_category.value
    diagnostics = result.diagnostics

    scope.set_tag("service", SERVICE_NAME)
    scope.set_tag("error_category", category)
    if host:
        scope.set_tag("host", host)
    scope.set_tag("http_status", str(http_status))
    scope.set_tag("render_ms", str(diagnostics.render_ms))
    scrape_context: dict[str, object] = {
        "request_id": diagnostics.request_id,
        "attempts": diagnostics.attempts,
    }
    if phase := diagnostics.timeout_phase:
        scope.set_tag("timeout_phase", phase.value)
        scrape_context["timeout_phase"] = phase.value
    scope.set_context("scrape", scrape_context)
    if diagnostics.strategy_used is not None:
        scope.set_tag("strategy_used", diagnostics.strategy_used.value)
    if diagnostics.execution_tier is not None:
        scope.set_tag("execution_tier", diagnostics.execution_tier.value)


def report_terminal_outcome(result: ScrapeError, *, http_status: int) -> None:
    """Emit P0 operational scrape failures to Sentry as grouped Issues."""
    from app.infra.sentry import sentry_is_ready

    if not sentry_is_ready():
        return
    if result.error_category not in _P0_CATEGORIES:
        return

    import sentry_sdk

    host = _hostname(result.url)
    category = result.error_category.value
    phase = result.diagnostics.timeout_phase
    category_label = f"{category}/{phase.value}" if phase else category

    with sentry_sdk.new_scope() as raw_scope:
        scope = cast(SentryScope, raw_scope)
        scope.fingerprint = _issue_fingerprint(category, host)
        _apply_scrape_tags(scope, result, http_status=http_status)
        sentry_sdk.capture_message(
            f"scrape terminal failure [{category_label}] host={host or 'unknown'}",
            level="error",
        )


def record_challenge_block(result: ScrapeError) -> None:
    """Increment challenge_block product signal metric; stdout logging stays in engine."""
    from app.infra.sentry import sentry_is_ready

    if not sentry_is_ready():
        return
    if result.error_category != ErrorCategory.CHALLENGE_BLOCK:
        return

    host = _hostname(result.url)
    diagnostics = result.diagnostics
    attributes: dict[str, str] = {
        "host": host or "unknown",
        "execution_tier": (
            diagnostics.execution_tier.value
            if diagnostics.execution_tier is not None
            else "unknown"
        ),
        "strategy_used": (
            diagnostics.strategy_used.value
            if diagnostics.strategy_used is not None
            else "unknown"
        ),
    }

    try:
        from sentry_sdk import metrics

        metrics.count("scrape.challenge_block", 1, attributes=attributes)
    except (ImportError, AttributeError):  # fmt: skip
        return


def emit_terminal_telemetry(result: ScrapeError, *, http_status: int) -> None:
    if result.error_category == ErrorCategory.CHALLENGE_BLOCK:
        record_challenge_block(result)
        return
    report_terminal_outcome(result, http_status=http_status)
