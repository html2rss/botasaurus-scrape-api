"""Best-effort HTTP metadata extraction from browser driver state."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("botasaurus_scrape_api")


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
        cls, driver: Any, target_url: str
    ) -> tuple[int | None, dict[str, str] | None, str | None]:
        reqs = getattr(driver, "requests", None)
        if not isinstance(reqs, (list, tuple)):
            return None, None, None
        for req in reversed(reqs):
            resp = getattr(req, "response", None)
            status_code = getattr(resp, "status_code", None)
            if status_code is not None:
                headers = getattr(resp, "headers", None)
                hdr_dict = (
                    {str(k): str(v) for k, v in dict(headers).items()}
                    if headers
                    else None
                )
                req_url = getattr(req, "url", None)
                return int(status_code), hdr_dict, str(req_url) if req_url else None
        return None, None, None

    @classmethod
    def extract_from_cdp_logs(
        cls, driver: Any, target_url: str
    ) -> tuple[int | None, dict[str, str] | None, str | None]:
        get_log = getattr(driver, "get_log", None)
        if not callable(get_log):
            return None, None, None

        try:
            logs = get_log("performance")
            if not isinstance(logs, list):
                return None, None, None

            for entry in reversed(logs):
                raw_msg = (
                    entry.get("message", "{}") if isinstance(entry, dict) else "{}"
                )
                msg_obj = json.loads(raw_msg) if isinstance(raw_msg, str) else raw_msg
                msg = msg_obj.get("message", {}) if isinstance(msg_obj, dict) else {}
                if msg.get("method") != "Network.responseReceived":
                    continue

                params = msg.get("params", {})
                resp = params.get("response", {})
                res_type = params.get("type") or resp.get("type")
                if res_type and res_type not in ("Document", "Other"):
                    continue

                status_code = resp.get("status")
                if status_code is not None:
                    headers = resp.get("headers")
                    hdr_dict = (
                        {str(k): str(v) for k, v in headers.items()}
                        if isinstance(headers, dict)
                        else None
                    )
                    url = resp.get("url")
                    return int(status_code), hdr_dict, str(url) if url else None
        except Exception as exc:
            logger.debug("cdp_metadata_extraction_failed error=%s", str(exc))

        return None, None, None

    @classmethod
    def fetch(cls, driver: Any, target_url: str) -> MetadataResult:
        final_url = getattr(driver, "current_url", None) or target_url

        try:
            for extractor in (cls.extract_from_requests, cls.extract_from_cdp_logs):
                status_code, headers, passive_url = extractor(driver, target_url)
                if status_code is not None:
                    return MetadataResult(
                        status_code=status_code,
                        headers=headers,
                        final_url=passive_url or str(final_url),
                        metadata_error=None,
                    )
        except Exception as exc:
            logger.warning("metadata_fetch_error error=%s", str(exc))

        return MetadataResult(
            status_code=200,
            headers=None,
            final_url=str(final_url),
            metadata_error=None,
        )
