# app/sentry.py
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("botasaurus_scrape_api")

_INITIALIZED = False


def _parse_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value.strip())
        return max(0.0, min(1.0, parsed))
    except (ValueError, AttributeError):  # fmt: skip
        return default


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    return default


def is_sentry_enabled() -> bool:
    """Return True if SENTRY_DSN is configured and non-empty."""
    return bool(os.getenv("SENTRY_DSN", "").strip())


def setup_sentry() -> bool:
    """Initialize the Sentry SDK if SENTRY_DSN is configured.

    Reads configuration from environment variables:
    - SENTRY_DSN: Required project DSN. If absent/empty, Sentry is not initialized.
    - SENTRY_ENVIRONMENT / ENVIRONMENT: Deployment environment (default: 'production').
    - SENTRY_RELEASE: Optional release identifier.
    - SENTRY_TRACES_SAMPLE_RATE: APM traces sample rate in [0.0, 1.0] (default: 0.0).
    - SENTRY_PROFILES_SAMPLE_RATE: Profiling sample rate in [0.0, 1.0] (default: 0.0).
    - SENTRY_SEND_DEFAULT_PII: Whether to send default PII (default: False).

    Returns:
        bool: True if Sentry was initialized, False otherwise.
    """
    global _INITIALIZED

    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.warning(
            "sentry_sdk_import_failed SENTRY_DSN is set but sentry-sdk package is not available"
        )
        return False

    environment = (
        os.getenv("SENTRY_ENVIRONMENT") or os.getenv("ENVIRONMENT") or "production"
    ).strip()
    release = os.getenv("SENTRY_RELEASE")
    traces_sample_rate = _parse_float(os.getenv("SENTRY_TRACES_SAMPLE_RATE"), 0.0)
    profiles_sample_rate = _parse_float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE"), 0.0)
    send_default_pii = _parse_bool(os.getenv("SENTRY_SEND_DEFAULT_PII"), default=False)

    init_kwargs: dict[str, Any] = {
        "dsn": dsn,
        "environment": environment,
        "traces_sample_rate": traces_sample_rate,
        "send_default_pii": send_default_pii,
    }

    if release:
        init_kwargs["release"] = release.strip()
    if profiles_sample_rate > 0.0:
        init_kwargs["profiles_sample_rate"] = profiles_sample_rate

    sentry_sdk.init(**init_kwargs)
    _INITIALIZED = True

    logger.info(
        "sentry_initialized environment=%s release=%s traces_sample_rate=%.2f",
        environment,
        release,
        traces_sample_rate,
    )
    return True


def flush_sentry(timeout: float = 2.0) -> None:
    """Flush queued Sentry events on application shutdown."""
    if not _INITIALIZED:
        return
    try:
        import sentry_sdk

        sentry_sdk.flush(timeout=timeout)
    except Exception as exc:
        logger.debug("sentry_flush_failed error=%s", str(exc))
