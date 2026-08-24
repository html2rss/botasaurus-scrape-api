"""Scrape endpoint routes."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from app.api.deps import ScrapeServiceDep, resolve_scrape_request_id
from app.api.errors import json_response
from app.api.openapi import get_scrape_error_responses, get_scrape_success_response
from app.schemas.request import ScrapeRequest
from app.schemas.response import ScrapeSuccess

router = APIRouter(tags=["scrape"])


@router.post(
    "/scrape",
    response_model=ScrapeSuccess,
    responses={**get_scrape_success_response(), **get_scrape_error_responses()},
    operation_id="scrape-url",
    summary="Scrape a URL",
    description=(
        "Fetch rendered HTML for a public http(s) URL. Invalid or blocked "
        "targets return `ScrapeError`. `wait_timeout_seconds` is clamped, not 422."
    ),
)
async def scrape(
    payload: ScrapeRequest,
    service: ScrapeServiceDep,
    x_request_id: str | None = Header(None, alias="X-Request-Id"),
) -> JSONResponse:
    target_url = str(payload.url)
    target_host = urlparse(target_url).hostname
    request_id = resolve_scrape_request_id(x_request_id, host=target_host)

    target_validation = service.validate_target(target_url)
    if not target_validation.is_allowed:
        outcome = service.validation_outcome(
            target_url,
            target_validation,
            request_id=request_id,
            default_message="Target URL is blocked",
        )
        return json_response(outcome.body, status_code=outcome.status_code)

    if payload.proxy:
        proxy_validation = service.validate_proxy(str(payload.proxy))
        if not proxy_validation.is_allowed:
            outcome = service.validation_outcome(
                target_url,
                proxy_validation,
                request_id=request_id,
                default_message="Proxy URL is invalid or blocked",
            )
            return json_response(outcome.body, status_code=outcome.status_code)

    outcome = await service.run(payload, request_id=request_id)
    return json_response(outcome.body, status_code=outcome.status_code)
