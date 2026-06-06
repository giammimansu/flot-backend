"""Flot — POST /trips handler.

Creates a new trip, attempts immediate match against pending trips at the
same airport. If a match is found, persists Match record + emits match.found.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from aws_lambda_powertools import Logger, Tracer
from pydantic import ValidationError

from lib import dynamo
from lib.airports import get_airport
from lib.eventbridge import put_event
from lib.flight_tracker import FlightTrackerError, fetch_flight_eta
from lib.http import AppError, app_handler, created
from lib.matching import get_time_bucket
from lib.trust import is_banned
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

    # P2 #10 — banned users cannot create trips.
    if is_banned(user_item):
        raise AppError(403, "Account sospeso per violazioni ripetute. Contatta il supporto.")

    trip_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat().replace("+00:00", "Z")

    destZone = req.destZone or coords_to_zone(airport.code, req.destLat, req.destLng)

    # v4 — resolve flightTime; prefer pre-resolved value from UI lookup (avoids extra API call)
    tracking_pending = False
    if req.flightTime:
        resolved_flight_time = req.flightTime
        flight_dt = datetime.fromisoformat(resolved_flight_time.replace("Z", "+00:00"))
        logger.info("flight_time_from_client", flightNumber=req.flightNumber, flightTime=resolved_flight_time)
    else:
        try:
            eta = fetch_flight_eta(req.flightNumber, req.flightDate)
        except FlightTrackerError as exc:
            logger.warning("flight_tracker_unavailable", flightNumber=req.flightNumber, reason=str(exc))
            eta = None

        if eta is not None:
            flight_dt = eta
            resolved_flight_time = flight_dt.isoformat().replace("+00:00", "Z")
            logger.info("flight_eta_resolved", flightNumber=req.flightNumber, eta=resolved_flight_time)
        else:
            # Degraded: fall back to static noon UTC on flight date
            resolved_flight_time = f"{req.flightDate}T12:00:00Z"
            flight_dt = datetime.fromisoformat(resolved_flight_time.replace("Z", "+00:00"))
            tracking_pending = True

    if req.mode == TripMode.LIVE:
        expires_at = now + timedelta(seconds=airport.search_timeout_sec)
    else:
        expires_at = flight_dt + timedelta(hours=2)

    slot_bucket = get_time_bucket(resolved_flight_time)
    gsi1pk = f"{airport.code}#{slot_bucket}"

    if req.mode == TripMode.SCHEDULED:
        trip_status = "tracking_pending" if tracking_pending else "scheduled"
    else:
        trip_status = "searching"

    trip_item = {
        "pk": f"TRIP#{trip_id}",
        "sk": "META",
        "tripId": trip_id,
        "userId": user_id,
        "airportCode": airport.code,
        "terminal": req.terminal,
        "direction": req.direction,
        "destination": req.destination,
        "destLat": Decimal(str(req.destLat)),
        "destLng": Decimal(str(req.destLng)),
        "destPlaceId": req.destPlaceId,
        "destZone": destZone,
        "mode": req.mode.value,
        "flightNumber": req.flightNumber,
        "flightDate": req.flightDate,
        "flightTime": resolved_flight_time,
        "flightEtaUpdatedAt": now_iso,
        "timeBucket": slot_bucket,
        "luggage": req.luggage,
        "paxCount": req.paxCount,
        "status": trip_status,
        "tentativeMatchId": None,
        "lang": user_item.get("lang"),
        "verified": user_item.get("verified", False),
        "createdAt": now_iso,
        "expiresAt": int(expires_at.timestamp()),
        "gsi1pk": gsi1pk,
        "gsi1sk": now_iso,
        "gsi2pk": f"USER#{user_id}",
        "gsi2sk": now_iso,
        "gsi5pk": f"{airport.code}#{trip_status}",
        "gsi5sk": resolved_flight_time,
    }

    dynamo.put_item(trip_item)
    put_event("trip.created", {"tripId": trip_id, "mode": req.mode.value})

    return created(
        {
            "tripId": trip_id,
            "status": trip_status,
            "airportCode": airport.code,
            "terminal": req.terminal,
            "direction": req.direction,
            "destination": req.destination,
            "destLat": req.destLat,
            "destLng": req.destLng,
            "destPlaceId": req.destPlaceId,
            "destZone": destZone,
            "mode": req.mode.value,
            "flightNumber": req.flightNumber,
            "flightDate": req.flightDate,
            "flightTime": resolved_flight_time,
            "createdAt": trip_item["createdAt"],
        },
        origin,
    )


