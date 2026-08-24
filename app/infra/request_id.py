"""Request id resolution from headers with fallback generation."""

from __future__ import annotations

import re
import uuid

from app.logging_config import get_logger

logger = get_logger()

_MAX_LENGTH = 128
_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def resolve_request_id(
    inbound: str | None, *, host: str | None = None
) -> tuple[str, bool]:
    """Return a path-safe request id and whether a fallback uuid4 was generated.

    @param inbound optional inbound header value
    @param host optional target hostname for fallback logging
    @return tuple of resolved id and used_fallback flag
    """
    candidate = inbound.strip() if inbound is not None else None
    if candidate is not None and _is_valid(candidate):
        return candidate, False

    reason = "absent" if not candidate else "invalid"
    logger.info("request_id_fallback host=%s reason=%s", host, reason)
    return str(uuid.uuid4()), True


def _is_valid(value: str | None) -> bool:
    if not value:
        return False
    if len(value) > _MAX_LENGTH:
        return False
    if ".." in value or "/" in value or "\\" in value or "\0" in value:
        return False
    return _SAFE_PATTERN.fullmatch(value) is not None
