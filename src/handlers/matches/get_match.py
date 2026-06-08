"""Flot — GET /matches/{matchId} handler."""
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
    match_id = path_params.get("matchId")
    if not match_id:
        raise AppError(400, "Missing matchId")

    match = get_item(f"MATCH#{match_id}", "META")
    if not match:
        raise AppError(404, "Match not found")
    if user_id not in (match.get("userId1"), match.get("userId2")):
        raise AppError(403, "Forbidden")

    trip1 = get_item(f"TRIP#{match['tripId1']}", "META") or {}
    trip2 = get_item(f"TRIP#{match['tripId2']}", "META") or {}

    return success(_to_response(match, trip1, trip2), origin)


def _trip_summary(trip: dict) -> dict:
    return {
        "tripId": trip.get("tripId"),
        "terminal": trip.get("terminal"),
        "direction": trip.get("direction"),
        "destination": trip.get("destination"),
        "destLat": float(trip["destLat"]) if trip.get("destLat") is not None else None,
        "destLng": float(trip["destLng"]) if trip.get("destLng") is not None else None,
        "destZone": trip.get("destZone"),
        "flightNumber": trip.get("flightNumber"),
        "flightDate": trip.get("flightDate"),
        "flightTime": trip.get("flightTime"),
        "luggage": int(trip["luggage"]) if trip.get("luggage") is not None else None,
        "paxCount": int(trip["paxCount"]) if trip.get("paxCount") is not None else None,
        "mode": trip.get("mode"),
    }


def _to_response(match: dict, trip1: dict, trip2: dict) -> dict:
    response = {
        "matchId": match.get("matchId"),
        "status": match.get("status"),
        "airportCode": match.get("airportCode"),
        "score": match.get("score"),
        "userId1": match.get("userId1"),
        "userId2": match.get("userId2"),
        "unlockedBy": match.get("unlockedBy", []),
        "unlockDeadline": match.get("unlockDeadline"),
        "createdAt": match.get("createdAt"),
        "trip1": _trip_summary(trip1),
        "trip2": _trip_summary(trip2),
    }
    if match.get("pickupPoint"):
        response["pickupPoint"] = match["pickupPoint"]
    return response
