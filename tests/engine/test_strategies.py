"""Unit tests for strategy resolution and driver scrolling helpers."""

from __future__ import annotations

import unittest
from typing import cast
from unittest.mock import MagicMock

from app.engine.driver_capabilities import DriverProtocol
from app.engine.strategies import apply_scrolling, configure_driver, resolve_strategies
from app.schemas.enums import NavigationMode
from tests.support.factories import scrape_request


class StrategyTests(unittest.TestCase):
    def test_strategy_selection(self) -> None:
        self.assertEqual(
            resolve_strategies(NavigationMode.AUTO, 0),
            [NavigationMode.GOOGLE_GET],
        )
        self.assertEqual(
            resolve_strategies(NavigationMode.AUTO, 2),
            [
                NavigationMode.GOOGLE_GET,
                NavigationMode.GOOGLE_GET_BYPASS,
                NavigationMode.GET,
            ],
        )
        self.assertEqual(
            resolve_strategies(NavigationMode.GET, 2),
            [NavigationMode.GET, NavigationMode.GET, NavigationMode.GET],
        )
        self.assertEqual(
            resolve_strategies(NavigationMode.ORGANIC_GET, 2),
            [
                NavigationMode.ORGANIC_GET,
                NavigationMode.ORGANIC_GET,
                NavigationMode.ORGANIC_GET,
            ],
        )

    def test_apply_scrolling(self) -> None:
        mock_driver = MagicMock()
        mock_driver.scroll_to_bottom = MagicMock()
        apply_scrolling(mock_driver)
        mock_driver.scroll_to_bottom.assert_called_once()

    def test_configure_driver_empty_tab_stopiteration(self) -> None:
        class EmptyTabDriver:
            @property
            def _tab(self) -> object:
                raise StopIteration

        payload = scrape_request(
            block_trackers=True,
            headers={"X-Test": "1"},
        )
        configure_driver(
            cast(DriverProtocol, EmptyTabDriver()),
            payload,
            "https://example.com/",
        )


if __name__ == "__main__":
    unittest.main()
