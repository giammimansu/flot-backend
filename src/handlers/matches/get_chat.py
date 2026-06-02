"""Flot — GET /matches/{matchId}/chat handler.

Returns paginated chat history for an unlocked match.
Only the two matched users can read the chat.

Query params:
  limit     — int, 1-100, default 50
  nextToken — base64-encoded LastEvaluatedKey for pagination
"""
from __future__ import annotations

import base64
import json

from aws_lambda_powertools import Logger, Tracer

from lib.dynamo import get_chat_messages, get_match
from lib.http import AppError, app_handler, success

logger = Logger()
tracer = Tracer()


def _encode_cursor(key: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(key, default=str).encode()).decode()


def _decode_cursor(token: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(token.encode()))
    except Exception as e:
        raise AppError(400, "Invalid nextToken") from e


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

    qs = event.get("queryStringParameters") or {}
    try:
        limit = max(1, min(100, int(qs.get("limit", 50))))
    except (ValueError, TypeError):
        limit = 50

    next_token = qs.get("nextToken")
    exclusive_start_key = _decode_cursor(next_token) if next_token else None

    match = get_match(match_id)
    if not match:
        raise AppError(404, "Match not found")
    if user_id not in (match.get("userId1"), match.get("userId2")):
        raise AppError(403, "Forbidden")
    if match.get("status") not in ("unlocked", "completed"):
        raise AppError(400, "Chat not available for this match status")

    messages, last_key = get_chat_messages(match_id, limit=limit, exclusive_start_key=exclusive_start_key)

    response: dict = {"messages": messages}
    if last_key:
        response["nextToken"] = _encode_cursor(last_key)

    return success(response, origin)
