# app/metadata.py
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("botasaurus_scrape_api")


@dataclass(frozen=True, slots=True)
class MetadataResult:
    status_code: Optional[int]
    headers: Optional[dict[str, str]]
    final_url: str
    metadata_error: Optional[str] = None


class MetadataExtractor:
    """Deep module extracting passive network response metadata and CDP events."""

    @classmethod
    def extract_from_requests(
        cls, driver: Any, target_url: str
    ) -> tuple[Optional[int], Optional[dict[str, str]], Optional[str]]:
        reqs = getattr(driver, "requests", None)
        if isinstance(reqs, (list, tuple)) and reqs:
            for req in reversed(reqs):
                resp = getattr(req, "response", None)
                if resp:
                    status_code = getattr(resp, "status_code", None)
                    headers = getattr(resp, "headers", None)
                    req_url = getattr(req, "url", None)
                    if status_code is not None:
                        hdr_dict = (
                            {str(k): str(v) for k, v in dict(headers).items()}
                            if headers
                            else None
                        )
                        return (
                            int(status_code),
                            hdr_dict,
                            str(req_url) if req_url else None,
                        )
        return None, None, None

    @classmethod
    def extract_from_cdp_logs(
        cls, driver: Any, target_url: str
    ) -> tuple[Optional[int], Optional[dict[str, str]], Optional[str]]:
        get_log = getattr(driver, "get_log", None)
        if not callable(get_log):
            return None, None, None

        try:
            logs = get_log("performance")
            if not isinstance(logs, list):
                return None, None, None

            for entry in reversed(logs):
                raw_msg = (
                    entry.get("message", "{}")
                    if isinstance(entry, dict)
                    else "{}"
                )
                msg_obj = (
                    json.loads(raw_msg)
                    if isinstance(raw_msg, str)
                    else raw_msg
                )
                msg = (
                    msg_obj.get("message", {})
                    if isinstance(msg_obj, dict)
                    else {}
                )
                if msg.get("method") == "Network.responseReceived":
                    params = msg.get("params", {})
                    resp = params.get("response", {})
                    res_type = params.get("type") or resp.get("type")
                    if res_type in ("Document", "Other", None) or not res_type:
                        status_code = resp.get("status")
                        headers = resp.get("headers")
                        url = resp.get("url")
                        if status_code is not None:
                            hdr_dict = (
                                {str(k): str(v) for k, v in headers.items()}
                                if isinstance(headers, dict)
                                else None
                            )
                            return (
                                int(status_code),
                                hdr_dict,
                                str(url) if url else None,
                            )
        except Exception as exc:
            logger.debug("cdp_metadata_extraction_failed error=%s", str(exc))

        return None, None, None

    @classmethod
    def fetch(cls, driver: Any, target_url: str) -> MetadataResult:
        final_url = getattr(driver, "current_url", None) or target_url

        try:
            # 1. Try driver.requests
            status_code, headers, passive_url = cls.extract_from_requests(
                driver, target_url
            )
            if status_code is not None:
                return MetadataResult(
                    status_code=status_code,
                    headers=headers,
                    final_url=passive_url or str(final_url),
                    metadata_error=None,
                )

            # 2. Try CDP performance logs
            status_code, headers, passive_url = cls.extract_from_cdp_logs(
                driver, target_url
            )
            if status_code is not None:
                return MetadataResult(
                    status_code=status_code,
                    headers=headers,
                    final_url=passive_url or str(final_url),
                    metadata_error=None,
                )
        except Exception as exc:
            logger.warning("metadata_fetch_error error=%s", str(exc))

        # Default fallback
        return MetadataResult(
            status_code=200,
            headers=None,
            final_url=str(final_url),
            metadata_error=None,
        )
