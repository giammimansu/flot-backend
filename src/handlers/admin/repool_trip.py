"""Flot Admin — POST /admin/trips/{tripId}/repool

Manually returns a trip to the scheduled pool.
Used when a trip is stuck in matched/expired and needs a fresh match attempt.

Auth: IAM.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib.dynamo import get_trip, table, now_iso
from lib.http import AppError, app_handler, success
from lib.state_machine import TripStateMachine

logger = Logger()
tracer = Tracer()

# States from which an admin repool is permitted.
_REPOOLABLE = frozenset({"matched", "partially_unlocked_wait", "expired", "tentative_match"})


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=False)
def handler(event: dict, context) -> dict:
    origin = event.get("_origin")
    path_params = event.get("pathParameters") or {}
    trip_id = path_params.get("tripId")
    if not trip_id:
        raise AppError(400, "Missing tripId")

    trip = get_trip(trip_id)
    if not trip:
        raise AppError(404, "Trip not found")

    current_status = trip.get("status", "")
    if current_status not in _REPOOLABLE:
        raise AppError(400, f"Cannot repool trip in status '{current_status}'. Repoolable: {sorted(_REPOOLABLE)}")

    airport_code = trip.get("airportCode", "")
    table.update_item(
        Key={"pk": trip["pk"], "sk": "META"},
        UpdateExpression=(
            "SET #status = :s, "
            "gsi5pk = :gsi5, "
            "updatedAt = :ua "
            "REMOVE matchId, tentativeMatchId"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":s": "scheduled",
            ":gsi5": f"{airport_code}#scheduled",
            ":ua": now_iso(),
        },
    )

    logger.info("admin_repool_trip", tripId=trip_id, previousStatus=current_status)
    return success({"tripId": trip_id, "previousStatus": current_status, "newStatus": "scheduled"}, origin)
