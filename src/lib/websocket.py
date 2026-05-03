"""Flot — WebSocket connection manager.

Uses API Gateway Management API to push messages to connected clients.
Connections are stored in DynamoDB; users are looked up via GSI3-UserConn.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from . import dynamo

logger = Logger(child=True)

CONN_TTL_SECONDS = 24 * 60 * 60  # 24h

# Endpoint set by environment (template injects WS endpoint URL).
_endpoint = os.environ.get("WS_ENDPOINT")
_apigw_client = boto3.client("apigatewaymanagementapi", endpoint_url=_endpoint) if _endpoint else None


# ── Connection registry ──────────────────────────────────────────────


def store_connection(connection_id: str, user_id: str) -> None:
    """Persist a new WS connection. PK=CONN#<id>, also indexed by userId on GSI3."""
    now = int(time.time())
    item = {
        "pk": f"CONN#{connection_id}",
        "sk": "META",
        "connectionId": connection_id,
        "userId": user_id,
        "connectedAt": now,
        "ttl": now + CONN_TTL_SECONDS,
        "gsi3pk": f"USER#{user_id}",
        "gsi3sk": connection_id,
    }
    dynamo.put_item(item)


def remove_connection(connection_id: str) -> None:
    """Delete connection record."""
    dynamo.delete_item(f"CONN#{connection_id}", "META")


def get_user_connections(user_id: str) -> list[str]:
    """Return all active connection IDs for a user (GSI3-UserConn)."""
    items = dynamo.query_gsi(
        index_name="GSI3-UserConn",
        pk_name="gsi3pk",
        pk_value=f"USER#{user_id}",
    )
    return [it["gsi3sk"] for it in items if "gsi3sk" in it]


# ── Push messages ────────────────────────────────────────────────────


def _client():
    if not _apigw_client:
        raise RuntimeError("WS_ENDPOINT not configured")
    return _apigw_client


def send_to_connection(connection_id: str, payload: dict[str, Any]) -> bool:
    """Send a JSON payload to a single connection. Returns False if stale (410)."""
    try:
        _client().post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(payload, default=str).encode("utf-8"),
        )
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("GoneException", "410"):
            logger.info("Stale WS connection — removing", extra={"connectionId": connection_id})
            remove_connection(connection_id)
            return False
        logger.error("WS send failed", extra={"error": str(e), "connectionId": connection_id})
        raise


def send_to_user(user_id: str, payload: dict[str, Any]) -> int:
    """Fan-out a payload to all active connections of a user. Returns delivered count."""
    delivered = 0
    for conn_id in get_user_connections(user_id):
        if send_to_connection(conn_id, payload):
            delivered += 1
    return delivered
