"""Flot — GET /trips/my handler.

Returns active and recent trips for the current user.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer
from lib import dynamo
from lib.http import app_handler, json_response

logger = Logger()
tracer = Tracer()

@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]
    
    # Query GSI2: userId -> createdAt
    # We retrieve the recent trips of the user
    trips = dynamo.query_gsi(
        index_name="GSI2-UserTrips",
        pk_name="gsi2pk",
        pk_value=f"USER#{user_id}"
    )

    # Sort descending by createdAt
    trips.sort(key=lambda x: x.get("createdAt", ""), reverse=True)

    return json_response({"trips": trips}, origin)
