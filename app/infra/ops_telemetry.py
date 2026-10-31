from typing import Protocol, cast
from urllib.parse import urlparse

from app.constants import SERVICE_NAME
from app.schemas.enums import ErrorCategory, TimeoutPhase
from app.schemas.response import ScrapeError


class SentryScope(Protocol):
    """Structural seam for the sentry_sdk scope used by terminal telemetry."""

    fingerprint: list[str] | None

    def set_tag(self, key: str, value: str) -> None: ...

    def set_context(self, key: str, value: dict[str, object]) -> None: ...


def _hostname(url: str) -> str | None:
    return urlparse(url).hostname


def _issue_fingerprint(error_category: str, host: str | None) -> list[str]:
    return [SERVICE_NAME, error_category, host or "unknown"]


def _apply_scrape_tags(
    scope: SentryScope,
    result: ScrapeError,
    *,
    http_status: int,
    warm_hit: bool | None = None,
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
    if warm_hit is not None:
        scope.set_tag("warm_hit", "true" if warm_hit else "false")
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


def report_terminal_outcome(
    result: ScrapeError,
    *,
    http_status: int,
    warm_hit: bool | None = None,
) -> None:
    """Emit service-side scrape failures to Sentry as grouped Issues."""
    from app.infra.sentry import sentry_is_ready

    if not sentry_is_ready():
        return
    if not is_service_side_failure(result):
        return

    import sentry_sdk

    host = _hostname(result.url)
    category = result.error_category.value
    phase = result.diagnostics.timeout_phase
    category_label = f"{category}/{phase.value}" if phase else category

    with sentry_sdk.new_scope() as raw_scope:
        scope = cast(SentryScope, raw_scope)
        scope.fingerprint = _issue_fingerprint(category, host)
        _apply_scrape_tags(scope, result, http_status=http_status, warm_hit=warm_hit)
        sentry_sdk.capture_message(
            f"scrape terminal failure [{category_label}] host={host or 'unknown'}",
            level="error",
        )


def is_service_side_failure(result: ScrapeError) -> bool:
    """True for failures that signal our service health, not the scraped site.

    Queue/boot timeouts (capacity, Chromium start) and failures before any
    navigation attempt (boot, storage) are actionable. Work-phase timeouts,
    navigation errors on the target and anti-bot blocks are expected product
    outcomes and are tracked as OpenTelemetry metrics/traces instead.
    """
    diagnostics = result.diagnostics
    if result.error_category == ErrorCategory.TIMEOUT:
        return diagnostics.timeout_phase in {TimeoutPhase.QUEUE, TimeoutPhase.BOOT}
    return (
        result.error_category == ErrorCategory.NAVIGATION_ERROR
        and diagnostics.attempts == 0
    )


def emit_terminal_telemetry(
    result: ScrapeError,
    *,
    http_status: int,
    warm_hit: bool | None = None,
) -> None:
    report_terminal_outcome(result, http_status=http_status, warm_hit=warm_hit)
