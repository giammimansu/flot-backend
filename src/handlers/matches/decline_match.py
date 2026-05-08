"""Flot — POST /matches/{matchId}/decline handler."""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib.dynamo import get_item
from lib.eventbridge import put_event
from lib.http import AppError, app_handler, success

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]

    path_params = event.get("pathParameters") or {}
    match_id = path_params.get("matchId")
    if not match_id:
        raise AppError(400, "Missing matchId")

    match = get_item(f"MATCH#{match_id}", "META")
    if not match:
        raise AppError(404, "Match not found")
    if user_id not in (match.get("userId1"), match.get("userId2")):
        raise AppError(403, "Forbidden")
    if match.get("status") != "pending":
        raise AppError(400, f"Cannot decline a match with status '{match.get('status')}'")

    put_event("match.dissolved", {
        "matchId": match_id,
        "reason": "user_declined",
        "declinedByUserId": user_id,
    })

    logger.info("match_declined", matchId=match_id, userId=user_id)
    return success({"matchId": match_id, "status": "dissolved"}, origin)
