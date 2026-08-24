"""TypedDict shapes for CDP log entries and XHR capture state."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class CdpNetworkResponse(TypedDict, total=False):
    status: int
    headers: dict[str, str]
    url: str
    type: str


class CdpResponseReceivedParams(TypedDict, total=False):
    type: str
    response: CdpNetworkResponse


class CdpResponseReceivedMessage(TypedDict, total=False):
    method: str
    params: CdpResponseReceivedParams


class CdpLogMessageEnvelope(TypedDict, total=False):
    message: CdpResponseReceivedMessage


class CdpPerformanceLogEntry(TypedDict):
    message: str


class PendingXhrMeta(TypedDict):
    url: str
    status_code: int
    headers: dict[str, str]
    request_id: NotRequired[str | int]
