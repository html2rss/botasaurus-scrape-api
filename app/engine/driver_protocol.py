"""Typed seams over the Botasaurus Driver.

Every member is verified against the real ``Driver``/``Tab`` classes in
``tests/engine/test_driver_contract.py``; do not declare methods the vendor lacks.
"""

import logging
from typing import Any, Protocol

from app.logging_config import get_logger, log_event

logger = get_logger()


class CdpTabProtocol(Protocol):
    def send(self, cdp_obj: Any) -> Any: ...

    def after_response_received(self, handler: Any, /) -> None: ...

    def add_handler(self, event_type: type[Any], handler: Any, /) -> None: ...


class TabProtocol(CdpTabProtocol, Protocol):
    def block_urls(self, urls: list[str]) -> None: ...


class DriverRequestResponseProtocol(Protocol):
    status_code: int | None
    headers: dict[str, str] | object | None


class DriverRequestProtocol(Protocol):
    url: str | None
    response: DriverRequestResponseProtocol | None


class DriverProtocol(Protocol):
    page_html: str | None
    current_url: str | None
    requests: object

    def get(self, url: str, /, **kwargs: Any) -> Any: ...

    def google_get(self, url: str, /, **kwargs: Any) -> Any: ...

    def wait_for_element(self, selector: str, /, **kwargs: Any) -> Any: ...

    def sleep(self, seconds: float, /) -> None: ...

    def short_random_sleep(self) -> None: ...

    def scroll_to_bottom(self) -> None: ...

    def add_cookies(self, cookies: list[dict[str, str]]) -> None: ...

    def detect_and_bypass_cloudflare(self) -> None: ...

    def is_bot_detected(self) -> bool: ...

    def close(self) -> None: ...

    @property
    def _tab(self) -> TabProtocol: ...


_STOPITERATION_RUNTIME = "StopIteration interacts badly with generators"


def resolve_cdp_tab(driver: DriverProtocol) -> TabProtocol | None:
    """Return ``driver._tab``, or ``None`` when the browser has no page yet.

    Botasaurus exposes ``_tab`` as a property that calls ``get_first_tab()``.
    An empty browser raises ``StopIteration``; a generator boundary turns that
    into ``RuntimeError``. Both are treated as no tab.
    """
    try:
        tab = driver._tab  # pyright: ignore[reportPrivateUsage]
    except StopIteration as exc:
        logger.debug("driver_tab_unavailable error=%s", exc)
        return None
    except RuntimeError as exc:
        if _STOPITERATION_RUNTIME not in str(exc):
            raise
        logger.debug("driver_tab_unavailable error=%s", exc)
        return None
    return tab


def close_driver(driver: DriverProtocol) -> None:
    """Best-effort ``driver.close()``; failures are logged, never raised."""
    try:
        driver.close()
    except Exception as exc:
        log_event(logger, logging.WARNING, "driver_close_failed", error=str(exc))
