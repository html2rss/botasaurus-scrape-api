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
from botasaurus.request import Request
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, field_validator

DEFAULT_SCRAPE_TIMEOUT_SECONDS = int(os.getenv("SCRAPE_TIMEOUT_SECONDS", "20"))
DEFAULT_WAIT_TIMEOUT_SECONDS = min(15, DEFAULT_SCRAPE_TIMEOUT_SECONDS)
_MAX_WORKERS = int(os.getenv("SCRAPE_MAX_WORKERS", "4"))
_RUNTIME_ROOT = Path("/tmp/scrape")

ExecutionMode = Literal["auto", "request", "browser"]
NavigationMode = Literal["auto", "get", "google_get", "google_get_bypass"]
ErrorCategory = Literal[
    "timeout", "challenge_block", "navigation_error", "metadata_error"
]

app = FastAPI(title="Botasaurus Scrape API", version="1.2.0")
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

_TRACKER_URL_PATTERNS = [
    "*google-analytics.com*",
    "*googletagmanager.com*",
    "*facebook.net*",
    "*doubleclick.net*",
    "*sentry.io*",
    "*hotjar.com*",
    "*clarity.ms*",
    "*datadoghq-browser-agent.com*",
    "*segment.io*",
    "*analytics.js*",
    "*.woff",
    "*.woff2",
    "*.ttf",
]

_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class ScrapeRequest(BaseModel):
    url: HttpUrl
    execution_mode: ExecutionMode = "auto"
    navigation_mode: NavigationMode = "auto"
    max_retries: int = Field(default=2, ge=0, le=3)
    wait_for_selector: Optional[str] = None
    wait_timeout_seconds: int = Field(
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        ge=1,
        le=DEFAULT_SCRAPE_TIMEOUT_SECONDS,
    )
    block_images: bool = True
    block_images_and_css: bool = False
    block_trackers: bool = True
    wait_for_complete_page_load: bool = True
    user_agent: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    cookies: Optional[dict[str, str]] = None
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
    execution_tier: Optional[str] = None
    detected_challenge: Optional[str] = None


def _error_payload(
    url: str,
    message: str,
    *,
    request_id: str,
    attempts: int = 0,
    strategy_used: Optional[str] = None,
    render_ms: int = 0,
    error_category: Optional[ErrorCategory] = None,
    execution_tier: Optional[str] = None,
    detected_challenge: Optional[str] = None,
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
        "execution_tier": execution_tier,
        "detected_challenge": detected_challenge,
    }


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_WELL_KNOWN_PREFIX:
        # Validate the embedded IPv4 address for NAT64 translated destinations
        try:
            embedded_ipv4 = ipaddress.IPv4Address(ip.packed[-4:])
            return _is_blocked_ip(embedded_ipv4)
        except ValueError:
            return True

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


def _detect_block_challenge(
    html: str, status_code: Optional[int]
) -> tuple[bool, bool, Optional[str]]:
    lower_html = html.lower()
    matched_marker = None
    for marker in _CHALLENGE_MARKERS:
        if marker.lower() in lower_html:
            matched_marker = marker
            break

    challenge_detected = matched_marker is not None
    blocked_detected = challenge_detected or status_code in {401, 403, 429}
    return blocked_detected, challenge_detected, matched_marker


def _extract_passive_metadata(
    driver: Driver, target_url: str
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

    get_log = getattr(driver, "get_log", None)
    if callable(get_log):
        try:
            import json

            logs = get_log("performance")
            if isinstance(logs, list):
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
        except Exception:
            pass

    return None, None, None


def _fetch_metadata(
    driver: Driver, target_url: str
) -> tuple[Optional[int], Optional[dict[str, str]], str, Optional[str]]:
    final_url = getattr(driver, "current_url", None) or target_url

    # Rely strictly on passive CDP / network interception to eliminate duplicate fetch requests
    try:
        status_code, headers, passive_url = _extract_passive_metadata(
            driver, target_url
        )
        if status_code is not None:
            return status_code, headers, passive_url or str(final_url), None
    except Exception:
        pass

    # Default to 200 if page rendered HTML and no challenge detected
    return 200, None, str(final_url), None


def _register_request_id(request_id: str) -> None:
    with _active_request_ids_lock:
        if request_id in _active_request_ids:
            raise RuntimeError("request id collision detected")
        _active_request_ids.add(request_id)


def _unregister_request_id(request_id: str) -> None:
    with _active_request_ids_lock:
        _active_request_ids.discard(request_id)


def _run_request_scrape(
    payload: ScrapeRequest,
    request_id: str,
    started_monotonic: float,
) -> Optional[dict[str, Any]]:
    target_url = str(payload.url)
    remaining_budget = max(
        1,
        int(
            DEFAULT_SCRAPE_TIMEOUT_SECONDS
            - (time.monotonic() - started_monotonic)
        ),
    )

    req_headers = dict(payload.headers) if payload.headers else {}
    user_agent = (
        payload.user_agent
        or req_headers.get("User-Agent")
        or req_headers.get("user-agent")
    )
    proxies = (
        {"http": payload.proxy, "https": payload.proxy}
        if payload.proxy
        else None
    )

    req = Request()
    try:
        resp = req.get(
            target_url,
            headers=req_headers if req_headers else None,
            cookies=payload.cookies,
            user_agent=user_agent,
            proxies=proxies,
            timeout=remaining_budget,
            browser="chrome",
            allow_redirects=True,
        )

        html = resp.text or ""
        status_code = int(resp.status_code) if resp.status_code is not None else 200
        headers_dict = (
            {str(k): str(v) for k, v in resp.headers.items()}
            if getattr(resp, "headers", None)
            else None
        )
        final_url = str(resp.url) if getattr(resp, "url", None) else target_url

        blocked_detected, challenge_detected, detected_marker = (
            _detect_block_challenge(html, status_code)
        )
        render_ms = int((time.monotonic() - started_monotonic) * 1000)

        # In auto mode, escalate to browser if challenge, block, non-2xx status, or empty body
        is_clean_success = (
            not blocked_detected
            and not challenge_detected
            and (200 <= status_code < 300)
            and len(html.strip()) > 0
            and not payload.wait_for_selector  # Wait selector requires browser DOM
        )

        if payload.execution_mode == "auto" and not is_clean_success:
            logger.info(
                "request_tier_escalating request_id=%s host=%s status=%d blocked=%s challenge=%s",
                request_id,
                urlparse(target_url).hostname,
                status_code,
                blocked_detected,
                challenge_detected,
            )
            return None

        result = {
            "url": target_url,
            "final_url": final_url,
            "status_code": status_code,
            "headers": headers_dict,
            "html": html,
            "error": None,
            "metadata_error": None,
            "request_id": request_id,
            "attempts": 1,
            "strategy_used": "anti_detect_request",
            "render_ms": render_ms,
            "blocked_detected": blocked_detected,
            "challenge_detected": challenge_detected,
            "error_category": None,
            "execution_tier": "http_request",
            "detected_challenge": detected_marker,
        }

        if blocked_detected:
            result["error"] = "Challenge block detected"
            result["error_category"] = "challenge_block"

        return result
    finally:
        try:
            req.close()
        except Exception:
            pass


def _run_browser_scrape(
    payload: ScrapeRequest,
    request_id: str,
    started_monotonic: float,
) -> dict[str, Any]:
    target_url = str(payload.url)
    runtime_dir = _RUNTIME_ROOT / request_id
    profile_dir = runtime_dir / "profile"
    driver: Optional[Driver] = None

    runtime_dir.mkdir(parents=True, exist_ok=False)
    profile_dir.mkdir(parents=True, exist_ok=False)

    strategies = _strategies_for_request(payload.navigation_mode, payload.max_retries)
    attempts = 0

    user_agent = payload.user_agent
    if not user_agent and payload.headers:
        user_agent = payload.headers.get("User-Agent") or payload.headers.get(
            "user-agent"
        )

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
            user_agent=user_agent,
            window_size=payload.window_size,
            lang=payload.lang,
            remove_default_browser_check_argument=True,
        )

        if payload.block_trackers and hasattr(driver, "_tab"):
            try:
                driver._tab.block_urls(_TRACKER_URL_PATTERNS)
            except Exception:
                pass

        if payload.cookies:
            try:
                for c_name, c_val in payload.cookies.items():
                    try:
                        driver.add_cookies([{"name": str(c_name), "value": str(c_val), "url": target_url}])
                    except Exception:
                        pass
            except Exception:
                pass

        if payload.headers and hasattr(driver, "_tab"):
            try:
                driver._tab.set_extra_http_headers(payload.headers)
            except Exception:
                pass

        for attempt_index, strategy in enumerate(strategies, start=1):
            attempts = attempt_index
            try:
                remaining_budget = max(
                    1,
                    int(
                        DEFAULT_SCRAPE_TIMEOUT_SECONDS
                        - (time.monotonic() - started_monotonic)
                    ),
                )
                _navigate(driver, target_url, strategy, remaining_budget)
                _wait_for_readiness(
                    driver,
                    selector=payload.wait_for_selector,
                    timeout_seconds=min(
                        payload.wait_timeout_seconds, remaining_budget
                    ),
                )

                html = driver.page_html or ""
                status_code, headers, final_url, metadata_error = _fetch_metadata(
                    driver, target_url
                )
                blocked_detected, challenge_detected, detected_marker = (
                    _detect_block_challenge(html, status_code)
                )

                if challenge_detected or blocked_detected:
                    logger.warning(
                        "scrape_challenge_detected request_id=%s host=%s strategy=%s attempt=%d marker=%s",
                        request_id,
                        urlparse(target_url).hostname,
                        strategy,
                        attempt_index,
                        detected_marker,
                    )
                    if attempt_index < len(strategies):
                        continue

                    render_ms = int((time.monotonic() - started_monotonic) * 1000)
                    return {
                        "url": target_url,
                        "final_url": final_url,
                        "status_code": status_code,
                        "headers": headers,
                        "html": html,
                        "error": f"Bot challenge detected ({detected_marker or 'unknown'})",
                        "metadata_error": metadata_error,
                        "request_id": request_id,
                        "attempts": attempts,
                        "strategy_used": strategy,
                        "render_ms": render_ms,
                        "blocked_detected": blocked_detected,
                        "challenge_detected": challenge_detected,
                        "error_category": "challenge_block",
                        "execution_tier": "browser_driver",
                        "detected_challenge": detected_marker,
                    }

                render_ms = int((time.monotonic() - started_monotonic) * 1000)
                return {
                    "url": target_url,
                    "final_url": final_url,
                    "status_code": status_code,
                    "headers": headers,
                    "html": html,
                    "error": None,
                    "metadata_error": metadata_error,
                    "request_id": request_id,
                    "attempts": attempts,
                    "strategy_used": strategy,
                    "render_ms": render_ms,
                    "blocked_detected": False,
                    "challenge_detected": False,
                    "error_category": None,
                    "execution_tier": "browser_driver",
                    "detected_challenge": None,
                }
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
                if attempt_index < len(strategies):
                    continue

                render_ms = int((time.monotonic() - started_monotonic) * 1000)
                category = "timeout" if "timeout" in str(exc).lower() else "navigation_error"
                return _error_payload(
                    target_url,
                    str(exc),
                    request_id=request_id,
                    attempts=attempts,
                    strategy_used=strategy,
                    render_ms=render_ms,
                    error_category=category,
                    execution_tier="browser_driver",
                )

        render_ms = int((time.monotonic() - started_monotonic) * 1000)
        return _error_payload(
            target_url,
            "Scrape failed after all strategy attempts",
            request_id=request_id,
            attempts=attempts,
            strategy_used=strategies[-1] if strategies else None,
            render_ms=render_ms,
            error_category="navigation_error",
            execution_tier="browser_driver",
        )
    finally:
        try:
            if driver is not None:
                driver.close()
        finally:
            shutil.rmtree(runtime_dir, ignore_errors=True)


def _run_scrape(
    payload: ScrapeRequest, deadline_monotonic: Optional[float] = None
) -> dict[str, Any]:
    target_url = str(payload.url)
    request_id = str(uuid.uuid4())
    started_monotonic = time.monotonic()

    if deadline_monotonic and started_monotonic >= deadline_monotonic:
        return _error_payload(
            target_url,
            "Scrape timed out in threadpool queue before execution started",
            request_id=request_id,
            attempts=0,
            strategy_used=None,
            render_ms=0,
            error_category="timeout",
        )

    _register_request_id(request_id)
    try:
        should_try_request_tier = (
            payload.execution_mode == "request"
            or (
                payload.execution_mode == "auto"
                and payload.navigation_mode == "auto"
                and not payload.wait_for_selector
            )
        )

        if should_try_request_tier:
            try:
                request_result = _run_request_scrape(
                    payload, request_id, started_monotonic
                )
                if request_result is not None:
                    return request_result
            except Exception as exc:
                logger.info(
                    "request_tier_failed request_id=%s host=%s error=%s",
                    request_id,
                    urlparse(target_url).hostname,
                    str(exc),
                )
                if payload.execution_mode == "request":
                    render_ms = int((time.monotonic() - started_monotonic) * 1000)
                    return _error_payload(
                        target_url,
                        str(exc),
                        request_id=request_id,
                        attempts=1,
                        strategy_used="anti_detect_request",
                        render_ms=render_ms,
                        error_category="navigation_error",
                        execution_tier="http_request",
                    )

        # Execute browser tier (for mode="browser", explicit navigation_mode, or when mode="auto" escalates)
        return _run_browser_scrape(payload, request_id, started_monotonic)
    finally:
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
    is_allowed, validation_status, validation_error = _validate_target_url(
        target_url
    )
    if not is_allowed:
        return JSONResponse(
            status_code=validation_status,
            content=_validation_error_payload(
                target_url, validation_error or "Target URL is blocked"
            ),
        )

    if payload.proxy:
        proxy_url = str(payload.proxy)
        is_allowed_proxy, proxy_status, proxy_error = _validate_target_url(proxy_url)
        if not is_allowed_proxy:
            return JSONResponse(
                status_code=proxy_status,
                content=_validation_error_payload(
                    target_url, f"Proxy URL is invalid or blocked: {proxy_error}"
                ),
            )

    started_monotonic = time.monotonic()
    deadline_monotonic = started_monotonic + DEFAULT_SCRAPE_TIMEOUT_SECONDS

    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(_executor, _run_scrape, payload, deadline_monotonic),
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
        "scrape_complete request_id=%s host=%s mode=%s tier=%s attempts=%s status=%d error_category=%s",
        result.get("request_id"),
        urlparse(target_url).hostname,
        payload.navigation_mode,
        result.get("execution_tier"),
        result.get("attempts"),
        status_code,
        result.get("error_category"),
    )
    return JSONResponse(status_code=status_code, content=result)


@app.on_event("shutdown")
def shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)
