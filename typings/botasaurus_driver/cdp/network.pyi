from collections.abc import Generator
from typing import Any

RequestId = str | int

class LoadingFinished:
    request_id: RequestId

    def __init__(self, *, request_id: RequestId) -> None: ...

class NetworkResponse:
    url: str
    status: int
    mime_type: str | None
    headers: dict[str, str]

def get_response_body(
    request_id: RequestId,
) -> Generator[dict[str, Any], dict[str, Any], tuple[str, bool]]: ...
