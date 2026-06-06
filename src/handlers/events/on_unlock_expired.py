import stripe
from boto3.dynamodb.conditions import Attr

from lib.airports import get_airport
from lib.dynamo import get_match, get_trip, table, now_iso
from lib.notifications import notify_user
from lib.state_machine import MatchStateMachine, TripStateMachine
from lib.metrics import business_metrics
from lib.trust import record_violation
from aws_lambda_powertools import Logger
from lib.schedulers import cancel_all_unlock_reminders

logger = Logger()

def repool_trip(trip: dict):
    """Rimette un trip nel pool per il re-match dal Matchmaker."""
    table.update_item(
        Key={"pk": trip["pk"], "sk": "META"},
        UpdateExpression=(
            "SET #status = :status, "
            "gsi5pk = :gsi5, "
            "matchId = :mid, "
            "updatedAt = :ua"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "scheduled",
            ":gsi5": f"{trip['airportCode']}#scheduled",
            ":mid": None,
            ":ua": now_iso(),
        },
    )
    logger.info("trip_repooled", tripId=trip["pk"])

@logger.inject_lambda_context
def handler(event, context):
    detail = event["detail"]
    match_id = detail["matchId"]
    match = get_match(match_id)

    # Guard: il match potrebbe essere stato sbloccato nel frattempo
    if MatchStateMachine.is_terminal(match["status"]) or match["status"] != "partially_unlocked":
        logger.info("unlock_timeout_skipped",
            matchId=match_id,
            currentStatus=match["status"],
        )
        return

    airport = get_airport(match["airportCode"])

    # 1. Void PaymentIntent del primo unlock
    first_pi_id = match.get("firstUnlockPaymentIntentId")
    if first_pi_id:
        try:
            stripe.PaymentIntent.cancel(first_pi_id)
            logger.info("payment_intent_voided", piId=first_pi_id)
        except stripe.error.StripeError as e:
            logger.error("void_failed", piId=first_pi_id, error=str(e))
            # Continua comunque — il PI scadrà da solo

    # 2. Aggiorna Match status
    table.update_item(
        Key={"pk": f"MATCH#{match_id}", "sk": "META"},
        UpdateExpression=(
            "SET #status = :status, "
            "dissolveReason = :reason, "
            "updatedAt = :ua"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "unlock_expired",
            ":reason": "partner_no_response",
            ":ua": now_iso(),
        },
        ConditionExpression=Attr("status").eq("partially_unlocked"),
    )

    # 3. Re-pool entrambi i trip (se abilitato)
    if airport.unlock_repool_enabled:
        trip_a = get_trip(match["tripId1"])
        trip_b = get_trip(match["tripId2"])

        for trip in [trip_a, trip_b]:
            if not TripStateMachine.is_terminal(trip["status"]) and trip["status"] in ("matched", "partially_unlocked_wait"):
                # Aggiungiamo logic per evitare re-match con lo stesso partner
                previous_partner_user_id = match["userId2"] if trip["userId"] == match["userId1"] else match["userId1"]
                
                table.update_item(
                    Key={"pk": trip["pk"], "sk": "META"},
                    UpdateExpression=(
                        "SET #status = :status, "
                        "gsi5pk = :gsi5, "
                        "matchId = :mid, "
                        "previousMatchPartners = list_append("
                        "  if_not_exists(previousMatchPartners, :empty_list), :new_partner"
                        "), "
                        "updatedAt = :ua"
                    ),
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":status": "scheduled",
                        ":gsi5": f"{trip['airportCode']}#scheduled",
                        ":mid": None,
                        ":empty_list": [],
                        ":new_partner": [previous_partner_user_id],
                        ":ua": now_iso(),
                    },
                )
                logger.info("trip_repooled_with_exclusion", tripId=trip["pk"], excludedPartner=previous_partner_user_id)

    # 4. Notifica entrambi gli utenti
    unlocked_user_id = match["unlockedBy"][0]
    partner_user_id = match["userId2"] if unlocked_user_id == match["userId1"] else match["userId1"]

    # Al primo (che ha pagato): rassicurazione
    notify_user(unlocked_user_id, {
        "type": "unlock_expired_payer",
        "title": "Nessun addebito",
        "body": "Il tuo partner non ha risposto in tempo. €0 addebitati. Cerchiamo qualcun altro!",
        "matchId": match_id,
    })

    # Al secondo (che non ha risposto): info
    notify_user(partner_user_id, {
        "type": "unlock_expired_non_payer",
        "title": "Match scaduto",
        "body": "Non hai sbloccato in tempo. Cercheremo un nuovo partner per te.",
        "matchId": match_id,
    })

    # 5. Cancella eventuali reminder schedulati rimasti
    cancel_all_unlock_reminders(match_id)

    # P2 #10 — non-responder accrues a trust violation (may trigger ban).
    record_violation(partner_user_id, "unlock_no_response", airport)

    # Business metric: deadlock timed out
    business_metrics.record_deadlock_resolution(resolved=False, airport_code=match.get("airportCode", "ALL"))

    logger.info("unlock_expired",
        matchId=match_id,
        unlockedBy=unlocked_user_id,
        nonResponder=partner_user_id,
    )
