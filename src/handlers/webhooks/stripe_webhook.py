"""Flot — Stripe webhook handler.

Handles Stripe events delivered to POST /webhooks/stripe.

Deduplication: each Stripe event ID is written to DynamoDB with a conditional
put (pk=STRIPE_EVENT#<id>, sk=META) before processing. A ConditionalCheckFailed
means we've already processed this event — safe to return 200 immediately.

Supported events:
  - payment_intent.amount_capturable_updated  (PI authorized, ready to capture)
  - payment_intent.payment_failed             (PI failed — alert ops)
  - payment_intent.canceled                   (PI voided)
"""
from __future__ import annotations

import json
import os

import stripe
from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

from lib.dynamo import table, now_iso
from lib.http import app_handler, success, AppError

logger = Logger()

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def _dedup_event(event_id: str) -> bool:
    """Idempotency guard. Returns True if event is new (should process), False if duplicate."""
    try:
        table.put_item(
            Item={
                "pk": f"STRIPE_EVENT#{event_id}",
                "sk": "META",
                "processedAt": now_iso(),
                # TTL: keep dedup records for 7 days to cover Stripe's 3-day retry window.
                "ttl": int(__import__("time").time()) + 7 * 86400,
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("stripe_event_duplicate", eventId=event_id)
            return False
        raise


def _handle_amount_capturable_updated(pi: dict) -> None:
    """PI is authorized. Log only — capture happens in unlock_match when both users unlock."""
    logger.info(
        "stripe_pi_authorized",
        piId=pi["id"],
        amount=pi.get("amount"),
        matchId=pi.get("metadata", {}).get("matchId"),
    )


def _handle_payment_failed(pi: dict) -> None:
    logger.error(
        "stripe_pi_failed",
        piId=pi["id"],
        lastError=pi.get("last_payment_error"),
        matchId=pi.get("metadata", {}).get("matchId"),
    )


def _handle_canceled(pi: dict) -> None:
    logger.info(
        "stripe_pi_canceled",
        piId=pi["id"],
        reason=pi.get("cancellation_reason"),
        matchId=pi.get("metadata", {}).get("matchId"),
    )


_HANDLERS = {
    "payment_intent.amount_capturable_updated": _handle_amount_capturable_updated,
    "payment_intent.payment_failed": _handle_payment_failed,
    "payment_intent.canceled": _handle_canceled,
}


@logger.inject_lambda_context
@app_handler(requires_auth=False)
def handler(event: dict, context) -> dict:
    raw_body = event.get("body") or ""
    sig_header = (event.get("headers") or {}).get("stripe-signature", "")

    # Verify webhook signature when secret is configured.
    if STRIPE_WEBHOOK_SECRET:
        try:
            stripe_event = stripe.Webhook.construct_event(
                raw_body, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except stripe.error.SignatureVerificationError:
            raise AppError(400, "Invalid Stripe signature")
    else:
        # No secret configured (dev/test) — parse body directly.
        try:
            stripe_event = json.loads(raw_body)
        except json.JSONDecodeError:
            raise AppError(400, "Invalid JSON body")

    event_id = stripe_event.get("id", "")
    event_type = stripe_event.get("type", "")

    if not _dedup_event(event_id):
        return success({"received": True, "duplicate": True}, event.get("_origin"))

    handler_fn = _HANDLERS.get(event_type)
    if handler_fn:
        pi = stripe_event.get("data", {}).get("object", {})
        handler_fn(pi)
    else:
        logger.debug("stripe_event_unhandled", eventType=event_type)

    return success({"received": True}, event.get("_origin"))
