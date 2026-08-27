"""Typed optional-driver method adapter for Botasaurus Driver seams."""

from __future__ import annotations

from typing import Any, Protocol, overload, runtime_checkable

from app.logging_config import get_logger

logger = get_logger()


@runtime_checkable
class DriverTabProtocol(Protocol):
    def block_urls(self, patterns: list[str]) -> None: ...

    def set_extra_http_headers(self, headers: dict[str, str]) -> None: ...


@runtime_checkable
class CdpTabProtocol(Protocol):
    def send(self, cdp_obj: Any) -> Any: ...

    def after_response_received(self, handler: Any, /) -> None: ...

    def add_handler(self, event_type: type[Any], handler: Any, /) -> None: ...


@runtime_checkable
class DriverRequestResponseProtocol(Protocol):
    status_code: int | None
    headers: dict[str, str] | object | None


@runtime_checkable
class DriverRequestProtocol(Protocol):
    url: str | None
    response: DriverRequestResponseProtocol | None


@runtime_checkable
class DriverProtocol(Protocol):
    page_html: str | None
    current_url: str | None
    requests: list[DriverRequestProtocol] | tuple[DriverRequestProtocol, ...] | object

    def get(self, url: str, /, **kwargs: Any) -> Any: ...

    def google_get(self, url: str, /, **kwargs: Any) -> Any: ...

    def organic_get(self, url: str, /, **kwargs: Any) -> Any: ...

    def wait_for_element(self, selector: str, /, **kwargs: Any) -> Any: ...

    def sleep(self, seconds: float, /) -> None: ...

    def sleep_random(self, minimum: float, maximum: float, /) -> None: ...

    def scroll_to_bottom(self) -> None: ...

    def scroll(self) -> None: ...

    def run_js(self, script: str, /) -> Any: ...

    def execute_script(self, script: str, /) -> Any: ...

    def add_cookies(self, cookies: list[dict[str, str]]) -> None: ...

    def bypass_cloudflare(self) -> None: ...

    def close(self) -> None: ...

    def get_log(self, log_type: str) -> list[dict[str, str]]: ...

    @property
    def _tab(self) -> CdpTabProtocol: ...


_STOPITERATION_RUNTIME = "StopIteration interacts badly with generators"


def resolve_cdp_tab(driver: DriverProtocol) -> CdpTabProtocol | None:
    """Return ``driver._tab``, or ``None`` when the browser has no page yet.

    Botasaurus exposes ``_tab`` as a property that calls ``get_first_tab()``.
    An empty browser raises ``StopIteration``; under Python 3.14 that can be
    wrapped as ``RuntimeError`` when crossing ``asyncio`` executor boundaries.
    """
    try:
        tab = getattr(driver, "_tab", None)
    except StopIteration as exc:
        logger.debug("driver_capability_failed method=_tab error=%s", exc)
        return None
    except RuntimeError as exc:
        if _STOPITERATION_RUNTIME not in str(exc):
            raise
        logger.debug("driver_capability_failed method=_tab error=%s", exc)
        return None
    return tab


def resolve_callable(
    driver: DriverProtocol | DriverTabProtocol, *names: str
) -> Any | None:
    """Return the first callable attribute among ``names``, else ``None``."""
    for name in names:
        method = getattr(driver, name, None)
        if callable(method):
            return method
    return None


@overload
def call_if_available[T](
    driver: DriverProtocol,
    name: str,
    /,
    *args: Any,
    default: T,
    **kwargs: Any,
) -> T: ...


@overload
def call_if_available(
    driver: DriverProtocol,
    name: str,
    /,
    *args: Any,
    default: None = None,
    **kwargs: Any,
) -> Any: ...


def call_if_available(
    driver: DriverProtocol,
    name: str,
    /,
    *args: Any,
    default: Any = None,
    **kwargs: Any,
) -> Any:
    method = resolve_callable(driver, name)
    if method is None:
        return default
    try:
        return method(*args, **kwargs)
    except Exception as exc:
        logger.debug("driver_capability_failed method=%s error=%s", name, exc)
        return default


def call_quietly(
    driver: DriverProtocol | DriverTabProtocol, name: str, /, *args: Any, **kwargs: Any
) -> None:
    method = resolve_callable(driver, name)
    if method is None:
        return
    try:
        method(*args, **kwargs)
    except Exception as exc:
        logger.debug("driver_capability_failed method=%s error=%s", name, exc)
