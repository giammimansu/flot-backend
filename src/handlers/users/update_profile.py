"""Flot — PUT /users/me handler.

Updates the authenticated user's profile in DynamoDB.
Only allows updating: name, lang, gender, ageGroup.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aws_lambda_powertools import Logger, Tracer
from pydantic import ValidationError

from lib.dynamo import update_item
from lib.http import AppError, app_handler, success
from lib.validation import UpdateProfileRequest

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    """PUT /users/me — Update user profile fields."""
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]
    body: dict = event["_body"]

    # Validate input with Pydantic (rejects unknown fields)
    try:
        request = UpdateProfileRequest(**body)
    except ValidationError as e:
        raise AppError(400, "Validation error", details={"errors": e.errors()}) from e

    # Build updates dict (only non-None fields)
    updates: dict = {}
    for field_name, value in request.model_dump(exclude_none=True).items():
        # Convert enums to their string value
        updates[field_name] = value.value if hasattr(value, "value") else value

    if not updates:
        raise AppError(400, "No valid fields to update")

    # Always update the updatedAt timestamp
    updates["updatedAt"] = datetime.now(timezone.utc).isoformat()

    pk = f"USER#{user_id}"
    sk = "PROFILE"

    updated_item = update_item(pk=pk, sk=sk, updates=updates)

    # Build response (strip internal keys)
    profile = {
        "userId": updated_item.get("userId"),
        "email": updated_item.get("email"),
        "name": updated_item.get("name"),
        "photoUrl": updated_item.get("photoUrl"),
        "blurredPhotoUrl": updated_item.get("blurredPhotoUrl"),
        "thumbUrl": updated_item.get("thumbUrl"),
        "isPro": updated_item.get("isPro", False),
        "verified": updated_item.get("verified", False),
        "lang": updated_item.get("lang"),
        "gender": updated_item.get("gender"),
        "ageGroup": updated_item.get("ageGroup"),
        "createdAt": updated_item.get("createdAt"),
    }

    logger.info("Profile updated", extra={"userId": user_id, "fields": list(updates.keys())})

    return success(profile, origin)
