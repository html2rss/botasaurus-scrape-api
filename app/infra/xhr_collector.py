"""XHR/fetch JSON response capture via CDP network events.

Phase 0 finding: ``Network.getResponseBody`` deadlocks when called from inside a
``LoadingFinished`` handler (CDP session re-entrancy). Handlers only record
metadata / readiness; ``harvest()`` fetches bodies on the caller thread after
navigation/scroll completes.
"""

from __future__ import annotations

import base64
import threading
from typing import cast

from app.engine.driver_capabilities import CdpTabProtocol
from app.infra.cdp_types import PendingXhrMeta
from app.logging_config import get_logger
from app.schemas.response import XhrResponse

logger = get_logger()


class XhrCollector:
    """Collects JSON XHR/fetch response bodies via CDP handlers."""

    MAX_RESPONSES = 20
    MAX_BODY_BYTES = 500_000
    # Mirror html2rss BotasaurusContract::ParsedResponse::MAX_XHR_AGGREGATE_BYTES.
    MAX_AGGREGATE_BYTES = 2_000_000
    _HEADER_ALLOWLIST = frozenset({"content-type"})

    def __init__(self, target_url: str) -> None:
        self._target_url = str(target_url).rstrip("/")
        self._pending: dict[str, PendingXhrMeta] = {}
        self._ready_ids: list[str] = []
        self._collected: list[XhrResponse] = []
        self._aggregate_bytes: int = 0
        self._lock = threading.Lock()

    def install(self, tab: CdpTabProtocol) -> None:
        """Enable the network domain and register handlers before navigation."""
        from botasaurus_driver import cdp
        from botasaurus_driver.core.custom_storage_cdp import enable_network

        tab.send(enable_network())
        tab.after_response_received(self._on_response)
        tab.add_handler(cdp.network.LoadingFinished, self._on_finished)

    def reset(self) -> None:
        """Clear pending/ready/collected state between strategy attempts."""
        with self._lock:
            self._pending.clear()
            self._ready_ids.clear()
            self._collected.clear()
            self._aggregate_bytes = 0

    @classmethod
    def _allowlisted_headers(cls, headers: object) -> dict[str, str]:
        """Keep only content-type; drop Set-Cookie and other headers."""
        allowed: dict[str, str] = {}
        header_map = cast(
            dict[object, object], headers if isinstance(headers, dict) else {}
        )
        for key, value in header_map.items():
            normalized = str(key).lower()
            if normalized in cls._HEADER_ALLOWLIST:
                allowed[normalized] = str(value)
        return allowed

    def _on_response(
        self, request_id: object, response: object, _event: object
    ) -> None:
        url = str(getattr(response, "url", ""))
        if url.rstrip("/") == self._target_url:
            return
        mime_type = str(getattr(response, "mime_type", "") or "")
        if "json" not in mime_type.lower():
            return
        with self._lock:
            if len(self._collected) + len(self._pending) >= self.MAX_RESPONSES:
                return
            stored_request_id = (
                request_id if isinstance(request_id, (str, int)) else str(request_id)
            )
            self._pending[str(request_id)] = PendingXhrMeta(
                url=url,
                status_code=int(getattr(response, "status", 0)),
                headers=self._allowlisted_headers(getattr(response, "headers", {})),
                request_id=stored_request_id,
            )

    def _on_finished(self, event: object) -> None:
        # Do not call get_response_body here — CDP deadlocks (Phase 0 spike).
        rid = str(getattr(event, "request_id", ""))
        with self._lock:
            if rid in self._pending and rid not in self._ready_ids:
                self._ready_ids.append(rid)

    def harvest(self, tab: CdpTabProtocol) -> list[XhrResponse]:
        """Fetch bodies for finished responses on the caller thread."""
        with self._lock:
            ready = list(self._ready_ids)
            self._ready_ids.clear()
            jobs = [
                (rid, self._pending.pop(rid)) for rid in ready if rid in self._pending
            ]

        for rid, meta in jobs:
            body = self._fetch_body(tab, meta["request_id"], rid)
            if body is None:
                continue
            body_bytes = len(body.encode("utf-8"))
            entry = XhrResponse(
                url=meta["url"],
                status_code=meta["status_code"],
                headers=meta["headers"],
                body=body,
            )
            with self._lock:
                if len(self._collected) >= self.MAX_RESPONSES:
                    break
                if self._aggregate_bytes + body_bytes > self.MAX_AGGREGATE_BYTES:
                    break
                self._collected.append(entry)
                self._aggregate_bytes += body_bytes

        return self.results()

    def _fetch_body(
        self, tab: CdpTabProtocol, request_id: str | int, rid: str
    ) -> str | None:
        from botasaurus_driver import cdp

        try:
            body, b64 = tab.send(cdp.network.get_response_body(request_id))
        except Exception as exc:
            logger.debug(
                "xhr_get_response_body_failed request_id=%s error=%s", rid, exc
            )
            return None

        if b64:
            try:
                body = base64.b64decode(cast(str, body)).decode(
                    "utf-8", errors="replace"
                )
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

    def results(self) -> list[XhrResponse]:
        with self._lock:
            return list(self._collected)
