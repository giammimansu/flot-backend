"""Flot — GET /notifications handler.

Returns active notifications for the current user.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer
from botocore.exceptions import ClientError
from lib import dynamo
from lib.http import app_handler, success

logger = Logger()
tracer = Tracer()

@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]
    
    table = dynamo.get_table()
    
    try:
        response = table.query(
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :sk)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={":pk": f"USER#{user_id}", ":sk": "NOTIF#"},
            ScanIndexForward=False  # Descending order (newest first)
        )
        items = response.get("Items", [])
    except ClientError as e:
        logger.error("Failed to query notifications", exc_info=True)
        items = []

    return success({"notifications": items}, origin)
