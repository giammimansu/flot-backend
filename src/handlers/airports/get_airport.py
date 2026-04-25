"""Flot — GET /airports/{code} handler.

Returns a single airport configuration by IATA code.
Public endpoint — no auth required.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib.airports import airport_to_dict, get_airport
from lib.http import AppError, app_handler, success

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=False)
def handler(event: dict, context) -> dict:
    """GET /airports/{code} — Get single airport config."""
    origin: str | None = event["_origin"]

    code = (event.get("pathParameters") or {}).get("code", "").upper()

    if not code:
        raise AppError(400, "Missing airport code")

    try:
        airport = get_airport(code)
    except ValueError:
        raise AppError(404, f"Airport {code} not found or not active")

    return success(airport_to_dict(airport), origin)
