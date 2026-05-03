"""Flot — POST /trips handler.

Creates a new trip, attempts immediate match against pending trips at the
same airport. If a match is found, persists Match record + emits match.found.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

from aws_lambda_powertools import Logger, Tracer
from pydantic import ValidationError

from lib import dynamo
from lib.airports import get_airport
from lib.eventbridge import put_event
from lib.http import AppError, app_handler, created
from lib.matching import get_time_bucket
from lib.validation import CreateTripRequest, TripMode
from lib.zones import coords_to_zone, is_valid_direction, is_valid_terminal, is_valid_zone

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    """POST /trips — create trip, run match attempt, return trip + match (if any)."""
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]
    body: dict = event["_body"]

    try:
        req = CreateTripRequest.model_validate(body)
    except ValidationError as e:
        raise AppError(400, "Invalid trip payload", details={"errors": e.errors()}) from e

    # Validate airport + child resources
    try:
        airport = get_airport(req.airportCode)
    except ValueError as e:
        raise AppError(400, str(e)) from e

    if not is_valid_terminal(airport.code, req.terminal):
        raise AppError(400, f"Terminal {req.terminal} not valid for {airport.code}")
    if req.destZone and not is_valid_zone(airport.code, req.destZone):
        raise AppError(400, f"Zone {req.destZone} not valid for {airport.code}")
    if not is_valid_direction(airport.code, req.direction):
        raise AppError(400, f"Direction {req.direction} not valid for {airport.code}")

    # Load user (for matching profile bonuses)
    user_item = dynamo.get_item(f"USER#{user_id}", "PROFILE") or {}

    trip_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat().replace("+00:00", "Z")

    destZone = req.destZone or coords_to_zone(airport.code, req.destLat, req.destLng)

    flight_dt = datetime.fromisoformat(req.flightTime.replace("Z", "+00:00"))
    
    if req.mode == TripMode.LIVE:
        expires_at = now + timedelta(seconds=airport.search_timeout_sec)
        slot_bucket = get_time_bucket(req.flightTime)
        gsi1pk = f"{airport.code}#{slot_bucket}"
        event_name = "trip.created"
    else:
        expires_at = flight_dt + timedelta(hours=2)
        slot_bucket = get_time_bucket(req.flightTime)
        gsi1pk = f"{airport.code}#{slot_bucket}"
        event_name = "trip.created"

    trip_item = {
        "pk": f"TRIP#{trip_id}",
        "sk": "META",
        "tripId": trip_id,
        "userId": user_id,
        "airportCode": airport.code,
        "terminal": req.terminal,
        "direction": req.direction,
        "destination": req.destination,
        "destLat": float(req.destLat),
        "destLng": float(req.destLng),
        "destPlaceId": req.destPlaceId,
        "destZone": destZone,
        "mode": req.mode.value,
        "flightTime": req.flightTime,
        "timeBucket": slot_bucket,
        "luggage": req.luggage,
        "paxCount": req.paxCount,
        "status": "scheduled" if req.mode == TripMode.SCHEDULED else "searching",
        "lang": user_item.get("lang"),
        "verified": user_item.get("verified", False),
        "createdAt": now_iso,
        "expiresAt": int(expires_at.timestamp()),
        "gsi1pk": gsi1pk,
        "gsi1sk": now_iso,
        "gsi2pk": f"USER#{user_id}",
        "gsi2sk": now_iso,
        "gsi5pk": f"{airport.code}#{'scheduled' if req.mode == TripMode.SCHEDULED else 'searching'}",
        "gsi5sk": req.flightTime,
    }

    dynamo.put_item(trip_item)
    
    event_payload = {"tripId": trip_id, "mode": req.mode.value}
    put_event(event_name, event_payload)

    # Attempt immediate match
    match_payload: dict | None = None
    return created(
        {
            "tripId": trip_id,
            "status": "scheduled" if req.mode == TripMode.SCHEDULED else "searching",
            "airportCode": airport.code,
            "terminal": req.terminal,
            "direction": req.direction,
            "destination": req.destination,
            "destLat": req.destLat,
            "destLng": req.destLng,
            "destPlaceId": req.destPlaceId,
            "destZone": destZone,
            "mode": req.mode.value,
            "flightTime": req.flightTime,
            "createdAt": trip_item["createdAt"]
        },
        origin,
    )


