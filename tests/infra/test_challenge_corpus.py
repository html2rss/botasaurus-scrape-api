"""Shared challenge corpus: fixtures asserted by ChallengeDetector."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.infra.detector import ChallengeDetector

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "challenge"

_POSITIVE = (
    "cloudflare_interstitial.html",
    "datadome_interstitial.html",
    "vercel_checkpoint.html",
)


class ChallengeCorpusTests(unittest.TestCase):
    def test_positive_fixtures_are_unclean(self) -> None:
        for name in _POSITIVE:
            with self.subTest(fixture=name):
                html = (_FIXTURE_DIR / name).read_text(encoding="utf-8")
                assessment = ChallengeDetector.detect(html, 200)
                self.assertFalse(assessment.is_clean, msg=name)
                self.assertTrue(assessment.blocked_detected, msg=name)

    def test_clean_fixture_is_clean(self) -> None:
        html = (_FIXTURE_DIR / "clean.html").read_text(encoding="utf-8")
        assessment = ChallengeDetector.detect(html, 200)
        self.assertTrue(assessment.is_clean)

    def test_cloudflare_fixture_reports_marker(self) -> None:
        html = (_FIXTURE_DIR / "cloudflare_interstitial.html").read_text(
            encoding="utf-8"
        )
        assessment = ChallengeDetector.detect(html, 200)
        self.assertIsNotNone(assessment.detected_marker)
        assert assessment.detected_marker is not None
        self.assertIn("moment", assessment.detected_marker.lower())


if __name__ == "__main__":
    unittest.main()
