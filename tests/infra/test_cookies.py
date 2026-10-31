import unittest
from http.cookiejar import Cookie, CookieJar
from typing import Any, cast

from app.engine.driver_protocol import DriverProtocol
from app.infra.cookies import driver_cookies, jar_cookies


def _cookie(name: str, value: str, domain: str) -> Cookie:
    return Cookie(
        0, name, value, None, False, domain, True, domain.startswith("."),
        "/", True, False, None, True, None, None, {},
    )  # fmt: skip


class _Driver:
    def __init__(self, cookies: list[dict[str, Any]]) -> None:
        self._cookies = cookies

    def get_cookies(self) -> list[dict[str, Any]]:
        return self._cookies


class CookieScopingTests(unittest.TestCase):
    def test_driver_cookies_keep_only_target_host_scope(self) -> None:
        driver = _Driver(
            [
                {"name": "datadome", "value": "abc", "domain": ".example.com"},
                {"name": "host", "value": "1", "domain": "www.example.com"},
                {"name": "ads", "value": "x", "domain": ".tracker.net"},
                {"name": "lookalike", "value": "y", "domain": "ample.com"},
            ]
        )
        got = driver_cookies(
            cast(DriverProtocol, driver), "https://www.example.com/feed", None
        )
        self.assertEqual(got, {"datadome": "abc", "host": "1"})

    def test_final_url_host_is_in_scope(self) -> None:
        driver = _Driver([{"name": "a", "value": "1", "domain": "other.org"}])
        got = driver_cookies(
            cast(DriverProtocol, driver), "https://example.com", "https://other.org/x"
        )
        self.assertEqual(got, {"a": "1"})

    def test_driver_read_failure_returns_empty(self) -> None:
        class Broken:
            def get_cookies(self) -> list[dict[str, Any]]:
                raise RuntimeError("cdp gone")

        self.assertEqual(
            driver_cookies(cast(DriverProtocol, Broken()), "https://example.com", None),
            {},
        )

    def test_jar_cookies_scoped(self) -> None:
        jar = CookieJar()
        jar.set_cookie(_cookie("datadome", "abc", ".example.com"))
        jar.set_cookie(_cookie("ads", "x", ".tracker.net"))
        self.assertEqual(
            jar_cookies(jar, "https://example.com", "https://example.com/"),
            {"datadome": "abc"},
        )


if __name__ == "__main__":
    unittest.main()
