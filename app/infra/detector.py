"""Anti-bot challenge detection from HTML and driver signals."""

import logging
from dataclasses import dataclass

from app.engine.driver_protocol import DriverProtocol
from app.logging_config import get_logger, log_event
from app.schemas.response import ChallengeSignal

logger = get_logger()

# Markers shared with html2rss BlockedSurface via tests/fixtures/challenge/.
# One-sided substrings without a shared fixture were deleted in the corpus trim.
_CHALLENGE_MARKERS: tuple[str, ...] = (
    "Just a moment...",
    "checking your browser before accessing",
    "Enable JavaScript and cookies to continue",
    "cf-challenge",
    "cdn-cgi/challenge-platform",
    "cloudflare ray id",
    "captcha-delivery.com",
    "Vercel Security Checkpoint",
    "checking the security",
    "vercel.com/security",
)

_CHALLENGE_MARKERS_PAIRS: tuple[tuple[str, str], ...] = tuple(
    (m, m.lower()) for m in _CHALLENGE_MARKERS
)


@dataclass(frozen=True, slots=True)
class ChallengeAssessment:
    blocked_detected: bool
    challenge_detected: bool
    detected_marker: str | None = None

    @property
    def is_clean(self) -> bool:
        return not self.blocked_detected and not self.challenge_detected

    def to_signal(self) -> ChallengeSignal:
        """Convert domain assessment to wire ChallengeSignal DTO."""
        return ChallengeSignal(
            blocked=self.blocked_detected,
            detected=self.challenge_detected,
            marker=self.detected_marker,
        )


UNREADABLE_SURFACE = ChallengeAssessment(
    blocked_detected=True,
    challenge_detected=True,
    detected_marker="unreadable_surface",
)


class ChallengeDetector:
    """Deep module encapsulating anti-bot challenge heuristics, HTTP status codes, and driver checks."""

    @staticmethod
    def _driver_flags_bot(driver: DriverProtocol) -> bool:
        try:
            return driver.is_bot_detected()
        except Exception as exc:
            log_event(
                logger, logging.WARNING, "driver_bot_detection_failed", error=str(exc)
            )
            return False

    @classmethod
    def detect(
        cls,
        html: str,
        status_code: int | None = None,
        driver: DriverProtocol | None = None,
    ) -> ChallengeAssessment:
        matched_marker: str | None = None

        # 1. Driver-level anti-bot signal (Cloudflare / PerimeterX)
        if driver is not None and cls._driver_flags_bot(driver):
            matched_marker = "botasaurus_driver_bot_detected"

        # 2. HTML text markers (only inspect if driver check did not match)
        if matched_marker is None and html:
            lower_html = html.lower()
            for marker, marker_lower in _CHALLENGE_MARKERS_PAIRS:
                if marker_lower in lower_html:
                    matched_marker = marker
                    break

        challenge_detected = matched_marker is not None
        blocked_detected = challenge_detected or (
            status_code in {401, 403, 429} if status_code is not None else False
        )

        return ChallengeAssessment(
            blocked_detected=blocked_detected,
            challenge_detected=challenge_detected,
            detected_marker=matched_marker,
        )
