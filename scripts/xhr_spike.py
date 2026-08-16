#!/usr/bin/env python3
"""Phase 0 spike variant: defer get_response_body off the CDP handler thread."""
from __future__ import annotations

import base64
import sys
import threading
import traceback

from botasaurus.browser import Driver
from botasaurus_driver import cdp
from botasaurus_driver.core.custom_storage_cdp import enable_network

TARGET = "https://example.com/"
JSON_URL = "https://jsonplaceholder.typicode.com/todos/1"

pending: dict[str, dict] = {}
finished_ids: list[str] = []
collected: list[dict] = []
lock = threading.Lock()


def on_response(request_id, response, _event) -> None:
    url = str(response.url)
    mime = (response.mime_type or "").lower()
    if url.startswith("chrome:") or url.startswith("chrome-"):
        return
    print(f"[ResponseReceived] id={request_id} status={response.status} mime={mime!r} url={url}")
    if url.rstrip("/") == TARGET.rstrip("/"):
        return
    if "json" not in mime:
        return
    with lock:
        pending[str(request_id)] = {
            "url": url,
            "status": int(response.status),
            "mime": mime,
            "request_id": request_id,
        }
        print(f"[PENDING] id={request_id}")


def on_finished(event: cdp.network.LoadingFinished) -> None:
    rid = str(event.request_id)
    with lock:
        if rid not in pending:
            return
        finished_ids.append(rid)
    print(f"[LoadingFinished] id={rid} (deferred body fetch)")


def fetch_bodies(tab) -> None:
    with lock:
        ids = list(finished_ids)
        metas = {rid: pending[rid] for rid in ids if rid in pending}
    for rid, meta in metas.items():
        print(f"[FETCH] id={rid} url={meta['url']}")
        try:
            body, b64 = tab.send(cdp.network.get_response_body(meta["request_id"]))
            if b64:
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            print(f"[BODY] bytes={len(body)} preview={body[:240]!r}")
            with lock:
                collected.append({**meta, "body": body})
                pending.pop(rid, None)
        except Exception as exc:
            print(f"[BODY ERROR] id={rid} error={exc}")
            traceback.print_exc()


def main() -> int:
    driver = Driver(
        headless=True,
        enable_xvfb_virtual_display=False,
        block_images=True,
        wait_for_complete_page_load=True,
        remove_default_browser_check_argument=True,
    )
    try:
        tab = driver._tab
        tab.send(enable_network())
        tab.after_response_received(on_response)
        tab.add_handler(cdp.network.LoadingFinished, on_finished)

        print(f"navigating to {TARGET}")
        driver.get(TARGET, timeout=30)
        driver.sleep(0.5)

        print(f"fetching {JSON_URL}")
        js_result = driver.run_js(
            f"""
            return fetch({JSON_URL!r})
              .then(r => r.text())
              .then(t => t.slice(0, 120))
              .catch(e => String(e));
            """
        )
        print(f"run_js result={js_result!r}")
        driver.sleep(1)

        fetch_bodies(tab)

        print(f"collected={len(collected)}")
        for item in collected:
            print(f"SUCCESS body={item['body'][:300]}")

        ok = any(item["body"].lstrip().startswith(("{", "[")) for item in collected)
        if not ok:
            print("FAIL: no decoded JSON sub-resource body captured")
            return 1
        print("SPIKE_OK deferred_main_thread_fetch")
        return 0
    finally:
        try:
            driver.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
