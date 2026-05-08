"""Flot — GET /users/{userId} handler.

Returns public profile of another user. Caller must share an active match with them.
"""
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
    caller_id: str = event["_user_id"]
    origin: str | None = event["_origin"]

    path_params = event.get("pathParameters") or {}
    target_id = path_params.get("userId")
    if not target_id:
        raise AppError(400, "Missing userId")
    if target_id == caller_id:
        raise AppError(400, "Use /users/me for your own profile")

    # Verify caller shares a match with target (via caller's matched trip)
    caller_trip = _find_matched_trip_with(caller_id, target_id)
    if not caller_trip:
        raise AppError(403, "Forbidden")

    item = get_item(f"USER#{target_id}", "PROFILE")
    if not item:
        raise AppError(404, "User not found")

    match = get_item(f"MATCH#{caller_trip['matchId']}", "META") or {}
    unlocked = caller_id in match.get("unlockedBy", [])

    return success(_public_profile(item, unlocked), origin)


def _find_matched_trip_with(caller_id: str, target_id: str) -> dict | None:
    """Return caller's matched trip that belongs to a match shared with target_id."""
    from lib import dynamo
    trips = dynamo.query_gsi(
        index_name="GSI2-UserTrips",
        pk_name="gsi2pk",
        pk_value=f"USER#{caller_id}",
    )
    for trip in trips:
        if trip.get("status") != "matched" or not trip.get("matchId"):
            continue
        match = get_item(f"MATCH#{trip['matchId']}", "META")
        if not match:
            continue
        if target_id in (match.get("userId1"), match.get("userId2")):
            return trip
    return None


def _public_profile(item: dict, unlocked: bool) -> dict:
    name: str = item.get("name") or ""
    parts = name.split()
    first_name = parts[0] if parts else ""
    last_name = parts[-1] if len(parts) > 1 else ""

    if unlocked:
        return {
            "userId": item.get("userId"),
            "firstName": first_name,
            "lastName": last_name,
            "photoUrl": item.get("photoUrl"),
            "verified": item.get("verified", False),
            "age": item.get("age"),
            "city": item.get("city"),
            "languages": item.get("languages"),
        }
    return {
        "userId": item.get("userId"),
        "firstName": first_name,
        "verified": item.get("verified", False),
        "blurredPhotoUrl": item.get("blurredPhotoUrl"),
    }
