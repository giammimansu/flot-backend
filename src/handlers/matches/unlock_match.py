import os
from datetime import datetime, timedelta, timezone

import stripe
from boto3.dynamodb.conditions import Attr

from lib.airports import get_airport
from lib.dynamo import get_match, get_trip, get_user, table, now_iso
from lib.events import put_event
from lib.auth import get_user_id
from lib.errors import AppError
from lib.validation import validate
from pydantic import BaseModel
from aws_lambda_powertools import Logger
from lib.schedulers import create_unlock_timeout_schedule, cancel_unlock_timeout_schedule

logger = Logger()
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

class UnlockRequest(BaseModel):
    matchId: str

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
def handler(event, context):
    user_id = get_user_id(event)
    body = validate(UnlockRequest, event.get("body", "{}"))
    match = get_match(body.matchId)
    trip_id = event["pathParameters"]["tripId"]
    trip = get_trip(trip_id)
    airport = get_airport(match["airportCode"])

    # Validazioni
    if match["status"] not in ("pending", "partially_unlocked"):
        raise AppError(400, "Match is not in a valid state for unlock")

    if user_id in match.get("unlockedBy", []):
        raise AppError(400, "You have already unlocked this match")

    if user_id not in (match["userId1"], match["userId2"]):
        raise AppError(403, "Not your match")

    # FAKE_DOOR_MODE check
    if os.environ.get("FAKE_DOOR_MODE") == "true":
        # Registra intent senza Stripe
        record_fake_door_intent(user_id, match["matchId"])
        return {"fakeDoor": True, "message": "Coming soon"}

    # Crea PaymentIntent con capture manuale
    intent = stripe.PaymentIntent.create(
        amount=airport.unlock_fee,
        currency=airport.currency.lower(),
        capture_method="manual",
        metadata={
            "matchId": match["matchId"],
            "userId": user_id,
            "tripId": trip["tripId"],
            "airportCode": airport.code,
        },
    )

    now = datetime.now(timezone.utc)
    unlocked_by = match.get("unlockedBy", []) + [user_id]

    if len(unlocked_by) == 1:
        # ── PRIMO UNLOCK ──
        # Auth hold attivo, ma nessun capture
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
            ConditionExpression=Attr("status").eq("pending"),  # idempotenza
        )

        # Emetti evento per notifica urgente al partner
        partner_id = match["userId2"] if user_id == match["userId1"] else match["userId1"]
        partner_user = get_user(partner_id)
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

        # Crea EventBridge Scheduler one-shot per il timeout
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
        # ── SECONDO UNLOCK — CAPTURE SIMULTANEO ──
        first_pi_id = match["firstUnlockPaymentIntentId"]

        # Capture entrambi in transazione
        try:
            stripe.PaymentIntent.capture(first_pi_id)
            stripe.PaymentIntent.capture(intent.id)
        except stripe.error.StripeError as e:
            # Se il capture del primo fallisce (expired?), void il secondo
            logger.error("capture_failed", matchId=match["matchId"], error=str(e))
            stripe.PaymentIntent.cancel(intent.id)
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

        # Cancella il timeout scheduler (non serve più)
        cancel_unlock_timeout_schedule(match["matchId"])

        # Emetti payment.completed per entrambi
        put_event("payment.completed", {
            "matchId": match["matchId"],
            "userId1": match["userId1"],
            "userId2": match["userId2"],
        })

        logger.info("match_fully_unlocked", matchId=match["matchId"])

    # Salva Payment record
    save_payment(user_id, match["matchId"], intent.id, airport)

    return {
        "statusCode": 200,
        "body": {
            "paymentIntentClientSecret": intent.client_secret,
            "amount": airport.unlock_fee,
            "currency": airport.currency.lower(),
            "matchStatus": "partially_unlocked" if len(unlocked_by) == 1 else "unlocked",
        }
    }
