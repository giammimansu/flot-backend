"""Flot — GET /trips/my handler.

Returns active and recent trips for the current user. Trips that are matched or
unlocked carry a compact `partner` summary so the trips list can render the
co-rider without an extra round-trip per card.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer
from lib import dynamo
from lib.dynamo import get_item
from lib.http import app_handler, success

logger = Logger()
tracer = Tracer()

# Statuses whose card shows a co-rider.
_PARTNER_STATUSES = {"matched", "unlocked"}


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]

    # Query GSI2: userId -> createdAt
    # We retrieve the recent trips of the user
    trips = dynamo.query_gsi(
        index_name="GSI2-UserTrips",
        pk_name="gsi2pk",
        pk_value=f"USER#{user_id}"
    )

    # Sort descending by createdAt
    trips.sort(key=lambda x: x.get("createdAt", ""), reverse=True)

    # Attach a compact partner summary to matched/unlocked trips.
    for trip in trips:
        if trip.get("status") in _PARTNER_STATUSES and trip.get("matchId"):
            partner = _partner_summary(user_id, trip["matchId"])
            if partner:
                trip["partner"] = partner

    return success({"trips": trips}, origin)


def _partner_summary(user_id: str, match_id: str) -> dict | None:
    """Compact co-rider for the trips list. Locked while not yet unlocked."""
    match = get_item(f"MATCH#{match_id}", "META")
    if not match:
        return None

    partner_id = (
        match.get("userId2")
        if match.get("userId1") == user_id
        else match.get("userId1")
    )
    if not partner_id:
        return None

    profile = get_item(f"USER#{partner_id}", "PROFILE")
    if not profile:
        return None

    name: str = profile.get("name") or ""
    parts = name.split()
    first_name = parts[0] if parts else ""
    last_name = parts[-1] if len(parts) > 1 else ""

    unlocked = user_id in match.get("unlockedBy", [])
    if not unlocked:
        # Locked: identity stays masked until the match is unlocked.
        return {
            "userId": partner_id,
            "firstName": first_name,
            "verified": profile.get("verified", False),
            "blurredPhotoUrl": profile.get("blurredPhotoUrl"),
            "unlocked": False,
        }

    from handlers.users.get_user_rating import compute_rating
    return {
        "userId": partner_id,
        "firstName": first_name,
        "lastName": last_name,
        "photoUrl": profile.get("photoUrl"),
        "verified": profile.get("verified", False),
        "rating": compute_rating(profile),
        "tripCount": _count_completed_trips(partner_id),
        "unlocked": True,
    }


def _count_completed_trips(user_id: str) -> int:
    """Number of completed trips for a user (shared-ride history)."""
    trips = dynamo.query_gsi(
        index_name="GSI2-UserTrips",
        pk_name="gsi2pk",
        pk_value=f"USER#{user_id}",
    )
    return sum(1 for t in trips if t.get("status") == "completed")
