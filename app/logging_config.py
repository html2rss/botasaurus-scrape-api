"""Logging bootstrap for the service process."""

from __future__ import annotations

import logging

LOGGER_NAME = "botasaurus_scrape_api"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
