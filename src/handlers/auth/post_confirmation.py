"""Flot — Cognito PostConfirmation trigger.

Runs after a user signs up via Google/Apple social login.
Creates the USER#{userId} / PROFILE item in DynamoDB.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit

from lib.dynamo import put_item

logger = Logger()
tracer = Tracer()
metrics = Metrics()


@logger.inject_lambda_context(log_event=True)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict, context) -> dict:
    """Cognito PostConfirmation Lambda trigger.

    Called after user confirms signup (auto-confirmed for social login).
    Creates the user profile in DynamoDB.
    """
    trigger_source = event.get("triggerSource", "")

    # Only process PostConfirmation_ConfirmSignUp
    if trigger_source != "PostConfirmation_ConfirmSignUp":
        logger.info("Skipping trigger", extra={"triggerSource": trigger_source})
        return event

    user_attributes = event.get("request", {}).get("userAttributes", {})
    user_id = user_attributes.get("sub")

    if not user_id:
        logger.error("Missing sub in userAttributes")
        return event

    # Extract profile data from Cognito attributes
    email = user_attributes.get("email", "")
    name = user_attributes.get("name") or user_attributes.get("given_name", "")
    picture = user_attributes.get("picture", "")

    now = datetime.now(timezone.utc).isoformat()

    user_item = {
        "pk": f"USER#{user_id}",
        "sk": "PROFILE",
        "userId": user_id,
        "email": email,
        "name": name,
        "photoUrl": picture,
        "blurredPhotoUrl": "",
        "thumbUrl": "",
        "isPro": False,
        "verified": False,
        "lang": "it",  # Default for MXP launch
        "gender": None,
        "ageGroup": None,
        "createdAt": now,
        "updatedAt": now,
        # GSI2: userId → createdAt (for user trip history)
        "gsi2pk": user_id,
        "gsi2sk": now,
    }

    put_item(user_item)

    logger.info("User created", extra={"userId": user_id, "email": email})
    metrics.add_metric(name="UserCreated", unit=MetricUnit.Count, value=1)

    # Note: EventBridge user.created event will be added in Sprint 2

    # Must return the event object for Cognito
    return event
