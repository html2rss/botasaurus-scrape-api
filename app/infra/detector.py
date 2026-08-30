"""Anti-bot challenge detection from HTML and driver signals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from app.engine.driver_capabilities import DriverProtocol, call_if_available
from app.schemas.response import ChallengeSignal

_CHALLENGE_MARKERS: tuple[str, ...] = (
    "challenge-error-text",
    "Enable JavaScript and cookies to continue",
    "Just a moment...",
    "cf-challenge",
    "cf-turnstile",
    "captcha-delivery.com",
    "datadome",
    "DataDome CAPTCHA",
    "/captcha/?",
    "attention required! | cloudflare",
    "cloudflare-ray-id",
    "shield.recaptcha.net",
    "geo.captcha-delivery.com",
)

_CHALLENGE_MARKERS_PAIRS: tuple[tuple[str, str], ...] = tuple(
    (m, m.lower()) for m in _CHALLENGE_MARKERS
)

_DRIVER_SIGNAL_METHODS: tuple[tuple[str, str], ...] = (
    ("is_bot_detected", "botasaurus_driver_bot_detected"),
    ("is_in_challenge", "botasaurus_driver_challenge"),
    ("is_blocked", "botasaurus_driver_blocked"),
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


class ChallengeDetector:
    """Deep module encapsulating anti-bot challenge heuristics, HTTP status codes, and driver checks."""

    @classmethod
    def detect(
        cls,
        html: str,
        status_code: int | None = None,
        driver: object | None = None,
    ) -> ChallengeAssessment:
        matched_marker: str | None = None

        # 1. Driver-level anti-bot signal inspection
        if driver is not None:
            typed = cast(DriverProtocol, driver)
            for method_name, marker_label in _DRIVER_SIGNAL_METHODS:
                if call_if_available(typed, method_name, default=False):
                    matched_marker = marker_label
                    break

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
