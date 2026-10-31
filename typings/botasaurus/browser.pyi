from typing import Any

class Driver:
    page_html: str | None
    current_url: str | None
    requests: list[Any]

    def __init__(
        self,
        *,
        headless: bool = ...,
        enable_xvfb_virtual_display: bool = ...,
        proxy: str | None = ...,
        profile: str | None = ...,
        tiny_profile: bool = ...,
        block_images: bool = ...,
        block_images_and_css: bool = ...,
        wait_for_complete_page_load: bool = ...,
        chrome_executable_path: str | None = ...,
        extensions: list[Any] = ...,
        arguments: list[str] = ...,
        remove_default_browser_check_argument: bool = ...,
        user_agent: str | None = ...,
        window_size: list[int] | None = ...,
        lang: str | None = ...,
        beep: bool = ...,
        host: str | None = ...,
        port: int | None = ...,
    ) -> None: ...
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
    def _tab(self) -> Any: ...
