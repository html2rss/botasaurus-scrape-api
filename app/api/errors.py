"""HTTP exception handlers returning scrape error envelopes."""

from __future__ import annotations

from typing import TypedDict, cast
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response

from app.infra.request_id import resolve_request_id
from app.logging_config import get_logger
from app.schemas.response import ScrapeError, ScrapeSuccess, validation_error

logger = get_logger()

_NON_FIELD_LOC = {"body", "query", "path", "header"}


class ValidationErrorItem(TypedDict, total=False):
    loc: tuple[int | str, ...] | list[int | str]
    msg: str
    type: str
    input: object


def schema_field_from_loc(loc: tuple[int | str, ...] | list[int | str]) -> str:
    for part in loc:
        if part not in _NON_FIELD_LOC:
            return str(part)
    return str(loc[-1]) if loc else "unknown"


def first_schema_field(errors: list[ValidationErrorItem]) -> str:
    if not errors:
        return "unknown"
    return schema_field_from_loc(errors[0].get("loc", ()))


def validation_error_message(errors: list[ValidationErrorItem]) -> str:
    if not errors:
        return "Request validation failed"
    first = errors[0]
    field = schema_field_from_loc(first.get("loc", ()))
    msg = first.get("msg", "invalid")
    return f"{field}: {msg}"


def url_from_validation_body(body: object) -> str:
    if isinstance(body, dict):
        raw_url = cast(dict[str, object], body).get("url")
        if isinstance(raw_url, str):
            return raw_url
    return ""


def json_response(body: ScrapeSuccess | ScrapeError, *, status_code: int) -> Response:
    return Response(
        content=body.model_dump_json(),
        status_code=status_code,
        media_type="application/json",
    )


async def request_schema_validation_handler(
    request: Request, exc: Exception
) -> Response:
    validation_exc = (
        exc if isinstance(exc, RequestValidationError) else RequestValidationError([])
    )
    errors: list[ValidationErrorItem] = list(validation_exc.errors())
    url = url_from_validation_body(validation_exc.body)
    field = first_schema_field(errors)
    request_id = resolve_request_id(
        request.headers.get("X-Request-Id"),
        host=urlparse(url).hostname if url else None,
    )
    logger.info(
        "request_schema_422 host=%s field=%s",
        urlparse(url).hostname if url else None,
        field,
    )
    return json_response(
        validation_error(
            url,
            validation_error_message(errors),
            request_id=request_id,
        ),
        status_code=422,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    del exc
    logger.exception("unhandled_exception path=%s", request.url.path)
    request_id = resolve_request_id(request.headers.get("X-Request-Id"))
    return json_response(
        validation_error(
            "",
            "Internal server error",
            request_id=request_id,
        ),
        status_code=500,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(
        RequestValidationError,
        request_schema_validation_handler,
    )
    app.add_exception_handler(Exception, unhandled_exception_handler)
