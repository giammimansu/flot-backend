"""Flot — EventBridge handler for match.found

Sends real-time WebSocket notifications to the two matched users.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib.notifications import notify_match_found
from handlers.chat.system_message import post_match_confirmed

logger = Logger()
tracer = Tracer()

@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> None:
    """Handle match.found events."""
    detail = event.get("detail", {})
    match_id = detail.get("matchId")
    user1 = detail.get("userId1")
    user2 = detail.get("userId2")
    # For compatibility with matchmaker logic where tripId1/tripId2 might not be passed
    # we just provide the basic notification payload 
    
    if not all([match_id, user1, user2]):
        logger.warning("Missing required fields in match.found event")
        return

    # Notify User 1
    u1_payload = {
        "type": "MATCH_FOUND",
        "matchId": match_id,
        "matchedWith": user2,
    }
    notify_match_found(user1, detail, u1_payload)
    logger.info("Notified user1", extra={"userId": user1})

    # Notify User 2
    u2_payload = {
        "type": "MATCH_FOUND",
        "matchId": match_id,
        "matchedWith": user1,
    }
    notify_match_found(user2, detail, u2_payload)
    logger.info("Notified user2", extra={"userId": user2})

    # System message — posted once for the match
    try:
        post_match_confirmed(match_id)
    except Exception:
        logger.warning("system_message_failed", matchId=match_id)
