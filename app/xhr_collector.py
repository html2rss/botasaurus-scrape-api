# app/xhr_collector.py
"""Collect JSON XHR/fetch response bodies via CDP network events.

Phase 0 finding: ``Network.getResponseBody`` deadlocks when called from inside a
``LoadingFinished`` handler (CDP session re-entrancy). Handlers only record
metadata / readiness; ``harvest()`` fetches bodies on the caller thread after
navigation/scroll completes.
"""

from __future__ import annotations

import base64
import logging
import threading
from typing import Any

from botasaurus_driver import cdp
from botasaurus_driver.core.custom_storage_cdp import enable_network

logger = logging.getLogger("botasaurus_scrape_api")


class XhrCollector:
    """Collects JSON XHR/fetch response bodies via CDP handlers."""

    MAX_RESPONSES = 20
    MAX_BODY_BYTES = 500_000
    # Mirror html2rss BotasaurusContract::ParsedResponse::MAX_XHR_AGGREGATE_BYTES.
    MAX_AGGREGATE_BYTES = 2_000_000
    _HEADER_ALLOWLIST = frozenset({"content-type"})

    def __init__(self, target_url: str) -> None:
        self._target_url = str(target_url).rstrip("/")
        self._pending: dict[str, dict[str, Any]] = {}
        self._ready_ids: list[str] = []
        self._collected: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def install(self, tab: Any) -> None:
        """Enable the network domain and register handlers before navigation."""
        tab.send(enable_network())
        tab.after_response_received(self._on_response)
        tab.add_handler(cdp.network.LoadingFinished, self._on_finished)

    def reset(self) -> None:
        """Clear pending/ready/collected state between strategy attempts."""
        with self._lock:
            self._pending.clear()
            self._ready_ids.clear()
            self._collected.clear()

    @classmethod
    def _allowlisted_headers(cls, headers: Any) -> dict[str, str]:
        """Keep only content-type; drop Set-Cookie and other headers."""
        allowed: dict[str, str] = {}
        for key, value in dict(headers or {}).items():
            normalized = str(key).lower()
            if normalized in cls._HEADER_ALLOWLIST:
                allowed[normalized] = str(value)
        return allowed

    def _on_response(self, request_id: Any, response: Any, _event: Any) -> None:
        url = str(response.url)
        if url.rstrip("/") == self._target_url:
            return
        if "json" not in (response.mime_type or "").lower():
            return
        with self._lock:
            if len(self._collected) + len(self._pending) >= self.MAX_RESPONSES:
                return
            self._pending[str(request_id)] = {
                "url": url,
                "status_code": int(response.status),
                "headers": self._allowlisted_headers(response.headers),
                "request_id": request_id,
            }

    def _on_finished(self, event: cdp.network.LoadingFinished) -> None:
        # Do not call get_response_body here — CDP deadlocks (Phase 0 spike).
        rid = str(event.request_id)
        with self._lock:
            if rid in self._pending and rid not in self._ready_ids:
                self._ready_ids.append(rid)

    def harvest(self, tab: Any) -> list[dict[str, Any]]:
        """Fetch bodies for finished responses on the caller thread."""
        with self._lock:
            ready = list(self._ready_ids)
            self._ready_ids.clear()
            jobs = [
                (rid, self._pending.pop(rid)) for rid in ready if rid in self._pending
            ]
            aggregate_bytes = sum(
                len(entry["body"].encode("utf-8")) for entry in self._collected
            )

        for rid, meta in jobs:
            request_id = meta.pop("request_id")
            body = self._fetch_body(tab, request_id, rid)
            if body is None:
                continue
            body_bytes = len(body.encode("utf-8"))
            entry = {
                "url": meta["url"],
                "status_code": meta["status_code"],
                "headers": meta["headers"],
                "body": body,
            }
            with self._lock:
                if len(self._collected) >= self.MAX_RESPONSES:
                    break
                if aggregate_bytes + body_bytes > self.MAX_AGGREGATE_BYTES:
                    break
                self._collected.append(entry)
                aggregate_bytes += body_bytes

        return self.results()

    def _fetch_body(self, tab: Any, request_id: Any, rid: str) -> str | None:
        try:
            body, b64 = tab.send(cdp.network.get_response_body(request_id))
        except Exception as exc:
            logger.debug(
                "xhr_get_response_body_failed request_id=%s error=%s", rid, exc
            )
            return None

        if b64:
            try:
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            except Exception as exc:
                logger.debug(
                    "xhr_body_base64_decode_failed request_id=%s error=%s", rid, exc
                )
                return None

        if not isinstance(body, str) or not body:
            return None
        if len(body.encode("utf-8")) > self.MAX_BODY_BYTES:
            return None
        return body

    def results(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._collected)
