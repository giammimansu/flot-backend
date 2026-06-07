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

from lib.dynamo import table, now_iso, get_match
from lib.eventbridge import put_event
from lib.http import app_handler, success, AppError
from lib.metrics import business_metrics
from lib.schedulers import cancel_unlock_timeout_schedule

logger = Logger()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

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


def _mark_payments_captured(match: dict, first_pi: str, second_pi: str) -> None:
    """Best-effort: flip both Payment records to captured."""
    unlocked_by = match.get("unlockedBy", [])
    pairs = []
    if len(unlocked_by) == 2:
        pairs = [(unlocked_by[0], first_pi), (unlocked_by[1], second_pi)]
    for user_id, pi_id in pairs:
        try:
            table.update_item(
                Key={"pk": f"USER#{user_id}", "sk": f"PAYMENT#{pi_id}"},
                UpdateExpression="SET #s = :captured, updatedAt = :ua",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":captured": "captured", ":ua": now_iso()},
            )
        except ClientError as e:
            logger.warning("payment_mark_captured_failed", piId=pi_id, error=str(e))


def _release_capture_claim(match_id: str) -> None:
    """Drop the captureInProgress claim so a later webhook/retry can try again."""
    try:
        table.update_item(
            Key={"pk": f"MATCH#{match_id}", "sk": "META"},
            UpdateExpression="REMOVE captureInProgress",
        )
    except ClientError:
        pass


def _handle_amount_capturable_updated(pi: dict) -> None:
    """A hold was authorized. Capture both holds once BOTH partners are authorized.

    Manual-capture PaymentIntents must be confirmed by the client before capture.
    This webhook fires when a PI reaches requires_capture. We capture only when both
    the first and second holds are authorized, using the same safety ordering as #2
    (capture second first; refund it if the first then fails) so no one is charged
    unilaterally. A `captureInProgress` claim makes concurrent webhook deliveries safe.
    """
    match_id = pi.get("metadata", {}).get("matchId")
    if not match_id:
        logger.info("stripe_pi_authorized_no_match", piId=pi.get("id"))
        return

    match = get_match(match_id)
    if not match:
        logger.warning("stripe_pi_match_missing", piId=pi.get("id"), matchId=match_id)
        return

    if match.get("status") != "partially_unlocked":
        logger.info("capture_skip_status", matchId=match_id, status=match.get("status"))
        return

    first_pi = match.get("firstUnlockPaymentIntentId")
    second_pi = match.get("secondUnlockPaymentIntentId")
    if not (first_pi and second_pi):
        logger.info("capture_wait_second_hold", matchId=match_id, piId=pi.get("id"))
        return

    # Both PIs must be authorized (requires_capture) before we capture either.
    try:
        pi_first = stripe.PaymentIntent.retrieve(first_pi)
        pi_second = stripe.PaymentIntent.retrieve(second_pi)
    except stripe.error.StripeError as e:
        logger.error("capture_retrieve_failed", matchId=match_id, error=str(e))
        return
    if pi_first.status != "requires_capture" or pi_second.status != "requires_capture":
        logger.info(
            "capture_holds_not_ready",
            matchId=match_id,
            firstStatus=pi_first.status,
            secondStatus=pi_second.status,
        )
        return

    # Claim the capture so concurrent deliveries don't double-capture.
    try:
        table.update_item(
            Key={"pk": f"MATCH#{match_id}", "sk": "META"},
            UpdateExpression="SET captureInProgress = :t, updatedAt = :ua",
            ConditionExpression="attribute_not_exists(captureInProgress) AND #s = :partial",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":t": True, ":partial": "partially_unlocked", ":ua": now_iso()},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("capture_already_claimed", matchId=match_id)
            return
        raise

    # Capture second PI first. If it fails, cancel it — first is untouched → €0 charged.
    try:
        stripe.PaymentIntent.capture(second_pi)
    except stripe.error.StripeError as e:
        logger.error("capture_second_failed", matchId=match_id, piId=second_pi, error=str(e))
        try:
            stripe.PaymentIntent.cancel(second_pi)
        except stripe.error.StripeError:
            pass
        _release_capture_claim(match_id)
        return

    # Capture first PI. If it fails after the second was captured, refund the second → €0.
    try:
        stripe.PaymentIntent.capture(first_pi)
    except stripe.error.StripeError as e:
        logger.error("capture_first_failed", matchId=match_id, piId=first_pi, error=str(e))
        try:
            stripe.Refund.create(payment_intent=second_pi)
        except stripe.error.StripeError as re:
            logger.error("capture_refund_failed", piId=second_pi, error=str(re))
        _release_capture_claim(match_id)
        return

    # Both captured → finalize the unlock.
    try:
        table.update_item(
            Key={"pk": f"MATCH#{match_id}", "sk": "META"},
            UpdateExpression="SET #s = :unlocked, updatedAt = :ua REMOVE captureInProgress",
            ConditionExpression="#s = :partial",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":unlocked": "unlocked", ":partial": "partially_unlocked", ":ua": now_iso()},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("capture_finalize_race", matchId=match_id)
            return
        raise

    cancel_unlock_timeout_schedule(match_id)
    _mark_payments_captured(match, first_pi, second_pi)
    put_event("payment.completed", {
        "matchId": match_id,
        "userId1": match.get("userId1"),
        "userId2": match.get("userId2"),
    })
    business_metrics.record_deadlock_resolution(resolved=True, airport_code=match.get("airportCode", "ALL"))
    logger.info("match_captured_unlocked", matchId=match_id)


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
