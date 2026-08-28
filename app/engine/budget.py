"""Single owner of scrape wall-clock budget math shared across tiers."""

from __future__ import annotations

import time

from app.config import Settings

# AUTO→browser escalate only when at least this many seconds remain on the
# total scrape clock (Chromium boot is not free).
MIN_ESCALATE_REMAINING_SECONDS = 8


def elapsed_ms(started_monotonic: float) -> int:
    return int((time.monotonic() - started_monotonic) * 1000)


def remaining_total_seconds(settings: Settings, started_monotonic: float) -> int:
    return max(
        0,
        int(settings.scrape_timeout_seconds - (time.monotonic() - started_monotonic)),
    )


def remaining_work_seconds(settings: Settings, browser_ready_monotonic: float) -> int:
    return max(
        0,
        int(
            settings.scrape_work_timeout_seconds
            - (time.monotonic() - browser_ready_monotonic)
        ),
    )


def browser_step_budget_seconds(
    settings: Settings, started_monotonic: float, browser_ready_monotonic: float
) -> int:
    return min(
        remaining_total_seconds(settings, started_monotonic),
        remaining_work_seconds(settings, browser_ready_monotonic),
    )


def is_timeout_exception(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    name = exc.__class__.__name__.lower()
    if "timeout" in name:
        return True
    return "timeout" in str(exc).lower()
