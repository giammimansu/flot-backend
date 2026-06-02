"""Flot — EventBridge handler for match.expired

Fires when a definitive match never reached "completed" and the flight has
already departed. Marks the match and both trips as expired (no re-pool — the
flight is gone) and notifies both users.
"""
from lib.dynamo import get_match, get_trip, table, now_iso
from lib.notifications import save_notification
from lib.state_machine import MatchStateMachine, TripStateMachine
from aws_lambda_powertools import Logger

logger = Logger()


def expire_trip(trip: dict) -> None:
    """Mark a trip as expired and remove it from the active GSI5 index."""
    table.update_item(
        Key={"pk": trip["pk"], "sk": "META"},
        UpdateExpression="SET #status = :s, updatedAt = :ua REMOVE gsi5pk",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":s": "expired",
            ":ua": now_iso(),
        },
    )
    logger.info("trip_expired", tripId=trip["pk"])


@logger.inject_lambda_context
def handler(event, context):
    detail = event["detail"]
    match_id = detail["matchId"]
    reason = detail.get("reason", "flight_departed")
    match = get_match(match_id)

    if MatchStateMachine.is_terminal(match["status"]):
        return  # già gestito

    # 1. Match → expired
    table.update_item(
        Key={"pk": f"MATCH#{match_id}", "sk": "META"},
        UpdateExpression="SET #status = :s, dissolveReason = :r, updatedAt = :ua",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":s": "expired",
            ":r": reason,
            ":ua": now_iso(),
        },
    )

    # 2. Entrambi i trip → expired (no re-pool: il volo è partito)
    for trip_id in (match["tripId1"], match["tripId2"]):
        trip = get_trip(trip_id)
        if not TripStateMachine.is_terminal(trip["status"]):
            expire_trip(trip)

    # 3. Notifica entrambi gli utenti
    for user_id in (match["userId1"], match["userId2"]):
        save_notification(
            user_id,
            "Match scaduto",
            "Il volo è partito e il match non è stato completato.",
            {"type": "match_expired", "matchId": match_id},
        )

    logger.info("match_expired", matchId=match_id, reason=reason)
