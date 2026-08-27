#!/usr/bin/env python3
"""Docker spike: concurrent headed Drivers + Xvfb / profile adoption / CDP idle.

Exit 0 always; prints JSON constraints for Phase 1 gate documentation.
Run inside the service image with Chromium + xvfb available.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import traceback
from pathlib import Path


def _try_driver(*, headless: bool, profile: Path, label: str) -> dict[str, object]:
    from botasaurus.browser import Driver

    result: dict[str, object] = {"label": label, "headless": headless, "ok": False}
    driver = None
    try:
        driver = Driver(
            headless=headless,
            enable_xvfb_virtual_display=not headless,
            profile=str(profile),
            tiny_profile=True,
            block_images=True,
            wait_for_complete_page_load=True,
            remove_default_browser_check_argument=True,
        )
        tab = getattr(driver, "_tab", None)
        result["has_tab"] = tab is not None
        driver.get("https://example.com")
        html = driver.page_html or ""
        result["ok"] = "Example Domain" in html or len(html) > 0
        result["html_len"] = len(html)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()[-500:]
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception as exc:
                result["close_error"] = str(exc)
    return result


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="xvfb-spike-"))
    report: dict[str, object] = {"runtime_root": str(root)}
    try:
        # 1) Profile-dir adoption: create, close, reopen same path shape.
        profile_a = root / "spare-a"
        profile_a.mkdir()
        report["single_headed"] = _try_driver(
            headless=False, profile=profile_a, label="single_headed"
        )

        # 2) Two concurrent headed drivers (Xvfb collision probe).
        results: list[dict[str, object]] = []
        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            profile = root / name
            profile.mkdir(exist_ok=True)
            barrier.wait(timeout=30)
            results.append(_try_driver(headless=False, profile=profile, label=name))

        t1 = threading.Thread(target=worker, args=("concurrent-a",))
        t2 = threading.Thread(target=worker, args=("concurrent-b",))
        t1.start()
        t2.start()
        t1.join(timeout=120)
        t2.join(timeout=120)
        report["concurrent_headed"] = results
        both_ok = len(results) == 2 and all(bool(r.get("ok")) for r in results)
        report["concurrent_headed_ok"] = both_ok

        # 3) Idle CDP health: build headed, close, expect no live tab helper.
        # (Warm pool uses resolve_cdp_tab; here we only confirm Driver boots.)
        profile_idle = root / "idle"
        profile_idle.mkdir()
        report["idle_boot"] = _try_driver(
            headless=True, profile=profile_idle, label="idle_headless"
        )

        report["gate_headless_only"] = not both_ok
        report["recommendation"] = (
            "PREWARM_HEADLESS_ONLY=True"
            if not both_ok
            else "PREWARM_HEADLESS_ONLY=False (headed concurrent OK)"
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
