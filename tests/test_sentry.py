# tests/test_sentry.py
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import app.sentry as sentry_mod
from app.sentry import flush_sentry, is_sentry_enabled, setup_sentry


class SentryIntegrationTests(unittest.TestCase):
    def setUp(self):
        sentry_mod._INITIALIZED = False

    def test_is_sentry_enabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_sentry_enabled())

        with patch.dict(os.environ, {"SENTRY_DSN": ""}):
            self.assertFalse(is_sentry_enabled())

        with patch.dict(os.environ, {"SENTRY_DSN": "   "}):
            self.assertFalse(is_sentry_enabled())

        with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/123"}):
            self.assertTrue(is_sentry_enabled())

    def test_setup_sentry_noop_when_dsn_absent(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sentry_sdk.init") as mock_init,
        ):
            result = setup_sentry()
            self.assertFalse(result)
            mock_init.assert_not_called()
            self.assertFalse(sentry_mod._INITIALIZED)

    def test_setup_sentry_noop_when_dsn_empty_or_whitespace(self):
        for val in ("", "   ", "\t\n"):
            with (
                self.subTest(val=repr(val)),
                patch.dict(os.environ, {"SENTRY_DSN": val}),
                patch("sentry_sdk.init") as mock_init,
            ):
                result = setup_sentry()
                self.assertFalse(result)
                mock_init.assert_not_called()
                self.assertFalse(sentry_mod._INITIALIZED)

    def test_setup_sentry_initializes_with_defaults(self):
        dsn = "https://key@o123.ingest.sentry.io/456"
        with (
            patch.dict(os.environ, {"SENTRY_DSN": dsn}, clear=True),
            patch("sentry_sdk.init") as mock_init,
            self.assertLogs("botasaurus_scrape_api", level="INFO") as captured,
        ):
            result = setup_sentry()

            self.assertTrue(result)
            self.assertTrue(sentry_mod._INITIALIZED)
            mock_init.assert_called_once_with(
                dsn=dsn,
                environment="production",
                traces_sample_rate=0.0,
                send_default_pii=False,
            )

            log_output = "\n".join(captured.output)
            self.assertIn("sentry_initialized", log_output)
            self.assertIn("environment=production", log_output)
            self.assertNotIn("key@o123", log_output)

    def test_setup_sentry_reads_custom_env_options(self):
        dsn = "https://key@o123.ingest.sentry.io/456"
        env_vars = {
            "SENTRY_DSN": dsn,
            "SENTRY_ENVIRONMENT": "staging",
            "SENTRY_RELEASE": "v2.0.0+abc1234",
            "SENTRY_TRACES_SAMPLE_RATE": "0.25",
            "SENTRY_PROFILES_SAMPLE_RATE": "0.10",
            "SENTRY_SEND_DEFAULT_PII": "true",
        }
        with (
            patch.dict(os.environ, env_vars, clear=True),
            patch("sentry_sdk.init") as mock_init,
        ):
            result = setup_sentry()

            self.assertTrue(result)
            mock_init.assert_called_once_with(
                dsn=dsn,
                environment="staging",
                release="v2.0.0+abc1234",
                traces_sample_rate=0.25,
                profiles_sample_rate=0.10,
                send_default_pii=True,
            )

    def test_setup_sentry_clamps_sample_rates_and_handles_invalid_floats(self):
        dsn = "https://key@o123.ingest.sentry.io/456"
        # Invalid string falls back to 0.0
        with (
            patch.dict(
                os.environ,
                {"SENTRY_DSN": dsn, "SENTRY_TRACES_SAMPLE_RATE": "invalid_float"},
                clear=True,
            ),
            patch("sentry_sdk.init") as mock_init,
        ):
            result = setup_sentry()
            self.assertTrue(result)
            mock_init.assert_called_once_with(
                dsn=dsn,
                environment="production",
                traces_sample_rate=0.0,
                send_default_pii=False,
            )

        # > 1.0 is clamped to 1.0
        sentry_mod._INITIALIZED = False
        with (
            patch.dict(
                os.environ,
                {"SENTRY_DSN": dsn, "SENTRY_TRACES_SAMPLE_RATE": "2.5"},
                clear=True,
            ),
            patch("sentry_sdk.init") as mock_init,
        ):
            result = setup_sentry()
            self.assertTrue(result)
            mock_init.assert_called_once_with(
                dsn=dsn,
                environment="production",
                traces_sample_rate=1.0,
                send_default_pii=False,
            )

    def test_flush_sentry_noop_when_not_initialized(self):
        with patch("sentry_sdk.flush") as mock_flush:
            flush_sentry()
            mock_flush.assert_not_called()

    def test_flush_sentry_invokes_sdk_flush_when_initialized(self):
        sentry_mod._INITIALIZED = True
        with patch("sentry_sdk.flush") as mock_flush:
            flush_sentry(timeout=3.0)
            mock_flush.assert_called_once_with(timeout=3.0)

    def test_flush_sentry_swallows_exceptions_cleanly(self):
        sentry_mod._INITIALIZED = True
        with patch("sentry_sdk.flush", side_effect=RuntimeError("flush timeout")):
            # Should not raise
            flush_sentry()


if __name__ == "__main__":
    unittest.main()
