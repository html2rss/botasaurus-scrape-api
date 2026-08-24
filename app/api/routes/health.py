"""Health probe routes."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter

from app.api.openapi import get_health_responses
from app.constants import SERVICE_NAME
from app.schemas.response import HealthResponse


def _health() -> HealthResponse:
    try:
        botasaurus_version = version("botasaurus")
    except PackageNotFoundError:
        botasaurus_version = "unknown"

    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        botasaurus_version=botasaurus_version,
    )


def create_router() -> APIRouter:
    """Build the health router after OpenAPI metadata is configured."""
    router = APIRouter(tags=["health"])
    router.add_api_route(
        "/health",
        _health,
        methods=["GET"],
        response_model=HealthResponse,
        operation_id="get-health",
        summary="Health",
        description=(
            "Return liveness status, service name, and the installed Botasaurus version."
        ),
        responses=get_health_responses(),
    )
    return router
