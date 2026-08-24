from typing import Any, Literal

class HttpResponse:
    text: str | None
    status_code: int | None
    headers: dict[str, str]
    url: str

class Request:
    def get(
        self,
        url: str,
        /,
        *,
        referer: str = ...,
        params: dict[str, Any] | None = ...,
        data: Any = ...,
        headers: dict[str, str] | None = ...,
        browser: Literal["firefox", "chrome"] | None = ...,
        os: Literal["windows", "mac", "linux"] | None = ...,
        user_agent: str | None = ...,
        cookies: dict[str, str] | None = ...,
        files: Any = ...,
        auth: Any = ...,
        timeout: int | None = ...,
        allow_redirects: bool = ...,
        proxies: dict[str, str] | None = ...,
        hooks: Any = ...,
        stream: bool | None = ...,
        verify: bool | None = ...,
        cert: Any = ...,
        json: Any = ...,
    ) -> HttpResponse: ...
    def close(self) -> None: ...
