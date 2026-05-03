"""Flot — FlightTrackerFunction (v4).

Polls flight ETAs every 15 minutes for all trips in the 12-hour tracking window.
Updates flightTime + GSI keys when delta > 10 min.
Emits flight.delayed when a trip in tentative_match is affected.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from lib import dynamo
from lib.airports import get_active_airports
from lib.eventbridge import put_event
from lib.flight_tracker import fetch_flight_eta, FlightTrackerError
from lib.matching import get_time_bucket

logger = Logger()
tracer = Tracer()
metrics = Metrics()

TRACKING_WINDOW_HOURS = int(os.environ.get("TRACKING_WINDOW_HOURS", "12"))
MIN_DELTA_MIN = 10


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict, context) -> dict:
    now = datetime.now(timezone.utc)
    airports = get_active_airports()
    updated_count = 0

    for airport in airports:
        updated_count += _process_airport(airport.code, now)

    return {"tracked": updated_count}


def _process_airport(airport_code: str, now: datetime) -> int:
    window_end = now + timedelta(hours=TRACKING_WINDOW_HOURS)
    window_end_iso = window_end.isoformat().replace("+00:00", "Z")
    now_iso = now.isoformat().replace("+00:00", "Z")

    # Query both status buckets in the tracking window
    trips = []
    for status in ("scheduled", "tentative_match"):
        batch = dynamo.query_gsi(
            index_name="GSI5-TripStatus",
            pk_name="gsi5pk",
            pk_value=f"{airport_code}#{status}",
        )
        # Filter to tracking window: now <= flightTime <= now+12h
        trips += [
            t for t in batch
            if t.get("flightTime") and now_iso <= t["flightTime"] <= window_end_iso
        ]

    updated = 0
    for trip in trips:
        if _update_flight_eta(trip, now):
            updated += 1

    logger.info("flight_tracker_done", airport=airport_code, checked=len(trips), updated=updated)
    return updated


def _update_flight_eta(trip: dict, now: datetime) -> bool:
    flight_number = trip.get("flightNumber")
    flight_date = trip.get("flightDate")
    if not flight_number or not flight_date:
        return False

    try:
        eta = fetch_flight_eta(flight_number, flight_date)
    except FlightTrackerError as exc:
        logger.warning("flight_tracker_unavailable", tripId=trip["pk"], reason=str(exc))
        return False

    current_dt = datetime.fromisoformat(trip["flightTime"].replace("Z", "+00:00"))
    delta_min = abs((eta - current_dt).total_seconds()) / 60

    if delta_min < MIN_DELTA_MIN:
        return False

    new_flight_time = eta.isoformat().replace("+00:00", "Z")
    new_bucket = get_time_bucket(new_flight_time)
    airport_code = trip["airportCode"]

    dynamo.update_item(
        trip["pk"], "META",
        {
            "flightTime": new_flight_time,
            "timeBucket": new_bucket,
            "gsi1pk": f"{airport_code}#{new_bucket}",
            "flightEtaUpdatedAt": now.isoformat().replace("+00:00", "Z"),
        },
    )

    logger.info(
        "flight_eta_updated",
        tripId=trip["pk"],
        delta_min=round(delta_min, 1),
        newEta=new_flight_time,
    )
    metrics.add_metric(name="FlightEtaUpdates", unit=MetricUnit.Count, value=1)

    if trip.get("status") == "tentative_match" and trip.get("tentativeMatchId"):
        put_event("flight.delayed", {
            "tripId": trip["tripId"],
            "matchId": trip["tentativeMatchId"],
            "oldFlightTime": trip["flightTime"],
            "newFlightTime": new_flight_time,
            "deltaMinutes": round(delta_min, 1),
        })

    return True
