"""Flot Admin — GET /admin/matches/{matchId}/inspect

Returns the full unfiltered match state plus both trips.
Used by ops to diagnose stuck matches without querying DynamoDB directly.

Auth: IAM.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib.dynamo import get_match, get_trip
from lib.http import AppError, app_handler, success

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=False)
def handler(event: dict, context) -> dict:
    origin = event.get("_origin")
    path_params = event.get("pathParameters") or {}
    match_id = path_params.get("matchId")
    if not match_id:
        raise AppError(400, "Missing matchId")

    match = get_match(match_id)
    if not match:
        raise AppError(404, "Match not found")

    trip1 = get_trip(match.get("tripId1", "")) if match.get("tripId1") else None
    trip2 = get_trip(match.get("tripId2", "")) if match.get("tripId2") else None

    return success({
        "match": match,
        "trip1": trip1,
        "trip2": trip2,
    }, origin)
