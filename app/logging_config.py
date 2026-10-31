"""Logging bootstrap for the service process."""

import contextvars
import json
import logging
import sys
import traceback
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from types import MappingProxyType
from typing import cast

LOGGER_NAME = "botasaurus_scrape_api"

type LogFieldValue = str | int | float | bool | None


_EXC_STACK_MAX_CHARS = 4000
_request_context: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "request_log_context", default=MappingProxyType({})
)


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


@contextmanager
def request_log_context(**fields: str) -> Generator[None]:
    """Attach ``fields`` (request_id, host) to every JSON record in this scope.

    Copy the context into worker threads (``contextvars.copy_context().run``).
    """
    token = _request_context.set({**_request_context.get(), **fields})
    try:
        yield
    finally:
        _request_context.reset(token)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: bool = False,
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
    logger.log(
        level,
        message,
        exc_info=exc_info,
        extra={"event_name": event, "event_fields": fields},
    )


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
    """One JSON object per record.

    Event records drop the ``k=v`` ``msg`` (typed fields carry the same data);
    ``None`` fields are omitted; request context and exceptions are structured.
    """

    def format(self, record: logging.LogRecord) -> str:
        event_name = getattr(record, "event_name", None)
        raw_fields = getattr(record, "event_fields", None)
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        if isinstance(event_name, str):
            payload["event_name"] = event_name
        else:
            payload["msg"] = record.getMessage()
        payload.update(_request_context.get())
        if isinstance(raw_fields, dict):
            fields = cast(dict[str, LogFieldValue], raw_fields)
            payload.update({k: v for k, v in fields.items() if v is not None})
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            payload["exc_type"] = type(exc).__name__
            payload["exc_message"] = str(exc)
            payload["exc_stack"] = "".join(
                traceback.format_exception(*record.exc_info)
            )[-_EXC_STACK_MAX_CHARS:]
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

    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True

    websocket_logger = logging.getLogger("websocket")
    websocket_logger.handlers.clear()
    websocket_logger.propagate = True
    websocket_logger.addFilter(WebsocketTeardownFilter())

    logger = get_logger()
    logger.setLevel(level)
    return logger
