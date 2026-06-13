"""Flot — GET /users/{userId}/reviews handler.

Returns reviews received by another user. Caller must share an active match
with the target (same authz rule as the public profile). Reviews are
anonymous (no reviewer identity exposed).
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from handlers.users.get_user_public import _find_matched_trip_with
from lib import dynamo
from lib.http import AppError, app_handler, success

logger = Logger()
tracer = Tracer()

MAX_REVIEWS = 20


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

    # Verify caller shares a match with target.
    if not _find_matched_trip_with(caller_id, target_id):
        raise AppError(403, "Forbidden")

    items = dynamo.get_user_reviews(target_id, limit=MAX_REVIEWS)
    reviews = [
        {
            "rating": int(r.get("rating", 0) or 0),
            "comment": r.get("comment"),
            "airportCode": r.get("airportCode"),
            "createdAt": r.get("createdAt"),
        }
        for r in items
    ]

    return success({"userId": target_id, "count": len(reviews), "reviews": reviews}, origin)
