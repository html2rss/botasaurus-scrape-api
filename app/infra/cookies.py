"""Cookies earned during a scrape, scoped to the target host(s)."""

import logging
from collections.abc import Iterable
from http.cookiejar import CookieJar
from urllib.parse import urlparse

from app.engine.driver_protocol import DriverProtocol
from app.logging_config import get_logger, log_event

logger = get_logger()


def _hosts(*urls: str | None) -> set[str]:
    return {host for url in urls if url and (host := urlparse(url).hostname)}


def _scoped(hosts: set[str], cookies: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    """Keep cookies whose domain covers a target host (RFC 6265 domain match)."""
    out: dict[str, str] = {}
    for name, value, domain in cookies:
        scope = domain.lstrip(".").lower()
        if not scope or any(h == scope or h.endswith(f".{scope}") for h in hosts):
            out[name] = value
    return out


def driver_cookies(
    driver: DriverProtocol, target_url: str, final_url: str | None
) -> dict[str, str]:
    try:
        raw = driver.get_cookies()
    except Exception as exc:
        log_event(logger, logging.WARNING, "cookies_read_failed", error=str(exc))
        return {}
    return _scoped(
        _hosts(target_url, final_url),
        ((str(c["name"]), str(c["value"]), str(c.get("domain", ""))) for c in raw),
    )


def jar_cookies(
    jar: CookieJar, target_url: str, final_url: str | None
) -> dict[str, str]:
    return _scoped(
        _hosts(target_url, final_url),
        ((c.name, c.value or "", c.domain) for c in jar),
    )
