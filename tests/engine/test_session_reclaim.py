"""ScrapeSession force_close remains effective after early reclaim."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.config import get_settings
from app.engine import ScraperEngine
from app.engine.session import ScrapeSession
from app.engine.work_lease import WorkLease


class SessionReclaimTests(unittest.TestCase):
    def test_early_reclaim_without_driver_still_closes_later_assign(self) -> None:
        """Deadline reclaim during cold boot must not skip the later driver close."""
        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            lease = WorkLease.tracking_only(engine.settings)
            with ScrapeSession(engine, "req-reclaim-boot", lease=lease) as session:
                lease.reclaim()  # driver still None (cold boot race)
                driver = MagicMock()
                session.driver = driver
            driver.close.assert_called_once()

    def test_reclaim_closes_assigned_driver_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            lease = WorkLease.tracking_only(engine.settings)
            with ScrapeSession(engine, "req-reclaim-once", lease=lease) as session:
                driver = MagicMock()
                session.driver = driver
                lease.reclaim()
                lease.reclaim()
            driver.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
