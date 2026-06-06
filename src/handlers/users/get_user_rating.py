"""Flot — GET /users/{userId}/rating (P2 #11).

Public average rating for a user, computed from the running aggregates
(`ratingSum` / `ratingCount`) stored on the profile by create_review.
"""
from __future__ import annotations

from decimal import Decimal

from aws_lambda_powertools import Logger, Tracer

from lib.dynamo import get_item
from lib.http import AppError, app_handler, success

logger = Logger()
tracer = Tracer()


def compute_rating(profile: dict) -> dict:
    """Return {average, count} from a profile's rating aggregates."""
    count = int(profile.get("ratingCount", 0) or 0)
    total = Decimal(str(profile.get("ratingSum", 0) or 0))
    average = round(float(total) / count, 2) if count else None
    return {"average": average, "count": count}


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    origin: str | None = event["_origin"]

    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId")
    if not user_id:
        raise AppError(400, "Missing userId")

    profile = get_item(f"USER#{user_id}", "PROFILE")
    if not profile:
        raise AppError(404, "User not found")

    return success({"userId": user_id, **compute_rating(profile)}, origin)
