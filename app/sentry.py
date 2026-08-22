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
    return bool(os.getenv("SENTRY_DSN", "").strip())


def sentry_is_ready() -> bool:
    """Return True when Sentry DSN is set and init succeeded."""
    return is_sentry_enabled() and _INITIALIZED


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    tags = event.get("tags") or {}
    if tags.get("error_category") == "challenge_block":
        return None
    return event


def setup_sentry() -> bool:
    """Initialize Sentry when SENTRY_DSN is set. Returns True on success."""
    global _INITIALIZED

    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
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
        "integrations": [
            FastApiIntegration(),
            StarletteIntegration(),
        ],
        "before_send": _before_send,
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
    if not _INITIALIZED:
        return
    try:
        import sentry_sdk

        sentry_sdk.flush(timeout=timeout)
    except Exception as exc:
        logger.debug("sentry_flush_failed error=%s", str(exc))
