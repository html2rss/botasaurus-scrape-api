"""TypedDict shape for XHR capture state."""

from typing import TypedDict


class PendingXhrMeta(TypedDict):
    url: str
    status_code: int
    headers: dict[str, str]
    request_id: str | int
