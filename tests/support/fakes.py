"""Shared test fakes for scrape API layers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar


class FakeMetadataResponse:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "text/html"}
    url = "https://example.com/"


class FakeRequests:
    def get(self, _url):
        return FakeMetadataResponse()


class FakeDriver:
    def __init__(self, *args, **kwargs):
        self.page_html = "<html><body><h1>Example Domain</h1></body></html>"
        self.current_url = "https://example.com/"
        self.requests = FakeRequests()
        self._raise_wait = kwargs.pop("raise_wait", False)
        self.scrolled = False

    def get(self, *_args, **_kwargs):
        return None

    def google_get(self, *_args, **_kwargs):
        return None

    def organic_get(self, *_args, **_kwargs):
        return None

    def wait_for_element(self, *_args, **_kwargs):
        raise RuntimeError("missing selector")

    def scroll_to_bottom(self):
        self.scrolled = True

    def sleep(self, *_args, **_kwargs):
        return None

    def save_screenshot(self, filename):
        Path(filename).write_bytes(b"fake")

    def close(self):
        return None


class CaptureDriver(FakeDriver):
    last_init_kwargs: ClassVar[dict[str, Any] | None] = None

    def __init__(self, *args, **kwargs):
        type(self).last_init_kwargs = dict(kwargs)
        super().__init__(*args, **kwargs)


class FakeHttpResponse:
    def __init__(self, *, text, status_code, headers, url):
        self.text = text
        self.status_code = status_code
        self.headers = headers
        self.url = url


class FakeRequest:
    response: FakeHttpResponse | None = None

    def get(self, *_args, **_kwargs):
        return type(self).response

    def close(self):
        return None


class ArticleDriver(FakeDriver):
    ARTICLE_HTML = (
        "<html><body><article><h1>Headline</h1>"
        "<p>Lead paragraph</p></article></body></html>"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.page_html = self.ARTICLE_HTML


class FakeRequestId(str):
    def to_json(self):
        return str(self)


class FakeNetworkResponse:
    def __init__(self, url, status, mime_type, headers=None):
        self.url = url
        self.status = status
        self.mime_type = mime_type
        self.headers = headers or {}


class FakeTab:
    """Minimal CDP tab stub for XhrCollector unit tests."""

    def __init__(self, bodies=None):
        self.bodies = bodies or {}
        self.network_enabled = False
        self.response_handler = None
        self.finished_handler = None

    def send(self, cdp_obj):
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

    def after_response_received(self, handler):
        self.response_handler = handler

    def add_handler(self, _event_type, handler):
        self.finished_handler = handler
