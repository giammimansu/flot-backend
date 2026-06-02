"""Flot — DynamoDB client and helpers.

Module-level client is initialized once and reused across warm Lambda invocations.
All DynamoDB operations go through this module for consistent error handling.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any
from decimal import Decimal

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

logger = Logger(child=True)

# ── Module-level client (reused across invocations) ──────────────────
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(os.environ.get("TABLE_NAME", "Flot"))
table = _table  # public alias for handlers that need raw Table access


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
    sk_lte: str | None = None,
    limit: int | None = None,
    scan_forward: bool = True,
) -> list[dict[str, Any]]:
    """Query a GSI. Supports exact sk match, begins_with, or sk <= value."""
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
    elif sk_name and sk_lte:
        key_condition += " AND #sk <= :sklte"
        expression_names["#sk"] = sk_name
        expression_values[":sklte"] = sk_lte

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


def get_match(match_id: str) -> dict[str, Any] | None:
    return get_item(f"MATCH#{match_id}", "META")


def get_trip(trip_id: str) -> dict[str, Any] | None:
    return get_item(f"TRIP#{trip_id}", "META")


def get_user(user_id: str) -> dict[str, Any] | None:
    return get_item(f"USER#{user_id}", "PROFILE")


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def save_notification(user_id: str, payload: dict[str, Any]) -> None:
    import uuid
    _table.put_item(Item={
        "pk": f"USER#{user_id}",
        "sk": f"NOTIF#{uuid.uuid4()}",
        "userId": user_id,
        "read": False,
        "createdAt": now_iso(),
        **payload,
    })


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


# ── v4 — TentativeMatch helpers ───────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_tentative_match(
    trip_a: dict[str, Any],
    trip_b: dict[str, Any],
    score: float,
    dist_km: float,
    detour_min: float,
    lock_at: datetime,
    airport_code: str,
) -> dict[str, Any]:
    """
    Persists a TentativeMatch and updates both trips' status + tentativeMatchId atomically.
    Emits no notifications — TentativeMatch is silent per v4 spec.
    """
    match_id = str(uuid.uuid4())
    now = _now_iso()
    lock_at_iso = lock_at.isoformat().replace("+00:00", "Z")

    item: dict[str, Any] = {
        "pk": f"TENTATIVE_MATCH#{match_id}",
        "sk": "META",
        "matchId": match_id,
        "tripId1": trip_a["tripId"],
        "tripId2": trip_b["tripId"],
        "userId1": trip_a["userId"],
        "userId2": trip_b["userId"],
        "airportCode": airport_code,
        "score": round(score, 2),
        "distKm": round(dist_km, 2),
        "detourMinutes": round(detour_min, 2),
        "status": "tentative_match",
        "lockAt": lock_at_iso,
        "gsi6pk": f"{airport_code}#tentative",
        "createdAt": now,
    }

    updated_a = {**trip_a, "status": "tentative_match", "tentativeMatchId": match_id, "gsi5pk": f"{airport_code}#tentative_match"}
    updated_b = {**trip_b, "status": "tentative_match", "tentativeMatchId": match_id, "gsi5pk": f"{airport_code}#tentative_match"}

    transact_write([
        {"Put": {"Item": to_ddb(item), "TableName": os.environ["TABLE_NAME"]}},
        {"Put": {
            "Item": to_ddb(updated_a),
            "TableName": os.environ["TABLE_NAME"],
            # Guard: trip must still be scheduled — prevents double-assignment when two
            # concurrent optimize_pool runs both see the same trip and race to claim it.
            "ConditionExpression": "#s = :scheduled",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":scheduled": {"S": "scheduled"}},
        }},
        {"Put": {
            "Item": to_ddb(updated_b),
            "TableName": os.environ["TABLE_NAME"],
            "ConditionExpression": "#s = :scheduled",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":scheduled": {"S": "scheduled"}},
        }},
    ])

    logger.info(
        "tentative_match_created",
        matchId=match_id,
        tripId1=trip_a["tripId"],
        tripId2=trip_b["tripId"],
        score=round(score, 2),
        distKm=round(dist_km, 2),
        detourMin=round(detour_min, 2),
        lockAt=lock_at_iso,
    )
    return item


def dissolve_tentative_match(
    match_id: str,
    trip_a: dict[str, Any],
    trip_b: dict[str, Any],
) -> None:
    """
    Deletes TentativeMatch and returns both trips to 'scheduled' status atomically.
    Used when a better match is found or a flight delay breaks compatibility.
    """
    airport_code = trip_a.get("airportCode", "")
    reset_a = {**trip_a, "status": "scheduled", "tentativeMatchId": None, "gsi5pk": f"{airport_code}#scheduled"}
    reset_b = {**trip_b, "status": "scheduled", "tentativeMatchId": None, "gsi5pk": f"{airport_code}#scheduled"}

    transact_write([
        {"Delete": {
            "Key": to_ddb({"pk": f"TENTATIVE_MATCH#{match_id}", "sk": "META"}),
            "TableName": os.environ["TABLE_NAME"],
        }},
        {"Put": {"Item": to_ddb(reset_a), "TableName": os.environ["TABLE_NAME"]}},
        {"Put": {"Item": to_ddb(reset_b), "TableName": os.environ["TABLE_NAME"]}},
    ])

    logger.info("tentative_match_dissolved", matchId=match_id)


def query_tentative_matches_to_lock(airport_code: str, now: datetime) -> list[dict[str, Any]]:
    """
    Returns all TentativeMatch items for the airport whose lockAt <= now.
    Uses GSI6 sorted by lockAt — efficient O(k) where k = matches to lock.
    """
    now_iso = now.isoformat().replace("+00:00", "Z")
    return query_gsi(
        index_name="GSI6-TentativeMatch",
        pk_name="gsi6pk",
        pk_value=f"{airport_code}#tentative",
        sk_name="lockAt",
        sk_lte=now_iso,
    )


def get_tentative_match_between(trip_id_a: str, trip_id_b: str) -> dict[str, Any] | None:
    """
    Returns the active TentativeMatch linking trip_a and trip_b, or None.
    Reads tentativeMatchId from either trip to avoid a scan.
    """
    trip_a = get_item(f"TRIP#{trip_id_a}", "META")
    if not trip_a:
        return None
    match_id = trip_a.get("tentativeMatchId")
    if not match_id:
        return None
    tm = get_item(f"TENTATIVE_MATCH#{match_id}", "META")
    if not tm:
        return None
    # Verify it actually links both trips
    if {tm.get("tripId1"), tm.get("tripId2")} == {trip_id_a, trip_id_b}:
        return tm
    return None
