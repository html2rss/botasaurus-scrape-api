"""Shared test fakes for scrape API layers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar


class FakeMetadataResponse:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "text/html"}
    url = "https://example.com/"


class FakeRequests:
    def get(self, _url: str) -> FakeMetadataResponse:
        return FakeMetadataResponse()


class FakeDriverTab:
    def block_urls(self, patterns: list[str]) -> None:
        del patterns

    def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        del headers


class FakeDriver:
    page_html: str | None
    current_url: str | None
    requests: list[object]

    def __init__(self, *args: object, **kwargs: Any) -> None:
        del args
        self.page_html = "<html><body><h1>Example Domain</h1></body></html>"
        self.current_url = "https://example.com/"
        self.requests = [FakeRequests()]
        self._raise_wait = kwargs.pop("raise_wait", False)
        self.scrolled = False
        self._tab = FakeDriverTab()

    def get(self, *_args: object, **_kwargs: Any) -> None:
        return None

    def google_get(self, *_args: object, **_kwargs: Any) -> None:
        return None

    def organic_get(self, *_args: object, **_kwargs: Any) -> None:
        return None

    def wait_for_element(self, *_args: object, **_kwargs: Any) -> None:
        raise RuntimeError("missing selector")

    def scroll_to_bottom(self) -> None:
        self.scrolled = True

    def scroll(self) -> None:
        return None

    def sleep(self, *_args: object, **_kwargs: Any) -> None:
        return None

    def sleep_random(self, *_args: object, **_kwargs: Any) -> None:
        return None

    def run_js(self, _script: str) -> None:
        return None

    def execute_script(self, _script: str) -> None:
        return None

    def add_cookies(self, _cookies: list[dict[str, str]]) -> None:
        return None

    def bypass_cloudflare(self) -> None:
        return None

    def save_screenshot(self, filename: str) -> None:
        Path(filename).write_bytes(b"fake")

    def close(self) -> None:
        return None

    def get_log(self, _log_type: str) -> list[dict[str, str]]:
        return []


class CaptureDriver(FakeDriver):
    last_init_kwargs: ClassVar[dict[str, Any] | None] = None

    def __init__(self, *args: object, **kwargs: Any) -> None:
        type(self).last_init_kwargs = dict(kwargs)
        super().__init__(*args, **kwargs)


class FakeHttpResponse:
    def __init__(
        self,
        *,
        text: str,
        status_code: int,
        headers: dict[str, str],
        url: str,
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = headers
        self.url = url


class FakeRequest:
    response: FakeHttpResponse | None = None

    def get(self, *_args: object, **_kwargs: Any) -> FakeHttpResponse | None:
        return type(self).response

    def close(self) -> None:
        return None


def fake_request_cls(
    *,
    html: str = "<html>ok</html>",
    url: str = "https://example.com/",
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> type[FakeRequest]:
    """Build a patchable botasaurus Request substitute returning one canned response."""
    response = FakeHttpResponse(
        text=html,
        status_code=status_code,
        headers=headers if headers is not None else {"content-type": "text/html"},
        url=url,
    )
    return type("CannedFakeRequest", (FakeRequest,), {"response": response})


class ArticleDriver(FakeDriver):
    ARTICLE_HTML = (
        "<html><body><article><h1>Headline</h1>"
        "<p>Lead paragraph</p></article></body></html>"
    )

    def __init__(self, *args: object, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.page_html = self.ARTICLE_HTML


class FakeRequestId(str):
    def to_json(self) -> str:
        return str(self)


class FakeNetworkResponse:
    def __init__(
        self,
        url: str,
        status: int,
        mime_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self.mime_type = mime_type
        self.headers = headers or {}


class FakeTab:
    """Minimal CDP tab stub for XhrCollector unit tests."""

    def __init__(self, bodies: dict[str, tuple[str, bool]] | None = None) -> None:
        self.bodies = bodies or {}
        self.network_enabled = False
        self.response_handler: Callable[..., None] | None = None
        self.finished_handler: Callable[..., None] | None = None

    def send(self, cdp_obj: Any) -> Any:
        cmd = next(cdp_obj)
        method = cmd.get("method")
        if method == "Network.enable":
            self.network_enabled = True
            try:
                cdp_obj.send({})
            except StopIteration as exc:
                return exc.value
            return None
        if method == "Network.getResponseBody":
            rid = str(cmd["params"]["requestId"])
            body, b64 = self.bodies.get(rid, ("", False))
            try:
                cdp_obj.send({"body": body, "base64Encoded": b64})
            except StopIteration as exc:
                return exc.value
            return None
        raise AssertionError(f"unexpected CDP method: {method}")

    def after_response_received(self, handler: Callable[..., None]) -> None:
        self.response_handler = handler

    def add_handler(
        self, _event_type: type[object], handler: Callable[..., None]
    ) -> None:
        self.finished_handler = handler
