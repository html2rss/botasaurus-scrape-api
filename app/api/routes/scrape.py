"""Scrape endpoint routes."""

from __future__ import annotations

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from app.api.deps import ScrapeServiceDep
from app.api.errors import json_response
from app.api.openapi import get_scrape_error_responses, get_scrape_success_response
from app.schemas.request import ScrapeRequest
from app.schemas.response import ScrapeSuccess


async def _scrape(
    payload: ScrapeRequest,
    service: ScrapeServiceDep,
    x_request_id: str | None = Header(None, alias="X-Request-Id"),
) -> JSONResponse:
    outcome = await service.process(payload, inbound_request_id=x_request_id)
    return json_response(outcome.body, status_code=outcome.status_code)


def create_router() -> APIRouter:
    """Build the scrape router after OpenAPI metadata is configured."""
    router = APIRouter(tags=["scrape"])
    router.add_api_route(
        "/scrape",
        _scrape,
        methods=["POST"],
        response_model=ScrapeSuccess,
        responses={**get_scrape_success_response(), **get_scrape_error_responses()},
        operation_id="scrape-url",
        summary="Scrape a URL",
        description=(
            "Fetch rendered HTML for a public http(s) URL. Invalid or blocked "
            "targets return `ScrapeError`. `wait_timeout_seconds` is clamped, not 422."
        ),
    )
    return router
