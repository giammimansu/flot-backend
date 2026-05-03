"""Flot — $default WS handler.

Catch-all for messages that don't match a configured route. Responds with
an error so the client knows to use a known action.
"""
from __future__ import annotations

import json

from aws_lambda_powertools import Logger, Tracer

from lib import websocket as ws

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> dict:
    connection_id = event["requestContext"]["connectionId"]
    body = event.get("body")
    payload: dict = {}
    if body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}

    ws.send_to_connection(
        connection_id,
        {"type": "error", "message": "Unknown action", "received": payload.get("action")},
    )
    return {"statusCode": 200, "body": "ok"}
