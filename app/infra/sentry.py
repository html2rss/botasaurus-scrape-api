from __future__ import annotations

import logging
from typing import Any

from app.config import Settings

logger = logging.getLogger("botasaurus_scrape_api")

_INITIALIZED = False


def is_sentry_enabled(settings: Settings | None = None) -> bool:
    from app.config import get_settings

    resolved = settings or get_settings()
    return bool(resolved.sentry_dsn.strip())


def sentry_is_ready() -> bool:
    """Return True when Sentry DSN is set and init succeeded."""
    return is_sentry_enabled() and _INITIALIZED


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    tags = event.get("tags") or {}
    if tags.get("error_category") == "challenge_block":
        return None

    log_record = hint.get("log_record")
    logger_name = event.get("logger")
    if logger_name == "websocket" or getattr(log_record, "name", None) == "websocket":
        return None

    logentry = event.get("logentry") or {}
    message = logentry.get("formatted") or event.get("message") or ""
    if "Connection to remote host was lost" in str(message):
        return None

    return event


def setup_sentry(settings: Settings | None = None) -> bool:
    """Initialize Sentry when SENTRY_DSN is set. Returns True on success."""
    global _INITIALIZED

    from app.config import get_settings

    resolved = settings or get_settings()
    dsn = resolved.sentry_dsn.strip()
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

    traces_sample_rate = max(0.0, min(1.0, resolved.sentry_traces_sample_rate))
    profiles_sample_rate = max(0.0, min(1.0, resolved.sentry_profiles_sample_rate))

    init_kwargs: dict[str, Any] = {
        "dsn": dsn,
        "environment": resolved.effective_sentry_environment,
        "traces_sample_rate": traces_sample_rate,
        "send_default_pii": resolved.sentry_send_default_pii,
        "integrations": [
            FastApiIntegration(),
            StarletteIntegration(),
        ],
        "before_send": _before_send,
    }

    if resolved.sentry_release.strip():
        init_kwargs["release"] = resolved.sentry_release.strip()
    if profiles_sample_rate > 0.0:
        init_kwargs["profiles_sample_rate"] = profiles_sample_rate

    sentry_sdk.init(**init_kwargs)
    _INITIALIZED = True

    logger.info(
        "sentry_initialized environment=%s release=%s traces_sample_rate=%.2f",
        resolved.effective_sentry_environment,
        resolved.sentry_release or None,
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
