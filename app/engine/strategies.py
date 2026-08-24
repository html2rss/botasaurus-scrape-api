"""Browser navigation strategies and driver interaction helpers."""

from __future__ import annotations

import logging
from typing import Any

from botasaurus.browser import Driver

from app.infra.xhr_collector import XhrCollector
from app.schemas import NavigationMode, ScrapeRequest

logger = logging.getLogger("botasaurus_scrape_api")

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
    driver: Driver, target_url: str, strategy: NavigationMode, timeout_seconds: int
) -> None:
    if strategy == NavigationMode.ORGANIC_GET:
        method = getattr(
            driver, "organic_get", getattr(driver, "google_get", driver.get)
        )
    elif strategy.value.startswith("google_get"):
        method = getattr(driver, "google_get", driver.get)
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
    driver: Driver,
    payload: ScrapeRequest,
    target_url: str,
    collector: XhrCollector | None = None,
) -> None:
    if hasattr(driver, "_tab"):
        if collector is not None:
            try:
                collector.install(driver._tab)
            except Exception:
                pass

        if payload.block_trackers:
            try:
                driver._tab.block_urls(TRACKER_URL_PATTERNS)
            except Exception:
                pass

    if payload.cookies:
        for c_name, c_val in payload.cookies.items():
            try:
                driver.add_cookies(
                    [{"name": str(c_name), "value": str(c_val), "url": target_url}]
                )
            except Exception:
                pass

    if payload.headers and hasattr(driver, "_tab"):
        try:
            driver._tab.set_extra_http_headers(payload.headers)
        except Exception:
            pass


def wait_for_readiness(
    driver: Driver,
    *,
    selector: str | None,
    timeout_seconds: int,
) -> None:
    if selector:
        driver.wait_for_element(selector, wait=timeout_seconds)
        return

    sleep_random_fn = getattr(driver, "sleep_random", None)
    if callable(sleep_random_fn):
        try:
            sleep_random_fn(0.5, 1.2)
            return
        except Exception:
            pass
    driver.sleep(1)


def apply_scrolling(driver: Driver) -> None:
    scroll_bottom_fn = getattr(driver, "scroll_to_bottom", None)
    scroll_fn = getattr(driver, "scroll", None)
    run_js_fn = getattr(driver, "run_js", None)

    if callable(scroll_bottom_fn):
        try:
            scroll_bottom_fn()
        except Exception:
            pass
    elif callable(scroll_fn):
        try:
            scroll_fn()
        except Exception:
            pass
    elif callable(run_js_fn):
        try:
            run_js_fn("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            pass
    elif hasattr(driver, "execute_script"):
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            pass

    sleep_random_fn = getattr(driver, "sleep_random", None)
    if callable(sleep_random_fn):
        try:
            sleep_random_fn(0.4, 0.9)
            return
        except Exception:
            pass

    try:
        driver.sleep(0.5)
    except Exception:
        pass


def harvest_xhr(collector: XhrCollector, driver: Driver) -> list[dict[str, Any]]:
    tab = getattr(driver, "_tab", None)
    if tab is None:
        return collector.results()
    try:
        return collector.harvest(tab)
    except Exception as exc:
        logger.debug("xhr_harvest_failed error=%s", str(exc))
        return collector.results()
