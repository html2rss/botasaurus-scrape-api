"""Health probe routes."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter

from app.api.openapi import HEALTH_RESPONSES
from app.constants import SERVICE_NAME
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    operation_id="get-health",
    summary="Health",
    description="Return liveness status, service name, and the installed Botasaurus version.",
    responses=HEALTH_RESPONSES,
)
def health() -> HealthResponse:
    try:
        botasaurus_version = version("botasaurus")
    except PackageNotFoundError:
        botasaurus_version = "unknown"

    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        botasaurus_version=botasaurus_version,
    )
