"""Flot — GET /airports handler.

Returns all active airports with zones, terminals, and fares.
Public endpoint — no auth required.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib.airports import airport_to_dict, get_active_airports
from lib.http import app_handler, success

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=False)
def handler(event: dict, context) -> dict:
    """GET /airports — List all active airports."""
    origin: str | None = event["_origin"]

    airports = get_active_airports()
    data = [airport_to_dict(a) for a in airports]

    return success({"airports": data}, origin)
