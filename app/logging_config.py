"""Logging bootstrap for the service process."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import cast

LOGGER_NAME = "botasaurus_scrape_api"

type LogFieldValue = str | int | float | bool | None


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: LogFieldValue,
) -> None:
    """Emit a named event with typed fields.

    Text mode keeps the historical ``event k=v`` message shape (smoke greps).
    JSON mode attaches ``event_name`` + fields on the LogRecord for the
    formatter; both modes share one call site so text and JSON stay in parity.
    """
    parts = [event]
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    message = " ".join(parts)
    logger.log(level, message, extra={"event_name": event, "event_fields": fields})


class HealthAccessFilter(logging.Filter):
    """Drop uvicorn access records for GET /health."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not ('"GET /health' in message or "GET /health " in message)


class WebsocketTeardownFilter(logging.Filter):
    """Drop websocket logger records (TTL-reap Chromium teardown goodbye).

    Mirrors the Sentry ``before_send`` drop for ``logger=websocket`` so
    ``Connection to remote host was lost. - goodbye`` is not an OpenObserve error.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return False


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return f"{ts} {record.levelname} {record.name} {record.getMessage()}"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event_name = getattr(record, "event_name", None)
        raw_fields = getattr(record, "event_fields", None)
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if isinstance(event_name, str):
            payload["event_name"] = event_name
        if isinstance(raw_fields, dict):
            fields = cast(dict[str, LogFieldValue], raw_fields)
            for key, value in fields.items():
                if key in payload:
                    continue
                payload[key] = value
        return json.dumps(payload, default=str, separators=(",", ":"))


def setup_logging(
    level: int = logging.INFO,
    *,
    log_format: str = "text",
) -> logging.Logger:
    """Configure root + uvicorn loggers. Safe to call more than once."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter())
    root.addHandler(handler)

    health_filter = HealthAccessFilter()
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
        uv_logger.addFilter(health_filter)

    websocket_logger = logging.getLogger("websocket")
    websocket_logger.handlers.clear()
    websocket_logger.propagate = True
    websocket_logger.addFilter(WebsocketTeardownFilter())

    logger = get_logger()
    logger.setLevel(level)
    return logger
