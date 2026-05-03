"""Flot — $disconnect WebSocket handler. Removes the CONN# record."""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib import websocket as ws

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> dict:
    connection_id = event["requestContext"]["connectionId"]
    ws.remove_connection(connection_id)
    logger.info("WS disconnected", extra={"connectionId": connection_id})
    return {"statusCode": 200, "body": "Disconnected"}
