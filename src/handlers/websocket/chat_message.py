"""Flot — WebSocket chat_message handler.

Route: action=chat_message
Payload: { "action": "chat_message", "matchId": "<id>", "text": "<msg>" }

Flow:
  1. Resolve sender from CONN# record (already authenticated at $connect).
  2. Validate match is unlocked and sender is a member.
  3. Validate text (1–1000 chars via ChatMessageCreate).
  4. Persist ChatMessage to DynamoDB.
  5. Relay to partner via WS; if offline, send push notification (throttled).
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal

from aws_lambda_powertools import Logger, Tracer
from pydantic import ValidationError

from lib import dynamo, websocket as ws
from lib.state_machine import MatchStateMachine
from lib.validation import ChatMessageCreate

logger = Logger()
tracer = Tracer()

# Minimum gap between push notifications for chat messages to the same user.
PUSH_THROTTLE_SECONDS = int(os.environ.get("CHAT_PUSH_THROTTLE_SEC", "300"))


def _should_push(match_id: str, recipient_id: str) -> bool:
    """Return True if enough time has passed since the last push for this chat."""
    state = dynamo.get_item(f"CHAT_PUSH_STATE#{match_id}", recipient_id)
    if not state:
        return True
    last_push = state.get("lastPushAt", 0)
    return (time.time() - float(last_push)) >= PUSH_THROTTLE_SECONDS


def _record_push(match_id: str, recipient_id: str) -> None:
    dynamo.table.put_item(Item={
        "pk": f"CHAT_PUSH_STATE#{match_id}",
        "sk": recipient_id,
        "lastPushAt": Decimal(str(time.time())),
        "ttl": int(time.time()) + 7 * 86400,
    })


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> dict:
    connection_id = event["requestContext"]["connectionId"]

    # 1. Resolve sender from connection record
    conn = dynamo.get_item(f"CONN#{connection_id}", "META")
    if not conn:
        return {"statusCode": 401, "body": "Unknown connection"}
    sender_id: str = conn["userId"]

    # 2. Parse and validate payload
    body: dict = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except json.JSONDecodeError:
            return {"statusCode": 400, "body": "Invalid JSON"}

    try:
        req = ChatMessageCreate.model_validate(body)
    except ValidationError as e:
        ws.send_to_connection(connection_id, {"type": "error", "message": "Invalid message", "details": e.errors()})
        return {"statusCode": 400, "body": "Validation error"}

    # 3. Validate match state and membership
    match = dynamo.get_match(req.matchId)
    if not match:
        ws.send_to_connection(connection_id, {"type": "error", "message": "Match not found"})
        return {"statusCode": 404, "body": "Match not found"}

    if sender_id not in (match.get("userId1"), match.get("userId2")):
        ws.send_to_connection(connection_id, {"type": "error", "message": "Not your match"})
        return {"statusCode": 403, "body": "Forbidden"}

    if match.get("status") != "unlocked":
        ws.send_to_connection(connection_id, {
            "type": "error",
            "message": "Chat only available on unlocked matches",
            "matchStatus": match.get("status"),
        })
        return {"statusCode": 400, "body": "Match not unlocked"}

    # 4. Persist message
    msg = dynamo.save_chat_message(
        match_id=req.matchId,
        sender_id=sender_id,
        text=req.text,
        msg_type="user",
    )

    # 5. Relay to partner (WS fanout)
    partner_id = match["userId2"] if sender_id == match["userId1"] else match["userId1"]
    payload = {
        "type": "chat.message",
        "data": {
            "matchId": req.matchId,
            "messageId": msg["messageId"],
            "senderId": sender_id,
            "text": req.text,
            "createdAt": msg["createdAt"],
        },
    }
    delivered = ws.send_to_user(partner_id, payload)

    # 6. Push fallback if partner offline (throttled)
    if delivered == 0 and _should_push(req.matchId, partner_id):
        partner = dynamo.get_user(partner_id)
        if partner and partner.get("pushToken"):
            try:
                from lib.notifications import send_push_notification
                sender_name = (dynamo.get_user(sender_id) or {}).get("name", "Il tuo partner")
                send_push_notification(
                    token=partner["pushToken"],
                    title=sender_name.split()[0] if sender_name else "Flot",
                    body=req.text[:80] + ("…" if len(req.text) > 80 else ""),
                    payload={"matchId": req.matchId, "action": "open_chat"},
                )
                _record_push(req.matchId, partner_id)
            except Exception:
                logger.warning("chat_push_failed", matchId=req.matchId, partnerId=partner_id)

    # Echo back to sender
    ws.send_to_connection(connection_id, {**payload, "type": "chat.message.sent"})

    logger.info("chat_message_sent", matchId=req.matchId, senderId=sender_id, delivered=delivered)
    return {"statusCode": 200, "body": "ok"}
