"""Best-effort HTTP metadata extraction from browser driver state."""

import logging
from dataclasses import dataclass
from typing import cast

from app.engine.driver_protocol import DriverProtocol, DriverRequestProtocol
from app.logging_config import get_logger, log_event

logger = get_logger()


@dataclass(frozen=True, slots=True)
class MetadataResult:
    status_code: int | None
    headers: dict[str, str] | None
    final_url: str
    metadata_error: str | None = None


class MetadataExtractor:
    """Deep module extracting passive network response metadata and CDP events."""

    @classmethod
    def extract_from_requests(
        cls, driver: DriverProtocol
    ) -> tuple[int | None, dict[str, str] | None, str | None]:
        reqs = driver.requests
        if not isinstance(reqs, (list, tuple)):
            return None, None, None
        typed_reqs: list[DriverRequestProtocol] = list(
            cast(list[DriverRequestProtocol], reqs)
        )
        for req in reversed(typed_reqs):
            resp = req.response
            if resp is not None and resp.status_code is not None:
                status_code = resp.status_code
                headers = resp.headers
                hdr_dict = (
                    {
                        str(k): str(v)
                        for k, v in cast(dict[object, object], headers).items()
                    }
                    if isinstance(headers, dict)
                    else None
                )
                req_url = req.url
                return int(status_code), hdr_dict, str(req_url) if req_url else None
        return None, None, None

    @classmethod
    def fetch(cls, driver: DriverProtocol, target_url: str) -> MetadataResult:
        final_url = driver.current_url or target_url

        try:
            status_code, headers, passive_url = cls.extract_from_requests(driver)
            if status_code is not None:
                return MetadataResult(
                    status_code=status_code,
                    headers=headers,
                    final_url=passive_url or str(final_url),
                    metadata_error=None,
                )
        except Exception as exc:
            log_event(logger, logging.WARNING, "metadata_fetch_error", error=str(exc))

        return MetadataResult(
            status_code=200,
            headers=None,
            final_url=str(final_url),
            metadata_error=None,
        )
