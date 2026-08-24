from __future__ import annotations

from typing import Any, Protocol, TypedDict, cast

from app.config import SentrySettings, Settings
from app.logging_config import get_logger

logger = get_logger()

_initialized = False


class SentryEventHint(TypedDict, total=False):
    log_record: object


class SentryEvent(TypedDict, total=False):
    tags: dict[str, str]
    logger: str
    logentry: dict[str, str]
    message: str


class SentryBeforeSend(Protocol):
    def __call__(
        self, event: SentryEvent, hint: SentryEventHint
    ) -> SentryEvent | None: ...


def sentry_is_ready() -> bool:
    """Return True when Sentry init succeeded (init requires a DSN)."""
    return _initialized


def _before_send(event: SentryEvent, hint: SentryEventHint) -> SentryEvent | None:
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
    global _initialized

    from app.config import get_settings

    resolved = settings or get_settings()
    return _setup_sentry(resolved.sentry, deployment_environment=resolved.environment)


def _setup_sentry(sentry: SentrySettings, *, deployment_environment: str) -> bool:
    global _initialized

    dsn = sentry.dsn.strip()
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

    init_kwargs: dict[str, Any] = {
        "dsn": dsn,
        "environment": sentry.effective_environment(deployment_environment),
        "traces_sample_rate": sentry.traces_sample_rate,
        "send_default_pii": sentry.send_default_pii,
        "integrations": [
            FastApiIntegration(),
            StarletteIntegration(),
        ],
        "before_send": cast(SentryBeforeSend, _before_send),
    }

    if sentry.release.strip():
        init_kwargs["release"] = sentry.release.strip()
    if sentry.profiles_sample_rate > 0.0:
        init_kwargs["profiles_sample_rate"] = sentry.profiles_sample_rate

    sentry_sdk.init(**init_kwargs)
    _initialized = True

    logger.info(
        "sentry_initialized environment=%s release=%s traces_sample_rate=%.2f",
        sentry.effective_environment(deployment_environment),
        sentry.release or None,
        sentry.traces_sample_rate,
    )
    return True


def flush_sentry(timeout: float = 2.0) -> None:
    if not _initialized:
        return
    try:
        import sentry_sdk

        sentry_sdk.flush(timeout=timeout)
    except Exception as exc:
        logger.debug("sentry_flush_failed error=%s", str(exc))
