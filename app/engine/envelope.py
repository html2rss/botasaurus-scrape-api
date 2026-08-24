"""Success and error envelope builders plus HTML normalization."""

from __future__ import annotations

from app.infra.detector import ChallengeAssessment
from app.schemas.enums import ErrorCategory, ExecutionTier, NavigationMode, TimeoutPhase
from app.schemas.response import (
    ChallengeSignal,
    ScrapeDiagnostics,
    ScrapeError,
    ScrapeSuccess,
    XhrResponse,
)

HTML_DOCUMENT_CONTENT_TYPE = "text/html; charset=utf-8"


def utf8_normalize_html(html: str) -> str:
    if not html:
        return html
    try:
        html = html.encode("latin-1").decode("utf-8")
    # fmt: skip keeps parenthesized except (dev venv predates PEP 758 syntax)
    except (UnicodeEncodeError, UnicodeDecodeError):  # fmt: skip
        pass
    return html.encode("utf-8", errors="replace").decode("utf-8")


def html_document_headers(
    html: str, headers: dict[str, str] | None
) -> tuple[str, dict[str, str] | None]:
    if not html:
        return html, headers
    normalized = utf8_normalize_html(html)
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if str(key).lower() == "content-type":
            continue
        out[str(key)] = str(value)
    out["content-type"] = HTML_DOCUMENT_CONTENT_TYPE
    return normalized, out


def build_diagnostics(
    *,
    request_id: str,
    attempts: int = 0,
    strategy_used: NavigationMode | None = None,
    render_ms: int = 0,
    execution_tier: ExecutionTier | None = None,
    assessment: ChallengeAssessment | None = None,
    timeout_phase: TimeoutPhase | None = None,
) -> ScrapeDiagnostics:
    challenge = None
    if assessment is not None:
        challenge = ChallengeSignal(
            blocked=assessment.blocked_detected,
            detected=assessment.challenge_detected,
            marker=assessment.detected_marker,
        )
    return ScrapeDiagnostics(
        request_id=request_id,
        attempts=attempts,
        strategy_used=strategy_used,
        render_ms=render_ms,
        execution_tier=execution_tier,
        challenge=challenge,
        timeout_phase=timeout_phase,
    )


def build_success(
    url: str,
    *,
    request_id: str,
    html: str,
    attempts: int,
    render_ms: int,
    execution_tier: ExecutionTier,
    strategy_used: NavigationMode | None = None,
    final_url: str | None = None,
    status_code: int | None = 200,
    headers: dict[str, str] | None = None,
    metadata_error: str | None = None,
    assessment: ChallengeAssessment | None = None,
    xhr_responses: list[XhrResponse] | None = None,
) -> ScrapeSuccess:
    html, headers = html_document_headers(html, headers)
    return ScrapeSuccess(
        url=url,
        final_url=final_url or url,
        status_code=status_code,
        headers=headers,
        html=html,
        metadata_error=metadata_error,
        xhr_responses=xhr_responses or [],
        diagnostics=build_diagnostics(
            request_id=request_id,
            attempts=attempts,
            strategy_used=strategy_used,
            render_ms=render_ms,
            execution_tier=execution_tier,
            assessment=assessment,
        ),
    )


def build_error(
    url: str,
    message: str,
    *,
    request_id: str,
    error_category: ErrorCategory,
    attempts: int = 0,
    strategy_used: NavigationMode | None = None,
    render_ms: int = 0,
    execution_tier: ExecutionTier | None = None,
    assessment: ChallengeAssessment | None = None,
    timeout_phase: TimeoutPhase | None = None,
) -> ScrapeError:
    return ScrapeError(
        url=url,
        error=message,
        error_category=error_category,
        diagnostics=build_diagnostics(
            request_id=request_id,
            attempts=attempts,
            strategy_used=strategy_used,
            render_ms=render_ms,
            execution_tier=execution_tier,
            assessment=assessment,
            timeout_phase=timeout_phase,
        ),
    )
