"""Flot — Cognito PostConfirmation + PostAuthentication trigger.

Handles both triggers to cover email/password and social (Google/Apple) signups.
Creates the USER#{userId} / PROFILE item in DynamoDB on first login.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit

from lib.dynamo import get_item, put_item

logger = Logger()
tracer = Tracer()
metrics = Metrics()

_HANDLED_TRIGGERS = {
    "PostConfirmation_ConfirmSignUp",
    "PostAuthentication_Authentication",
}


@logger.inject_lambda_context(log_event=True)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict, context) -> dict:
    trigger_source = event.get("triggerSource", "")

    if trigger_source not in _HANDLED_TRIGGERS:
        logger.info("Skipping trigger", extra={"triggerSource": trigger_source})
        return event

    user_attributes = event.get("request", {}).get("userAttributes", {})
    user_id = user_attributes.get("sub")

    if not user_id:
        logger.error("Missing sub in userAttributes")
        return event

    # For PostAuthentication, skip if profile already exists (not first login)
    if trigger_source == "PostAuthentication_Authentication":
        existing = get_item(f"USER#{user_id}", "PROFILE")
        if existing:
            logger.info("User already exists, skipping", extra={"userId": user_id})
            return event

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
        "lang": "it",
        "gender": None,
        "ageGroup": None,
        "onboarding": False,
        "createdAt": now,
        "updatedAt": now,
        "gsi2pk": user_id,
        "gsi2sk": now,
    }

    put_item(user_item)

    logger.info("User created", extra={"userId": user_id, "email": email, "triggerSource": trigger_source})
    metrics.add_metric(name="UserCreated", unit=MetricUnit.Count, value=1)

    return event
