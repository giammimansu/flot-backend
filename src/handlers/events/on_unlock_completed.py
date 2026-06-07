"""Flot — EventBridge handler for payment.completed (full unlock).

Fires once both users have unlocked the match (status → unlocked), in both
the real Stripe path (webhook-driven capture) and the BETA_MODE path.
Posts the "chat is open" system message and notifies both users over WS so
the client reveals the chat.
"""
from __future__ import annotations

from lib.dynamo import get_match
from lib.websocket import send_to_user
from handlers.chat.system_message import post_match_unlocked
from aws_lambda_powertools import Logger

logger = Logger()


@logger.inject_lambda_context
def handler(event, context):
    detail = event["detail"]
    match_id = detail["matchId"]
    user_id1 = detail.get("userId1")
    user_id2 = detail.get("userId2")

    # 1. System message in chat (persisted even if WS delivery fails)
    try:
        post_match_unlocked(match_id)
    except Exception:
        logger.warning("system_message_failed", matchId=match_id)

    # 2. WS event so the client opens the chat immediately
    payload = {"type": "match.unlocked", "data": {"matchId": match_id}}
    for uid in (user_id1, user_id2):
        if uid:
            try:
                send_to_user(uid, payload)
            except Exception:
                logger.warning("ws_notify_failed", matchId=match_id, userId=uid)

    logger.info("unlock_completed_notified", matchId=match_id)
