"""Flot — POST /matches/{matchId}/review (P2 #11).

Lets a user rate their partner after a completed shared trip.

Rules:
- Caller must be a member of the match.
- Match must be `completed` (the trip.completed handler set completedAt).
- Window: within 48h of completedAt.
- One review per reviewer per match (idempotent — conditional put).

Storage (Review entity):
    pk = USER#<reviewedUserId>   sk = REVIEW#<matchId>
    reviewerId, rating (1-5), comment?, airportCode, createdAt

The reviewed user's profile carries running aggregates `ratingSum` + `ratingCount`
(updated atomically in the same transaction); the average is computed on read.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from aws_lambda_powertools import Logger, Tracer
from botocore.exceptions import ClientError
from pydantic import ValidationError

from lib import dynamo
from lib.dynamo import get_match, now_iso
from lib.http import AppError, app_handler, created
from lib.validation import CreateReviewRequest

logger = Logger()
tracer = Tracer()

REVIEW_WINDOW_HOURS = int(os.environ.get("REVIEW_WINDOW_HOURS", "48"))


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    reviewer_id: str = event["_user_id"]
    origin: str | None = event["_origin"]
    body: dict = event["_body"]

    path_params = event.get("pathParameters") or {}
    match_id = path_params.get("matchId")
    if not match_id:
        raise AppError(400, "Missing matchId")

    try:
        req = CreateReviewRequest.model_validate(body)
    except ValidationError as e:
        raise AppError(400, "Invalid review payload", details={"errors": e.errors()}) from e

    match = get_match(match_id)
    if not match:
        raise AppError(404, "Match not found")
    if reviewer_id not in (match.get("userId1"), match.get("userId2")):
        raise AppError(403, "Forbidden")

    if match.get("status") != "completed":
        raise AppError(409, "Review available only after the trip is completed")

    completed_at = match.get("completedAt")
    if completed_at:
        completed_dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - completed_dt).total_seconds() / 3600
        if age_hours > REVIEW_WINDOW_HOURS:
            raise AppError(410, "Review window has expired")

    reviewed_id = match["userId2"] if reviewer_id == match["userId1"] else match["userId1"]
    created_at = now_iso()
    table_name = os.environ["TABLE_NAME"]

    review_item = {
        "pk": f"USER#{reviewed_id}",
        "sk": f"REVIEW#{match_id}",
        "matchId": match_id,
        "reviewerId": reviewer_id,
        "reviewedUserId": reviewed_id,
        "rating": req.rating,
        "comment": req.comment,
        "airportCode": match.get("airportCode"),
        "createdAt": created_at,
    }

    try:
        dynamo.transact_write([
            {"Put": {
                "Item": dynamo.to_ddb(review_item),
                "TableName": table_name,
                # Idempotency: one review per reviewer per match.
                "ConditionExpression": "attribute_not_exists(pk)",
            }},
            {"Update": {
                "Key": dynamo.to_ddb({"pk": f"USER#{reviewed_id}", "sk": "PROFILE"}),
                "TableName": table_name,
                "UpdateExpression": "ADD ratingSum :r, ratingCount :one",
                "ExpressionAttributeValues": {
                    ":r": {"N": str(req.rating)},
                    ":one": {"N": "1"},
                },
            }},
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            raise AppError(409, "You already reviewed this match") from e
        raise

    logger.info("review_created", matchId=match_id, reviewedUserId=reviewed_id, rating=req.rating)
    return created({
        "matchId": match_id,
        "reviewedUserId": reviewed_id,
        "rating": req.rating,
    }, origin)
