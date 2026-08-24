"""Browser navigation strategies and driver interaction helpers."""

from __future__ import annotations

from typing import Any

from app.engine.driver_capabilities import (
    DriverProtocol,
    call_if_available,
    call_quietly,
    resolve_callable,
)
from app.infra.xhr_collector import XhrCollector
from app.logging_config import get_logger
from app.schemas.enums import NavigationMode
from app.schemas.request import ScrapeRequest
from app.schemas.response import XhrResponse

logger = get_logger()

_CAPABILITY_MISS = object()

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


def _driver_method(driver: DriverProtocol, *names: str):
    return resolve_callable(driver, *names) or driver.get


def navigate(
    driver: DriverProtocol,
    target_url: str,
    strategy: NavigationMode,
    timeout_seconds: int,
) -> None:
    if strategy == NavigationMode.ORGANIC_GET:
        method = _driver_method(driver, "organic_get", "google_get")
    elif strategy.value.startswith("google_get"):
        method = _driver_method(driver, "google_get")
    else:
        method = driver.get

    kwargs: dict[str, Any] = {}
    if strategy == NavigationMode.GOOGLE_GET_BYPASS:
        kwargs["bypass_cloudflare"] = True
    try:
        method(target_url, timeout=timeout_seconds, **kwargs)
    except TypeError:
        method(target_url, **kwargs)


def configure_driver(
    driver: DriverProtocol,
    payload: ScrapeRequest,
    target_url: str,
    collector: XhrCollector | None = None,
) -> None:
    tab = getattr(driver, "_tab", None)
    if tab is not None:
        if collector is not None:
            try:
                collector.install(tab)
            except Exception:
                pass

        if payload.block_trackers:
            call_quietly(tab, "block_urls", TRACKER_URL_PATTERNS)

    if payload.cookies:
        for c_name, c_val in payload.cookies.items():
            call_quietly(
                driver,
                "add_cookies",
                [{"name": str(c_name), "value": str(c_val), "url": target_url}],
            )

    if payload.headers and tab is not None:
        call_quietly(tab, "set_extra_http_headers", payload.headers)


def wait_for_readiness(
    driver: DriverProtocol,
    *,
    selector: str | None,
    timeout_seconds: int,
) -> None:
    if selector:
        driver.wait_for_element(selector, wait=timeout_seconds)
        return

    if (
        call_if_available(driver, "sleep_random", 0.5, 1.2, default=_CAPABILITY_MISS)
        is not _CAPABILITY_MISS
    ):
        return
    driver.sleep(1)


def apply_scrolling(driver: DriverProtocol) -> None:
    if (
        call_if_available(driver, "scroll_to_bottom", default=_CAPABILITY_MISS)
        is _CAPABILITY_MISS
        and call_if_available(driver, "scroll", default=_CAPABILITY_MISS)
        is _CAPABILITY_MISS
        and call_if_available(
            driver,
            "run_js",
            "window.scrollTo(0, document.body.scrollHeight);",
            default=_CAPABILITY_MISS,
        )
        is _CAPABILITY_MISS
    ):
        call_quietly(
            driver,
            "execute_script",
            "window.scrollTo(0, document.body.scrollHeight);",
        )

    if (
        call_if_available(driver, "sleep_random", 0.4, 0.9, default=_CAPABILITY_MISS)
        is _CAPABILITY_MISS
    ):
        call_quietly(driver, "sleep", 0.5)


def harvest_xhr(collector: XhrCollector, driver: DriverProtocol) -> list[XhrResponse]:
    tab = getattr(driver, "_tab", None)
    if tab is None:
        return collector.results()
    try:
        return collector.harvest(tab)
    except Exception as exc:
        logger.debug("xhr_harvest_failed error=%s", str(exc))
        return collector.results()
