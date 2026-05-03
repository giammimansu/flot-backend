"""Flot — GET /trips/{tripId} handler."""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib.dynamo import get_item
from lib.http import AppError, app_handler, success

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]

    path_params = event.get("pathParameters") or {}
    trip_id = path_params.get("tripId")
    if not trip_id:
        raise AppError(400, "Missing tripId")

    item = get_item(f"TRIP#{trip_id}", "META")
    if not item:
        raise AppError(404, "Trip not found")
    if item.get("userId") != user_id:
        raise AppError(403, "Forbidden")

    return success(_to_response(item), origin)


def _to_response(item: dict) -> dict:
    return {
        "tripId": item.get("tripId"),
        "userId": item.get("userId"),
        "airportCode": item.get("airportCode"),
        "terminal": item.get("terminal"),
        "direction": item.get("direction"),
        "destZone": item.get("destZone"),
        "flightTime": item.get("flightTime"),
        "timeBucket": item.get("timeBucket"),
        "luggage": item.get("luggage"),
        "paxCount": item.get("paxCount"),
        "status": item.get("status"),
        "matchId": item.get("matchId"),
        "createdAt": item.get("createdAt"),
    }
