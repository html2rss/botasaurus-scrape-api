"""Protocols must only declare members the real Botasaurus classes have."""

import unittest
from importlib import import_module
from typing import get_protocol_members

from app.engine.driver_protocol import DriverProtocol, TabProtocol

Driver = import_module("botasaurus_driver.driver").Driver
Tab = import_module("botasaurus_driver.core.tab").Tab


class DriverContractTests(unittest.TestCase):
    def test_driver_protocol_members_exist_on_vendor_driver(self) -> None:
        missing = [
            m for m in get_protocol_members(DriverProtocol) if not hasattr(Driver, m)
        ]
        self.assertEqual(missing, [])

    def test_cdp_tab_protocol_members_exist_on_vendor_tab(self) -> None:
        missing = [m for m in get_protocol_members(TabProtocol) if not hasattr(Tab, m)]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
