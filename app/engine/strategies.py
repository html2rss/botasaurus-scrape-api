"""Browser navigation strategies and driver interaction helpers."""

import logging

from app.engine.driver_protocol import DriverProtocol, resolve_cdp_tab
from app.infra.detector import (
    UNREADABLE_SURFACE,
    ChallengeAssessment,
    ChallengeDetector,
)
from app.infra.xhr_collector import XhrCollector
from app.logging_config import get_logger, log_event
from app.schemas.enums import NavigationMode
from app.schemas.request import ScrapeRequest
from app.schemas.response import XhrResponse

logger = get_logger()

_READINESS_CHUNK_SECONDS = 2

TRACKER_URL_PATTERNS: list[str] = [
    "*google-analytics.com*",
    "*googletagmanager.com*",
    "*facebook.net*",
    "*doubleclick.net*",
    "*sentry.io*",
    "*hotjar.com*",
    "*clarity.ms*",
    "*datadoghq-browser-agent.com*",
    "*segment.io*",
    "*analytics.js*",
    "*.woff",
    "*.woff2",
    "*.ttf",
]

_AUTO_STRATEGIES: tuple[NavigationMode, ...] = (
    NavigationMode.GOOGLE_GET,
    NavigationMode.GOOGLE_GET_BYPASS,
    NavigationMode.GET,
)


def resolve_strategies(mode: NavigationMode, max_retries: int) -> list[NavigationMode]:
    max_attempts = 1 + max_retries
    if mode == NavigationMode.AUTO:
        return list(_AUTO_STRATEGIES[: min(len(_AUTO_STRATEGIES), max_attempts)])
    return [mode] * max_attempts


def navigate(
    driver: DriverProtocol,
    target_url: str,
    strategy: NavigationMode,
    timeout_seconds: int,
) -> None:
    match strategy:
        case NavigationMode.GET:
            driver.get(target_url, timeout=timeout_seconds)
        case NavigationMode.GOOGLE_GET:
            driver.google_get(target_url, timeout=timeout_seconds)
        case NavigationMode.GOOGLE_GET_BYPASS:
            driver.google_get(
                target_url, bypass_cloudflare=True, timeout=timeout_seconds
            )
        case NavigationMode.AUTO:
            raise ValueError("navigation strategy must be resolved before navigate()")


def configure_driver(
    driver: DriverProtocol,
    payload: ScrapeRequest,
    target_url: str,
    collector: XhrCollector | None = None,
) -> None:
    tab = resolve_cdp_tab(driver)
    if tab is not None:
        if collector is not None:
            try:
                collector.install(tab)
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "xhr_collector_install_failed",
                    error=str(exc),
                )
        if payload.block_trackers:
            tab.block_urls(TRACKER_URL_PATTERNS)
        if payload.headers:
            from botasaurus_driver import cdp

            tab.send(
                cdp.network.set_extra_http_headers(cdp.network.Headers(payload.headers))
            )

    if payload.cookies:
        driver.add_cookies(
            [
                {"name": str(name), "value": str(val), "url": target_url}
                for name, val in payload.cookies.items()
            ]
        )


def _mid_wait_challenge(driver: DriverProtocol) -> ChallengeAssessment | None:
    """Probe during readiness wait. None if clean; unclean/unreadable otherwise.

    Unreadable surfaces fail closed immediately so selector waits do not burn
    remaining budget on a hostile/hung tab.
    """
    try:
        html = driver.page_html or ""
    except Exception:
        return UNREADABLE_SURFACE
    assessment = ChallengeDetector.detect(html, driver=driver)
    if assessment.is_clean:
        return None
    return assessment


def wait_for_readiness(
    driver: DriverProtocol,
    *,
    selector: str | None,
    timeout_seconds: int,
) -> ChallengeAssessment | None:
    """Wait for selector / settle; return unclean/unreadable assessment to fail closed.

    Selector waits run in ≤2s chunks so a challenge or unreadable surface fails
    closed before the full wait budget burns. Non-selector settles probe once.
    """
    if not selector:
        driver.short_random_sleep()
        return _mid_wait_challenge(driver)

    remaining = max(0, int(timeout_seconds))
    while remaining > 0:
        chunk = min(_READINESS_CHUNK_SECONDS, remaining)
        try:
            driver.wait_for_element(selector, wait=chunk)
            return None
        except Exception:
            assessment = _mid_wait_challenge(driver)
            if assessment is not None:
                return assessment
            remaining -= chunk

    driver.wait_for_element(selector, wait=0)
    return None


def apply_scrolling(driver: DriverProtocol) -> None:
    driver.scroll_to_bottom()
    driver.short_random_sleep()


def harvest_xhr(collector: XhrCollector, driver: DriverProtocol) -> list[XhrResponse]:
    tab = resolve_cdp_tab(driver)
    if tab is None:
        return collector.results()
    try:
        return collector.harvest(tab)
    except Exception as exc:
        logger.debug("xhr_harvest_failed error=%s", str(exc))
        return collector.results()
