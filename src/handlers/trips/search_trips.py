"""Flot — GET /trips/search handler.

Manual re-trigger of matching for an existing 'searching' trip.
Useful if user opened the app after the initial create attempt found nothing.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib import dynamo
from lib.airports import get_airport
from lib.eventbridge import put_event
from lib.http import AppError, app_handler, success
from lib.matching import build_match_item, compute_pickup_point, compute_pickup_time, find_best_match

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]

    qs = event.get("queryStringParameters") or {}
    trip_id = qs.get("tripId")
    if not trip_id:
        raise AppError(400, "Missing tripId query parameter")

    trip = dynamo.get_item(f"TRIP#{trip_id}", "META")
    if not trip:
        raise AppError(404, "Trip not found")
    if trip.get("userId") != user_id:
        raise AppError(403, "Forbidden")
    if trip.get("status") != "searching":
        return success({"match": None, "status": trip.get("status")}, origin)

    user_item = dynamo.get_item(f"USER#{user_id}", "PROFILE") or {}
    result = find_best_match(trip, user_item)
    if not result:
        return success({"match": None, "status": "searching"}, origin)

    candidate = result.candidate
    airport = get_airport(trip["airportCode"])
    pickup_point = compute_pickup_point(trip, candidate, airport)
    pickup_time = compute_pickup_time(trip, candidate, airport)
    match_item = build_match_item(trip, candidate, result.score, pickup_point=pickup_point, pickup_time=pickup_time)

    table = dynamo.get_table().name
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

    return success(
        {
            "match": {
                "matchId": match_item["matchId"],
                "score": match_item["score"],
                "status": match_item["status"],
            },
            "status": "matched",
        },
        origin,
    )


