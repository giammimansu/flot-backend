"""Flot — EventBridge handler for trip.completed.

Fires when a definitive match reached "unlocked" (connection established) and the
flight has departed + tolerance. This is the terminal happy-path of the lifecycle:

  Match:  unlocked          → completed
  Trip:   matched/...       → completed

Side effects:
  1. Mark match + both trips as completed (idempotent).
  2. Set DynamoDB TTL on all ChatMessage items of the match (48h from completedAt).
  3. Emit "review.requested" hook (consumed by the rating system — plan #11).
  4. Notify both users to leave a review.

Trigger source: dissolve_checker (scheduled scan of matched trips).
"""
from __future__ import annotations

import os
import time

from boto3.dynamodb.conditions import Key
from aws_lambda_powertools import Logger, Tracer

from lib.dynamo import get_match, get_trip, table, now_iso
from lib.eventbridge import put_event
from lib.notifications import deliver
from lib.state_machine import MatchStateMachine, TripStateMachine, InvalidTransitionError

logger = Logger()
tracer = Tracer()

CHAT_TTL_HOURS = int(os.environ.get("CHAT_TTL_HOURS", "48"))

# Match states from which completion is legal — kept for readability; authoritative
# source is MatchStateMachine._MATCH_EDGES.
_COMPLETABLE = ("unlocked",)


def _set_chat_ttl(match_id: str, ttl_epoch: int) -> int:
    """Set DynamoDB TTL on every ChatMessage of the match. Returns count updated.

    ChatMessages live under pk=MATCH#<id>, sk begins_with "CHAT#".
    """
    updated = 0
    last_key = None
    while True:
        kwargs = {
            "KeyConditionExpression": Key("pk").eq(f"MATCH#{match_id}")
            & Key("sk").begins_with("CHAT#"),
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = table.query(**kwargs)
        for msg in resp.get("Items", []):
            table.update_item(
                Key={"pk": msg["pk"], "sk": msg["sk"]},
                UpdateExpression="SET #ttl = :t",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":t": ttl_epoch},
            )
            updated += 1
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return updated


def _complete_trip(trip: dict, completed_at: str) -> None:
    """Mark a trip completed and drop it from the active GSI5 index."""
    table.update_item(
        Key={"pk": trip["pk"], "sk": "META"},
        UpdateExpression="SET #status = :s, completedAt = :c, updatedAt = :ua REMOVE gsi5pk",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":s": "completed",
            ":c": completed_at,
            ":ua": completed_at,
        },
    )


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> None:
    detail = event["detail"]
    match_id = detail["matchId"]

    match = get_match(match_id)
    if not match:
        logger.warning("trip_completed_match_missing", matchId=match_id)
        return

    if MatchStateMachine.is_terminal(match["status"]):
        logger.info("trip_completed_skip_terminal", matchId=match_id, status=match["status"])
        return  # idempotent — already handled

    if match["status"] not in _COMPLETABLE:
        # Not actually a happy-path completion (never unlocked). Leave for expire flow.
        logger.warning(
            "trip_completed_illegal_state",
            matchId=match_id,
            status=match["status"],
        )
        return

    completed_at = now_iso()

    # 1. Match → completed (guard against concurrent completion).
    try:
        table.update_item(
            Key={"pk": f"MATCH#{match_id}", "sk": "META"},
            UpdateExpression="SET #status = :s, completedAt = :c, updatedAt = :c",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":s": "completed", ":c": completed_at, ":u": "unlocked"},
            ConditionExpression="#status = :u",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info("trip_completed_race_skip", matchId=match_id)
        return

    # 2. Both trips → completed.
    for trip_id in (match["tripId1"], match["tripId2"]):
        trip = get_trip(trip_id)
        if trip and trip["status"] not in ("cancelled", "expired", "completed"):
            _complete_trip(trip, completed_at)

    # 3. Chat TTL — 48h from completion.
    ttl_epoch = int(time.time()) + CHAT_TTL_HOURS * 3600
    chat_count = _set_chat_ttl(match_id, ttl_epoch)

    # 4. Review hook (plan #11) + 5. notify both users.
    for user_id in (match["userId1"], match["userId2"]):
        put_event("review.requested", {
            "matchId": match_id,
            "userId": user_id,
            "completedAt": completed_at,
        })
        # deliver() persists in-app internally (persist=True) AND pushes
        # WS/Push/Email — so the review request reaches the user even with the
        # app closed. Do NOT also call save_notification (would double-persist).
        deliver(
            user_id,
            "Com'è andata?",
            "Il tuo viaggio condiviso è completato. Lascia una recensione al tuo partner.",
            {"type": "review_requested", "matchId": match_id},
        )

    logger.info(
        "trip_completed",
        matchId=match_id,
        chatMessagesExpiring=chat_count,
        completedAt=completed_at,
    )
