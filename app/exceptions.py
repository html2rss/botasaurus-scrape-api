"""Application-specific exceptions with stable semantics across layers."""

from __future__ import annotations


class BotasaurusScrapeError(Exception):
    """Base class for domain errors that map to scrape envelopes."""


class RequestIdCollisionError(BotasaurusScrapeError):
    """Raised when an inbound request id is already active."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__("request id collision detected")
