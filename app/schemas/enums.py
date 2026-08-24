from __future__ import annotations

from enum import StrEnum


class ExecutionMode(StrEnum):
    AUTO = "auto"
    REQUEST = "request"
    BROWSER = "browser"


class NavigationMode(StrEnum):
    AUTO = "auto"
    GET = "get"
    GOOGLE_GET = "google_get"
    GOOGLE_GET_BYPASS = "google_get_bypass"
    ORGANIC_GET = "organic_get"


class ExecutionTier(StrEnum):
    HTTP_REQUEST = "http_request"
    BROWSER_DRIVER = "browser_driver"


class ErrorCategory(StrEnum):
    TIMEOUT = "timeout"
    CHALLENGE_BLOCK = "challenge_block"
    NAVIGATION_ERROR = "navigation_error"
    METADATA_ERROR = "metadata_error"
    VALIDATION = "validation"


class TimeoutPhase(StrEnum):
    QUEUE = "queue"
    BOOT = "boot"
    WORK = "work"
