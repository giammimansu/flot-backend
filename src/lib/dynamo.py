"""Flot — DynamoDB client and helpers.

Module-level client is initialized once and reused across warm Lambda invocations.
All DynamoDB operations go through this module for consistent error handling.
"""
from __future__ import annotations

import os
from typing import Any
from decimal import Decimal

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

logger = Logger(child=True)

# ── Module-level client (reused across invocations) ──────────────────
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(os.environ.get("TABLE_NAME", "Flot"))


def get_table():
    """Return the DynamoDB Table resource."""
    return _table


def put_item(item: dict[str, Any]) -> dict[str, Any]:
    """Put a single item into DynamoDB."""
    try:
        _table.put_item(Item=item)
        return item
    except ClientError as e:
        logger.error("DynamoDB put_item failed", extra={"error": str(e), "pk": item.get("pk")})
        raise


def get_item(pk: str, sk: str) -> dict[str, Any] | None:
    """Get a single item by pk/sk. Returns None if not found."""
    try:
        response = _table.get_item(Key={"pk": pk, "sk": sk})
        return response.get("Item")
    except ClientError as e:
        logger.error("DynamoDB get_item failed", extra={"error": str(e), "pk": pk, "sk": sk})
        raise


def update_item(
    pk: str,
    sk: str,
    updates: dict[str, Any],
    condition_expression: str | None = None,
) -> dict[str, Any]:
    """Update specific attributes on an item. Returns the updated item."""
    if not updates:
        raise ValueError("No updates provided")

    expression_parts: list[str] = []
    expression_names: dict[str, str] = {}
    expression_values: dict[str, Any] = {}

    for i, (key, value) in enumerate(updates.items()):
        attr_name = f"#attr{i}"
        attr_value = f":val{i}"
        expression_parts.append(f"{attr_name} = {attr_value}")
        expression_names[attr_name] = key
        expression_values[attr_value] = value

    update_expression = "SET " + ", ".join(expression_parts)

    kwargs: dict[str, Any] = {
        "Key": {"pk": pk, "sk": sk},
        "UpdateExpression": update_expression,
        "ExpressionAttributeNames": expression_names,
        "ExpressionAttributeValues": expression_values,
        "ReturnValues": "ALL_NEW",
    }
    if condition_expression:
        kwargs["ConditionExpression"] = condition_expression

    try:
        response = _table.update_item(**kwargs)
        return response.get("Attributes", {})
    except ClientError as e:
        logger.error("DynamoDB update_item failed", extra={"error": str(e), "pk": pk, "sk": sk})
        raise


def query_gsi(
    index_name: str,
    pk_name: str,
    pk_value: str,
    sk_name: str | None = None,
    sk_value: str | None = None,
    sk_begins_with: str | None = None,
    limit: int | None = None,
    scan_forward: bool = True,
) -> list[dict[str, Any]]:
    """Query a GSI. Supports exact sk match or begins_with."""
    key_condition = "#pk = :pkval"
    expression_names: dict[str, str] = {"#pk": pk_name}
    expression_values: dict[str, Any] = {":pkval": pk_value}

    if sk_name and sk_value:
        key_condition += " AND #sk = :skval"
        expression_names["#sk"] = sk_name
        expression_values[":skval"] = sk_value
    elif sk_name and sk_begins_with:
        key_condition += " AND begins_with(#sk, :skprefix)"
        expression_names["#sk"] = sk_name
        expression_values[":skprefix"] = sk_begins_with

    kwargs: dict[str, Any] = {
        "IndexName": index_name,
        "KeyConditionExpression": key_condition,
        "ExpressionAttributeNames": expression_names,
        "ExpressionAttributeValues": expression_values,
        "ScanIndexForward": scan_forward,
    }
    if limit:
        kwargs["Limit"] = limit

    try:
        response = _table.query(**kwargs)
        return response.get("Items", [])
    except ClientError as e:
        logger.error("DynamoDB query_gsi failed", extra={"error": str(e), "index": index_name, "pk": pk_value})
        raise


def transact_write(items: list[dict[str, Any]]) -> None:
    """Execute a transactional write with multiple operations.

    Each item in the list should be a dict with one key:
    'Put', 'Update', or 'Delete', containing the operation params.
    """
    try:
        client = boto3.client("dynamodb")
        client.transact_write_items(TransactItems=items)
    except ClientError as e:
        logger.error("DynamoDB transact_write failed", extra={"error": str(e)})
        raise


def to_ddb(d: dict[str, Any]) -> dict[str, Any]:
    """Marshal a Python dict into DynamoDB AttributeValue JSON.

    Used for low-level transact_write_items calls (boto3 client API).
    """
    return {k: _attr(v) for k, v in d.items()}


def _attr(value: Any) -> dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float, Decimal)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, list):
        return {"L": [_attr(v) for v in value]}
    if isinstance(value, dict):
        return {"M": to_ddb(value)}
    return {"S": str(value)}


def delete_item(pk: str, sk: str) -> None:
    """Delete a single item by pk/sk."""
    try:
        _table.delete_item(Key={"pk": pk, "sk": sk})
    except ClientError as e:
        logger.error("DynamoDB delete_item failed", extra={"error": str(e), "pk": pk, "sk": sk})
        raise
