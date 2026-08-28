import unittest
from unittest.mock import MagicMock

from app.infra.detector import ChallengeDetector


class ChallengeDetectorUnitTests(unittest.TestCase):
    def test_detects_challenge_marker(self):
        res = ChallengeDetector.detect("<html>Just a moment...</html>", 200)
        self.assertTrue(res.challenge_detected)
        self.assertTrue(res.blocked_detected)
        self.assertEqual(res.detected_marker, "Just a moment...")
        self.assertFalse(res.is_clean)

    def test_detects_http_status_block_without_marker(self):
        res = ChallengeDetector.detect("<html>Forbidden</html>", 403)
        self.assertFalse(res.challenge_detected)
        self.assertTrue(res.blocked_detected)
        self.assertIsNone(res.detected_marker)
        self.assertFalse(res.is_clean)

    def test_clean_response(self):
        res = ChallengeDetector.detect("<html><h1>Hello</h1></html>", 200)
        self.assertTrue(res.is_clean)
        self.assertFalse(res.blocked_detected)
        self.assertFalse(res.challenge_detected)

    def test_soft_challenge_may_retry_strategies(self):
        soft = ChallengeDetector.detect("<html>Just a moment...</html>", 200)
        self.assertTrue(soft.may_retry_strategies(has_more=True))
        self.assertFalse(soft.may_retry_strategies(has_more=False))

    def test_hard_block_does_not_retry_strategies(self):
        hard = ChallengeDetector.detect("<html>Forbidden</html>", 403)
        self.assertFalse(hard.may_retry_strategies(has_more=True))
        self.assertFalse(hard.may_retry_strategies(has_more=False))

    def test_driver_bot_detection_integration(self):
        mock_driver = MagicMock()
        mock_driver.is_bot_detected.return_value = True

        res = ChallengeDetector.detect(
            "<html><body>Clean page</body></html>", 200, driver=mock_driver
        )
        self.assertTrue(res.challenge_detected)
        self.assertTrue(res.blocked_detected)
        self.assertEqual(res.detected_marker, "botasaurus_driver_bot_detected")
