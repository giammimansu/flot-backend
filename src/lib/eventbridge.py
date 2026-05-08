"""Flot — EventBridge publisher.

All cross-service events flow through the custom bus 'flot-events'.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

logger = Logger(child=True)

_client = boto3.client("events")
_bus_name = os.environ.get("EVENT_BUS_NAME", "flot-events")
_source = "flot-backend"


def put_event(detail_type: str, detail: dict[str, Any], source: str | None = None) -> None:
    """Publish a single event to the Flot event bus.

    detail_type: e.g. 'trip.created', 'match.found', 'payment.completed'
    detail: free-form JSON payload
    """
    try:
        response = _client.put_events(
            Entries=[
                {
                    "Source": source or _source,
                    "DetailType": detail_type,
                    "Detail": json.dumps(detail, default=str),
                    "EventBusName": _bus_name,
                }
            ]
        )
        failed = response.get("FailedEntryCount", 0)
        if failed:
            logger.error(
                "EventBridge put_events partial failure",
                extra={"failed_count": failed, "detail_type": detail_type},
            )
    except ClientError as e:
        logger.error(
            "EventBridge put_event failed",
            extra={"error": str(e), "detail_type": detail_type},
        )
        raise
