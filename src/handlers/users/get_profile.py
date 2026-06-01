"""Flot — GET /users/me handler.

Returns the authenticated user's profile from DynamoDB.
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
    """GET /users/me — Return current user profile."""
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]

    item = get_item(pk=f"USER#{user_id}", sk="PROFILE")

    if not item:
        raise AppError(404, "User profile not found")

    # Strip internal DynamoDB keys before returning
    profile = {
        "userId": item.get("userId"),
        "email": item.get("email"),
        "name": item.get("name"),
        "photoUrl": item.get("photoUrl"),
        "blurredPhotoUrl": item.get("blurredPhotoUrl"),
        "thumbUrl": item.get("thumbUrl"),
        "isPro": item.get("isPro", False),
        "verified": item.get("verified", False),
        "lang": item.get("lang"),
        "gender": item.get("gender"),
        "ageGroup": item.get("ageGroup"),
        "onboarding": item.get("onboarding", False),
        "createdAt": item.get("createdAt"),
    }

    return success(profile, origin)
