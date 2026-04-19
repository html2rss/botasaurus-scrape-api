import asyncio
import ipaddress
import logging
import os
import shutil
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from botasaurus.browser import Driver
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, field_validator

DEFAULT_SCRAPE_TIMEOUT_SECONDS = int(os.getenv("SCRAPE_TIMEOUT_SECONDS", "60"))
_MAX_WORKERS = int(os.getenv("SCRAPE_MAX_WORKERS", "4"))
_RUNTIME_ROOT = Path("/tmp/scrape")

NavigationMode = Literal["auto", "get", "google_get", "google_get_bypass"]
ErrorCategory = Literal[
    "timeout", "challenge_block", "navigation_error", "metadata_error"
]

app = FastAPI(title="Botasaurus Scrape API", version="1.1.0")
_executor = ThreadPoolExecutor(max_workers=max(1, _MAX_WORKERS))
_active_request_ids: set[str] = set()
_active_request_ids_lock = threading.Lock()

logger = logging.getLogger("botasaurus_scrape_api")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

_CHALLENGE_MARKERS = (
    "challenge-error-text",
    "Enable JavaScript and cookies to continue",
    "Just a moment...",
    "cf-challenge",
    "cf-turnstile",
    "captcha-delivery.com",
    "datadome",
    "DataDome CAPTCHA",
    "/captcha/?",
)

_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class ScrapeRequest(BaseModel):
    url: HttpUrl
    navigation_mode: NavigationMode = "auto"
    max_retries: int = Field(default=2, ge=0, le=3)
    wait_for_selector: Optional[str] = None
    wait_timeout_seconds: int = Field(
        default=DEFAULT_SCRAPE_TIMEOUT_SECONDS,
        ge=1,
        le=DEFAULT_SCRAPE_TIMEOUT_SECONDS,
    )
    block_images: bool = False
    block_images_and_css: bool = False
    wait_for_complete_page_load: bool = True
    user_agent: Optional[str] = None
    window_size: Optional[list[int]] = None
    lang: Optional[str] = None
    headless: bool = False
    proxy: Optional[str] = None

    @field_validator("window_size")
    @classmethod
    def validate_window_size(cls, value: Optional[list[int]]) -> Optional[list[int]]:
        if value is None:
            return value
        if len(value) != 2:
            raise ValueError("window_size must have exactly 2 integers")
        return value


class ScrapeResponse(BaseModel):
    url: str
    final_url: Optional[str]
    status_code: Optional[int]
    headers: Optional[dict[str, str]]
    html: str
    error: Optional[str]
    metadata_error: Optional[str] = None
    request_id: str
    attempts: int
    strategy_used: Optional[str]
    render_ms: int
    blocked_detected: bool
    challenge_detected: bool
    error_category: Optional[ErrorCategory] = None


def _error_payload(
    url: str,
    message: str,
    *,
    request_id: str,
    attempts: int = 0,
    strategy_used: Optional[str] = None,
    render_ms: int = 0,
    error_category: Optional[ErrorCategory] = None,
) -> dict[str, Any]:
    return {
        "url": url,
        "final_url": None,
        "status_code": None,
        "headers": None,
        "html": "",
        "error": message,
        "metadata_error": None,
        "request_id": request_id,
        "attempts": attempts,
        "strategy_used": strategy_used,
        "render_ms": render_ms,
        "blocked_detected": False,
        "challenge_detected": False,
        "error_category": error_category,
    }


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_WELL_KNOWN_PREFIX:
        # Public NAT64-translated destinations should not be blocked as reserved.
        return False

    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_target_url(raw_url: str) -> tuple[bool, int, Optional[str]]:
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        return False, 400, "Only http/https URLs are allowed"

    host = parsed.hostname
    if not host:
        return False, 400, "URL must include a hostname"

    normalized_host = host.strip().lower().rstrip(".")
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        return False, 403, "Target hostname is blocked"

    try:
        addr_infos = socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, 400, "Hostname could not be resolved"

    resolved_ips: set[str] = set()
    for info in addr_infos:
        sockaddr = info[4]
        if sockaddr:
            resolved_ips.add(sockaddr[0])

    for ip_text in resolved_ips:
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            return False, 403, f"Target resolved to blocked IP address: {ip_text}"

    return True, 200, None


def _strategies_for_request(mode: NavigationMode, max_retries: int) -> list[str]:
    max_attempts = 1 + max_retries
    if mode == "auto":
        ordered = ["google_get", "google_get_bypass", "get"]
        return ordered[: min(len(ordered), max_attempts)]

    return [mode] * max_attempts


def _navigate(
    driver: Driver, target_url: str, strategy: str, timeout_seconds: int
) -> None:
    if strategy == "google_get_bypass":
        try:
            driver.google_get(
                target_url, bypass_cloudflare=True, timeout=timeout_seconds
            )
            return
        except TypeError:
            driver.google_get(target_url, bypass_cloudflare=True)
            return

    if strategy == "google_get":
        try:
            driver.google_get(target_url, timeout=timeout_seconds)
            return
        except TypeError:
            driver.google_get(target_url)
            return

    try:
        driver.get(target_url, timeout=timeout_seconds)
    except TypeError:
        driver.get(target_url)


def _wait_for_readiness(
    driver: Driver,
    *,
    selector: Optional[str],
    timeout_seconds: int,
) -> None:
    if selector:
        driver.wait_for_element(selector, wait=timeout_seconds)
        return

    # Small stability delay to reduce half-rendered captures.
    driver.sleep(1)


def _detect_block_challenge(html: str, status_code: Optional[int]) -> tuple[bool, bool]:
    lower_html = html.lower()
    challenge_detected = any(
        marker.lower() in lower_html for marker in _CHALLENGE_MARKERS
    )
    blocked_detected = challenge_detected or status_code in {401, 403, 429}
    return blocked_detected, challenge_detected


def _fetch_metadata(
    driver: Driver, target_url: str
) -> tuple[Optional[int], Optional[dict[str, str]], str, Optional[str]]:
    status_code: Optional[int] = None
    headers: Optional[dict[str, str]] = None
    final_url = getattr(driver, "current_url", None) or target_url

    try:
        request_client = getattr(driver, "requests", None)
        if request_client is None or not hasattr(request_client, "get"):
            raise RuntimeError("driver.requests.get is unavailable")

        response = request_client.get(target_url)
        status_code = getattr(response, "status_code", None)
        response_headers = getattr(response, "headers", None)

        if response_headers:
            headers = {str(k): str(v) for k, v in dict(response_headers).items()}

        response_url = getattr(response, "url", None)
        if response_url:
            final_url = str(response_url)

        return status_code, headers, final_url, None
    except Exception as exc:  # best-effort metadata only
        return status_code, headers, str(final_url), str(exc)


def _register_request_id(request_id: str) -> None:
    with _active_request_ids_lock:
        if request_id in _active_request_ids:
            raise RuntimeError("request id collision detected")
        _active_request_ids.add(request_id)


def _unregister_request_id(request_id: str) -> None:
    with _active_request_ids_lock:
        _active_request_ids.discard(request_id)


def _run_scrape(payload: ScrapeRequest) -> dict[str, Any]:
    target_url = str(payload.url)
    request_id = str(uuid.uuid4())
    started_monotonic = time.monotonic()
    runtime_dir = _RUNTIME_ROOT / request_id
    profile_dir = runtime_dir / "profile"
    driver: Optional[Driver] = None

    _register_request_id(request_id)
    runtime_dir.mkdir(parents=True, exist_ok=False)
    profile_dir.mkdir(parents=True, exist_ok=False)

    strategies = _strategies_for_request(payload.navigation_mode, payload.max_retries)
    attempts = 0

    try:
        driver = Driver(
            headless=payload.headless,
            enable_xvfb_virtual_display=not payload.headless,
            proxy=payload.proxy,
            profile=str(profile_dir),
            tiny_profile=True,
            block_images=payload.block_images,
            block_images_and_css=payload.block_images_and_css,
            wait_for_complete_page_load=payload.wait_for_complete_page_load,
            user_agent=payload.user_agent,
            window_size=payload.window_size,
            lang=payload.lang,
            remove_default_browser_check_argument=True,
        )

        for attempt_index, strategy in enumerate(strategies, start=1):
            attempts = attempt_index
            try:
                _navigate(driver, target_url, strategy, DEFAULT_SCRAPE_TIMEOUT_SECONDS)
                _wait_for_readiness(
                    driver,
                    selector=payload.wait_for_selector,
                    timeout_seconds=payload.wait_timeout_seconds,
                )

                html = driver.page_html or ""
                status_code, headers, final_url, metadata_error = _fetch_metadata(
                    driver, target_url
                )
                blocked_detected, challenge_detected = _detect_block_challenge(
                    html, status_code
                )
                render_ms = int((time.monotonic() - started_monotonic) * 1000)

                logger.info(
                    "scrape_attempt request_id=%s host=%s mode=%s strategy=%s attempt=%d blocked=%s challenge=%s",
                    request_id,
                    urlparse(target_url).hostname,
                    payload.navigation_mode,
                    strategy,
                    attempt_index,
                    blocked_detected,
                    challenge_detected,
                )

                if (
                    payload.navigation_mode == "auto"
                    and blocked_detected
                    and attempt_index < len(strategies)
                ):
                    continue

                metadata_error_category: Optional[ErrorCategory] = None
                if metadata_error is not None:
                    metadata_error_category = "metadata_error"

                result = {
                    "url": target_url,
                    "final_url": final_url,
                    "status_code": status_code,
                    "headers": headers,
                    "html": html,
                    "error": None,
                    "metadata_error": metadata_error,
                    "request_id": request_id,
                    "attempts": attempt_index,
                    "strategy_used": strategy,
                    "render_ms": render_ms,
                    "blocked_detected": blocked_detected,
                    "challenge_detected": challenge_detected,
                    "error_category": metadata_error_category,
                }

                if blocked_detected:
                    result["error"] = "Challenge block detected"
                    result["error_category"] = "challenge_block"

                return result
            except Exception as exc:
                logger.warning(
                    "scrape_attempt_failed request_id=%s host=%s mode=%s strategy=%s attempt=%d error=%s",
                    request_id,
                    urlparse(target_url).hostname,
                    payload.navigation_mode,
                    strategy,
                    attempt_index,
                    str(exc),
                )

                if attempt_index == len(strategies):
                    render_ms = int((time.monotonic() - started_monotonic) * 1000)
                    return _error_payload(
                        target_url,
                        str(exc),
                        request_id=request_id,
                        attempts=attempt_index,
                        strategy_used=strategy,
                        render_ms=render_ms,
                        error_category="navigation_error",
                    )

        render_ms = int((time.monotonic() - started_monotonic) * 1000)
        return _error_payload(
            target_url,
            "Scrape failed for unknown reason",
            request_id=request_id,
            attempts=attempts,
            strategy_used=strategies[-1] if strategies else None,
            render_ms=render_ms,
            error_category="navigation_error",
        )
    finally:
        try:
            if driver is not None:
                driver.close()
        finally:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            _unregister_request_id(request_id)


def _validation_error_payload(url: str, message: str) -> dict[str, Any]:
    return _error_payload(
        url,
        message,
        request_id=str(uuid.uuid4()),
        attempts=0,
        strategy_used=None,
        render_ms=0,
        error_category="navigation_error",
    )


@app.get("/health")
def health() -> dict[str, str]:
    try:
        botasaurus_version = version("botasaurus")
    except PackageNotFoundError:
        botasaurus_version = "unknown"

    return {
        "status": "ok",
        "service": "botasaurus-scrape-api",
        "botasaurus_version": botasaurus_version,
    }


@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(payload: ScrapeRequest) -> JSONResponse:
    target_url = str(payload.url)
    is_allowed, validation_status, validation_error = _validate_target_url(target_url)
    if not is_allowed:
        return JSONResponse(
            status_code=validation_status,
            content=_validation_error_payload(
                target_url, validation_error or "Target URL is blocked"
            ),
        )

    started_monotonic = time.monotonic()

    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(_executor, _run_scrape, payload),
            timeout=DEFAULT_SCRAPE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        render_ms = int((time.monotonic() - started_monotonic) * 1000)
        timeout_result = _error_payload(
            target_url,
            f"Scrape timed out after {DEFAULT_SCRAPE_TIMEOUT_SECONDS} seconds",
            request_id=str(uuid.uuid4()),
            attempts=0,
            strategy_used=None,
            render_ms=render_ms,
            error_category="timeout",
        )
        logger.warning(
            "scrape_timeout host=%s mode=%s timeout_seconds=%d",
            urlparse(target_url).hostname,
            payload.navigation_mode,
            DEFAULT_SCRAPE_TIMEOUT_SECONDS,
        )
        return JSONResponse(status_code=504, content=timeout_result)

    status_code = 200 if not result.get("error") else 502
    logger.info(
        "scrape_complete request_id=%s host=%s mode=%s attempts=%s status=%d error_category=%s",
        result.get("request_id"),
        urlparse(target_url).hostname,
        payload.navigation_mode,
        result.get("attempts"),
        status_code,
        result.get("error_category"),
    )
    return JSONResponse(status_code=status_code, content=result)


@app.on_event("shutdown")
def shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)
