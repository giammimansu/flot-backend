"""Flot — EventBridge handler for trip.created.live and trip.created.scheduled

Triggered asynchronously when a trip is created.
Executes the match attempt. If successful, writes Match and updates status.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from aws_lambda_powertools import Logger, Tracer

from lib import dynamo
from lib.airports import get_airport
from lib.eventbridge import put_event
from lib.matching import build_match_item, find_best_match

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> None:
    """Handle trip.created.* events."""
    detail = event.get("detail", {})
    trip_id = detail.get("tripId")
    if not trip_id:
        logger.warning("Missing tripId in event detail")
        return

    trip = dynamo.get_item(f"TRIP#{trip_id}", "META")
    if not trip:
        logger.warning("Trip not found", extra={"tripId": trip_id})
        return

    if trip.get("status") != "searching":
        logger.info("Trip no longer searching", extra={"tripId": trip_id, "status": trip.get("status")})
        return

    user_id = trip["userId"]
    user_item = dynamo.get_item(f"USER#{user_id}", "PROFILE") or {}

    result = find_best_match(trip, user_item)
    if not result:
        logger.info("No match found", extra={"tripId": trip_id})
        return

    candidate = result.candidate
    match_item = build_match_item(trip, candidate, result.score)

    table = dynamo.get_table().name
    try:
        dynamo.transact_write(
            [
                {"Put": {"TableName": table, "Item": dynamo.to_ddb(match_item)}},
                {
                    "Update": {
                        "TableName": table,
                        "Key": dynamo.to_ddb({"pk": trip["pk"], "sk": "META"}),
                        "UpdateExpression": "SET #s = :matched, matchId = :mid",
                        "ConditionExpression": "#s = :searching",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": dynamo.to_ddb(
                            {":matched": "matched", ":searching": "searching", ":mid": match_item["matchId"]}
                        ),
                    }
                },
                {
                    "Update": {
                        "TableName": table,
                        "Key": dynamo.to_ddb({"pk": f"TRIP#{candidate['tripId']}", "sk": "META"}),
                        "UpdateExpression": "SET #s = :matched, matchId = :mid",
                        "ConditionExpression": "#s = :searching",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": dynamo.to_ddb(
                            {":matched": "matched", ":searching": "searching", ":mid": match_item["matchId"]}
                        ),
                    }
                },
            ]
        )
    except Exception as e:
        logger.error("Transaction failed (likely conflict)", extra={"error": str(e)})
        return

    logger.info("Match created", extra={"matchId": match_item["matchId"]})

    put_event(
        "match.found",
        {
            "matchId": match_item["matchId"],
            "airportCode": match_item["airportCode"],
            "userId1": match_item["userId1"],
            "userId2": match_item["userId2"],
            "tripId1": match_item["tripId1"],
            "tripId2": match_item["tripId2"],
            "score": match_item["score"],
        },
    )
