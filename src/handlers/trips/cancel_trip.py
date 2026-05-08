"""Flot — DELETE /trips/{tripId} handler.

Cancels a pre-scheduled trip. Used mainly by users to opt out of a trip they created before it matches or while searching.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer
from lib import dynamo
from lib.http import AppError, app_handler, json_response
from lib.eventbridge import put_event

logger = Logger()
tracer = Tracer()

@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]
    path_parameters = event.get("pathParameters") or {}
    trip_id = path_parameters.get("tripId")
    if not trip_id:
        raise AppError(400, "tripId is required")

    trip = dynamo.get_item(f"TRIP#{trip_id}", "META")
    if not trip:
        raise AppError(404, "Trip not found")

    if trip.get("userId") != user_id:
        raise AppError(403, "Forbidden")

    status = trip.get("status")
    if status not in ["searching", "scheduled"]:
        raise AppError(400, f"Cannot cancel trip in status: {status}")

    trip["status"] = "cancelled"
    
    # We should delete GSI keys to remove it from matchmaking queues
    if "gsi5pk" in trip:
        del trip["gsi5pk"]
    if "gsi5sk" in trip:
        del trip["gsi5sk"]
    if "gsi1pk" in trip:
        del trip["gsi1pk"]
    if "gsi1sk" in trip:
        del trip["gsi1sk"]
    
    dynamo.put_item(trip)

    put_event("trip.cancelled", {"tripId": trip_id, "airportCode": trip.get("airportCode")})

    return json_response(200, {"message": "Trip cancelled", "tripId": trip_id}, origin)
