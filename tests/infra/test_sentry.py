# pyright: reportPrivateUsage=false
"""Sentry setup, readiness, and event filtering tests (exercise module privates)."""

import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import app.infra.sentry as sentry_mod
from app.config import reset_settings_cache


@contextmanager
def env(**values: str):
    with patch.dict(os.environ, values, clear=True):
        reset_settings_cache()
        yield


class SentryIntegrationTests(unittest.TestCase):
    def setUp(self):
        sentry_mod._initialized = False
        reset_settings_cache()

    def test_sentry_is_ready_requires_successful_init(self):
        self.assertFalse(sentry_mod.sentry_is_ready())

        with (
            env(SENTRY_DSN="https://key@sentry.io/123"),
            patch("sentry_sdk.init"),
        ):
            self.assertFalse(sentry_mod.sentry_is_ready())
            self.assertTrue(sentry_mod.setup_sentry())
            self.assertTrue(sentry_mod.sentry_is_ready())

    def test_setup_sentry_noop_when_dsn_absent(self):
        with (
            env(),
            patch("sentry_sdk.init") as mock_init,
        ):
            result = sentry_mod.setup_sentry()
            self.assertFalse(result)
            mock_init.assert_not_called()
            self.assertFalse(sentry_mod._initialized)

    def test_setup_sentry_noop_when_dsn_empty_or_whitespace(self):
        for val in ("", "   ", "\t\n"):
            with (
                self.subTest(val=repr(val)),
                env(SENTRY_DSN=val),
                patch("sentry_sdk.init") as mock_init,
            ):
                result = sentry_mod.setup_sentry()
                self.assertFalse(result)
                mock_init.assert_not_called()
                self.assertFalse(sentry_mod._initialized)

    def test_setup_sentry_initializes_with_defaults(self):
        dsn = "https://key@o123.ingest.sentry.io/456"
        with (
            env(SENTRY_DSN=dsn),
            patch("sentry_sdk.init") as mock_init,
            self.assertLogs("botasaurus_scrape_api", level="INFO") as captured,
        ):
            result = sentry_mod.setup_sentry()

            self.assertTrue(result)
            self.assertTrue(sentry_mod._initialized)
            mock_init.assert_called_once()
            init_kwargs = mock_init.call_args.kwargs
            self.assertEqual(init_kwargs["dsn"], dsn)
            self.assertEqual(init_kwargs["environment"], "production")
            self.assertNotIn("traces_sample_rate", init_kwargs)
            self.assertNotIn("profiles_sample_rate", init_kwargs)
            self.assertFalse(init_kwargs["send_default_pii"])
            self.assertIs(init_kwargs["before_send"], sentry_mod._before_send)
            integration_names = [
                type(integration).__name__
                for integration in init_kwargs["integrations"]
            ]
            self.assertIn("FastApiIntegration", integration_names)
            self.assertIn("StarletteIntegration", integration_names)

            log_output = "\n".join(captured.output)
            self.assertIn("sentry_initialized", log_output)
            self.assertIn("environment=production", log_output)
            self.assertNotIn("key@o123", log_output)

    def test_setup_sentry_reads_custom_env_options(self):
        dsn = "https://key@o123.ingest.sentry.io/456"
        with (
            env(
                SENTRY_DSN=dsn,
                SENTRY_ENVIRONMENT="staging",
                SENTRY_RELEASE="v2.0.0+abc1234",
                SENTRY_SEND_DEFAULT_PII="true",
            ),
            patch("sentry_sdk.init") as mock_init,
        ):
            result = sentry_mod.setup_sentry()

            self.assertTrue(result)
            mock_init.assert_called_once()
            init_kwargs = mock_init.call_args.kwargs
            self.assertEqual(init_kwargs["dsn"], dsn)
            self.assertEqual(init_kwargs["environment"], "staging")
            self.assertEqual(init_kwargs["release"], "v2.0.0+abc1234")
            self.assertTrue(init_kwargs["send_default_pii"])
            self.assertIs(init_kwargs["before_send"], sentry_mod._before_send)

    def test_before_send_drops_websocket_teardown(self):
        dropped = sentry_mod._before_send(
            {
                "logger": "websocket",
                "logentry": {"formatted": "Connection to remote host was lost"},
            },
            {},
        )
        self.assertIsNone(dropped)

        kept = sentry_mod._before_send({"logger": "botasaurus_scrape_api"}, {})
        self.assertIsNotNone(kept)

    def test_flush_sentry_noop_when_not_initialized(self):
        with patch("sentry_sdk.flush") as mock_flush:
            sentry_mod.flush_sentry()
            mock_flush.assert_not_called()

    def test_flush_sentry_invokes_sdk_flush_when_initialized(self):
        sentry_mod._initialized = True
        with patch("sentry_sdk.flush") as mock_flush:
            sentry_mod.flush_sentry(timeout=3.0)
            mock_flush.assert_called_once_with(timeout=3.0)

    def test_flush_sentry_swallows_exceptions_cleanly(self):
        sentry_mod._initialized = True
        with patch("sentry_sdk.flush", side_effect=RuntimeError("flush timeout")):
            sentry_mod.flush_sentry()


if __name__ == "__main__":
    unittest.main()
