"""Typed optional-driver method adapter for Botasaurus Driver seams."""

from __future__ import annotations

import logging
from typing import Any, Protocol, cast, runtime_checkable

logger = logging.getLogger("botasaurus_scrape_api")


@runtime_checkable
class DriverTabProtocol(Protocol):
    def block_urls(self, patterns: list[str]) -> Any: ...

    def set_extra_http_headers(self, headers: dict[str, str]) -> Any: ...


@runtime_checkable
class DriverProtocol(Protocol):
    page_html: str | None
    current_url: str | None

    def get(self, url: str, /, **kwargs: Any) -> Any: ...

    def google_get(self, url: str, /, **kwargs: Any) -> Any: ...

    def organic_get(self, url: str, /, **kwargs: Any) -> Any: ...

    def wait_for_element(self, selector: str, /, **kwargs: Any) -> Any: ...

    def sleep(self, seconds: float, /) -> Any: ...

    def sleep_random(self, minimum: float, maximum: float, /) -> Any: ...

    def scroll_to_bottom(self) -> Any: ...

    def scroll(self) -> Any: ...

    def run_js(self, script: str, /) -> Any: ...

    def execute_script(self, script: str, /) -> Any: ...

    def add_cookies(self, cookies: list[dict[str, str]]) -> Any: ...

    def bypass_cloudflare(self) -> Any: ...

    def close(self) -> Any: ...

    @property
    def _tab(self) -> DriverTabProtocol: ...


def call_if_available[T](
    driver: DriverProtocol,
    name: str,
    /,
    *args: Any,
    default: T = None,  # type: ignore[assignment]
    **kwargs: Any,
) -> T:
    method = getattr(driver, name, None)
    if not callable(method):
        return default
    try:
        return cast(T, method(*args, **kwargs))
    except Exception as exc:
        logger.debug("driver_capability_failed method=%s error=%s", name, exc)
        return default


def call_quietly(
    driver: DriverProtocol, name: str, /, *args: Any, **kwargs: Any
) -> None:
    call_if_available(driver, name, *args, **kwargs)
