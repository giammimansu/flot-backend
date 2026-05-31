"""Flot — typing WebSocket handler. Relays typing indicator to matched partner."""
from __future__ import annotations

import json

from aws_lambda_powertools import Logger, Tracer

from lib import dynamo, websocket as ws

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> dict:
    connection_id = event["requestContext"]["connectionId"]
    body: dict = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except json.JSONDecodeError:
            pass

    match_id = body.get("matchId")
    if not match_id:
        return {"statusCode": 400, "body": "matchId required"}

    conn = dynamo.get_item(f"CONN#{connection_id}", "META")
    if not conn:
        return {"statusCode": 401, "body": "Unknown connection"}
    sender_id = conn["userId"]

    match = dynamo.get_item(f"MATCH#{match_id}", "META")
    if not match:
        return {"statusCode": 404, "body": "Match not found"}

    partner_id = match["userId2"] if match["userId1"] == sender_id else match["userId1"]
    ws.send_to_user(partner_id, {
        "event": "typing",
        "data": {"matchId": match_id, "userId": sender_id},
    })
    return {"statusCode": 200, "body": "ok"}
