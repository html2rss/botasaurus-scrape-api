from typing import Any

class Response:
    status_code: int
    text: str
    headers: dict[str, str]

    def json(self) -> dict[str, Any]: ...
    def raise_for_status(self) -> None: ...
