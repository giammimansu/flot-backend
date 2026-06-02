import json
import os
from datetime import datetime, timedelta, timezone

import stripe
from boto3.dynamodb.conditions import Attr

from lib.airports import get_airport
from lib.dynamo import get_match, get_trip, get_user, table, now_iso
from lib.eventbridge import put_event
from lib.http import AppError, app_handler, success
from lib.schedulers import create_unlock_timeout_schedule, cancel_unlock_timeout_schedule
from lib.state_machine import MatchStateMachine, InvalidTransitionError
from lib.metrics import business_metrics
from aws_lambda_powertools import Logger

logger = Logger()
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

def record_fake_door_intent(user_id: str, match_id: str):
    logger.info("fake_door_unlock_intent", userId=user_id, matchId=match_id)

def save_payment(user_id: str, match_id: str, intent_id: str, airport):
    table.put_item(
        Item={
            "pk": f"USER#{user_id}",
            "sk": f"PAYMENT#{intent_id}",
            "matchId": match_id,
            "amount": airport.unlock_fee,
            "currency": airport.currency.lower(),
            "createdAt": now_iso(),
            "status": "requires_capture",
        }
    )

@logger.inject_lambda_context
@app_handler(requires_auth=True)
def handler(event, context):
    user_id: str = event["_user_id"]
    origin: str | None = event.get("_origin")
    body = json.loads(event.get("body") or "{}")
    match_id = body.get("matchId")
    if not match_id:
        raise AppError(400, "Missing matchId")
    match = get_match(match_id)
    if not match:
        raise AppError(404, "Match not found")
    trip_id = (event.get("pathParameters") or {}).get("tripId")
    trip = get_trip(trip_id) if trip_id else None
    airport = get_airport(match["airportCode"])

    # Validazioni
    if match["status"] not in ("pending", "partially_unlocked"):
        raise AppError(400, "Match is not in a valid state for unlock")

    target_status = "partially_unlocked" if len(match.get("unlockedBy", [])) == 0 else "unlocked"
    try:
        MatchStateMachine.transition(match["status"], target_status)
    except InvalidTransitionError as e:
        raise AppError(400, str(e))

    if user_id in match.get("unlockedBy", []):
        raise AppError(400, "You have already unlocked this match")

    if user_id not in (match["userId1"], match["userId2"]):
        raise AppError(403, "Not your match")

    # BETA_MODE: unlock gratuito per i primi utenti, senza Stripe né scheduler
    if os.environ.get("BETA_MODE") == "true":
        now = datetime.now(timezone.utc)
        unlocked_by = match.get("unlockedBy", []) + [user_id]
        new_status = "unlocked" if len(unlocked_by) >= 2 else "partially_unlocked"

        table.update_item(
            Key={"pk": match["pk"], "sk": "META"},
            UpdateExpression=(
                "SET #status = :status, "
                "unlockedBy = :ub, "
                "updatedAt = :ua"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": new_status,
                ":ub": unlocked_by,
                ":ua": now.isoformat(),
            },
        )

        logger.info("beta_unlock", matchId=match["matchId"], userId=user_id, newStatus=new_status)

        if new_status == "unlocked":
            try:
                put_event("payment.completed", {
                    "matchId": match["matchId"],
                    "userId1": match["userId1"],
                    "userId2": match["userId2"],
                    "beta": True,
                })
            except Exception:
                pass  # event bus opzionale in beta

        return success({"success": True, "matchStatus": new_status}, origin)

    # Crea PaymentIntent con capture manuale (stub se Stripe non configurato)
    stripe_key = os.environ.get("STRIPE_SECRET_KEY")
    if stripe_key:
        intent = stripe.PaymentIntent.create(
            amount=airport.unlock_fee,
            currency=airport.currency.lower(),
            capture_method="manual",
            metadata={
                "matchId": match["matchId"],
                "userId": user_id,
                "tripId": trip["tripId"] if trip else "",
                "airportCode": airport.code,
            },
        )
    else:
        import uuid as _uuid

        class _StubIntent:
            id = f"pi_stub_{_uuid.uuid4().hex[:12]}"
            client_secret = f"{id}_secret_stub"

        intent = _StubIntent()

    now = datetime.now(timezone.utc)
    unlocked_by = match.get("unlockedBy", []) + [user_id]

    if len(unlocked_by) == 1:
        deadline = now + timedelta(minutes=airport.unlock_timeout_minutes)

        table.update_item(
            Key={"pk": match["pk"], "sk": "META"},
            UpdateExpression=(
                "SET #status = :status, "
                "unlockedBy = :ub, "
                "firstUnlockAt = :fua, "
                "unlockDeadline = :ud, "
                "firstUnlockPaymentIntentId = :fpi, "
                "updatedAt = :ua"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "partially_unlocked",
                ":ub": unlocked_by,
                ":fua": now.isoformat(),
                ":ud": deadline.isoformat(),
                ":fpi": intent.id,
                ":ua": now.isoformat(),
            },
            ConditionExpression=Attr("status").eq("pending"),
        )

        partner_id = match["userId2"] if user_id == match["userId1"] else match["userId1"]
        unlocking_user = get_user(user_id)

        put_event("match.partially_unlocked", {
            "matchId": match["matchId"],
            "unlockedByUserId": user_id,
            "unlockedByName": unlocking_user.get("name", "Your partner").split()[0],
            "partnerUserId": partner_id,
            "unlockDeadline": deadline.isoformat(),
            "airportCode": airport.code,
            "reminderIntervals": airport.unlock_reminder_intervals,
        })

        create_unlock_timeout_schedule(
            match_id=match["matchId"],
            fire_at=deadline,
        )

        logger.info("match_partially_unlocked",
            matchId=match["matchId"],
            unlockedBy=user_id,
            deadline=deadline.isoformat(),
        )

    elif len(unlocked_by) == 2:
        first_pi_id = match.get("firstUnlockPaymentIntentId", "")

        if stripe_key:
            # Capture second PI first. If it fails, cancel it — first PI never touched,
            # so no charge to either user. Only then capture first PI (already authorized).
            try:
                stripe.PaymentIntent.capture(intent.id)
            except stripe.error.StripeError as e:
                logger.error("capture_second_failed", matchId=match["matchId"], piId=intent.id, error=str(e))
                try:
                    stripe.PaymentIntent.cancel(intent.id)
                except stripe.error.StripeError:
                    pass  # already expired/cancelled — safe
                raise AppError(500, "Payment capture failed. No charges applied.")

            try:
                stripe.PaymentIntent.capture(first_pi_id)
            except stripe.error.StripeError as e:
                # Second PI already captured — refund it so neither user pays.
                logger.error("capture_first_failed", matchId=match["matchId"], piId=first_pi_id, error=str(e))
                try:
                    stripe.Refund.create(payment_intent=intent.id)
                except stripe.error.StripeError as re:
                    logger.error("refund_failed", piId=intent.id, error=str(re))
                raise AppError(500, "Payment capture failed. No charges applied.")

        table.update_item(
            Key={"pk": match["pk"], "sk": "META"},
            UpdateExpression=(
                "SET #status = :status, "
                "unlockedBy = :ub, "
                "secondUnlockPaymentIntentId = :spi, "
                "updatedAt = :ua"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "unlocked",
                ":ub": unlocked_by,
                ":spi": intent.id,
                ":ua": now.isoformat(),
            },
            ConditionExpression=Attr("status").eq("partially_unlocked"),
        )

        cancel_unlock_timeout_schedule(match["matchId"])

        put_event("payment.completed", {
            "matchId": match["matchId"],
            "userId1": match["userId1"],
            "userId2": match["userId2"],
        })

        # Business metric: deadlock resolved (both users unlocked)
        business_metrics.record_deadlock_resolution(resolved=True, airport_code=match.get("airportCode", "ALL"))

        logger.info("match_fully_unlocked", matchId=match["matchId"])

    save_payment(user_id, match["matchId"], intent.id, airport)

    return success({
        "paymentIntentClientSecret": intent.client_secret,
        "amount": airport.unlock_fee,
        "currency": airport.currency.lower(),
        "matchStatus": "partially_unlocked" if len(unlocked_by) == 1 else "unlocked",
    }, origin)
