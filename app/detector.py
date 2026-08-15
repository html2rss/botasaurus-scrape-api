# app/detector.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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
)


@dataclass(frozen=True, slots=True)
class ChallengeAssessment:
    blocked_detected: bool
    challenge_detected: bool
    detected_marker: Optional[str] = None
    error_category: Optional[str] = None

    @property
    def is_clean(self) -> bool:
        return not self.blocked_detected and not self.challenge_detected


class ChallengeDetector:
    """Deep module encapsulating anti-bot challenge heuristics and HTTP status blocks."""

    @classmethod
    def detect(cls, html: str, status_code: Optional[int]) -> ChallengeAssessment:
        lower_html = html.lower()
        matched_marker: Optional[str] = None

        for marker in _CHALLENGE_MARKERS:
            if marker.lower() in lower_html:
                matched_marker = marker
                break

        challenge_detected = matched_marker is not None
        blocked_detected = challenge_detected or (
            status_code in {401, 403, 429} if status_code is not None else False
        )

        error_category = "challenge_block" if (challenge_detected or blocked_detected) else None

        return ChallengeAssessment(
            blocked_detected=blocked_detected,
            challenge_detected=challenge_detected,
            detected_marker=matched_marker,
            error_category=error_category,
        )
