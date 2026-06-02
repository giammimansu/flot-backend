"""Flot Admin — POST /admin/flights/{tripId}/refresh-eta

Forces an immediate ETA re-fetch from the flight tracker for a trip.
Used when a flight's ETA is stale or the tracker was degraded at creation time.

Auth: IAM.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aws_lambda_powertools import Logger, Tracer

from lib.dynamo import get_trip, update_item
from lib.flight_tracker import fetch_flight_eta
from lib.http import AppError, app_handler, success
from lib.matching import get_time_bucket

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=False)
def handler(event: dict, context) -> dict:
    origin = event.get("_origin")
    path_params = event.get("pathParameters") or {}
    trip_id = path_params.get("tripId")
    if not trip_id:
        raise AppError(400, "Missing tripId")

    trip = get_trip(trip_id)
    if not trip:
        raise AppError(404, "Trip not found")

    flight_number = trip.get("flightNumber")
    flight_date = trip.get("flightDate")
    if not flight_number or not flight_date:
        raise AppError(400, "Trip has no flightNumber/flightDate — cannot refresh ETA")

    now = datetime.now(timezone.utc)
    eta = fetch_flight_eta(flight_number, flight_date)

    if eta is None:
        # Degraded — mark as such, keep existing flightTime
        update_item(trip["pk"], "META", {"trackingStatus": "degraded"})
        return success({"tripId": trip_id, "status": "degraded", "eta": None}, origin)

    new_flight_time = eta.isoformat().replace("+00:00", "Z")
    new_bucket = get_time_bucket(new_flight_time)
    airport_code = trip.get("airportCode", "")

    update_item(trip["pk"], "META", {
        "flightTime": new_flight_time,
        "timeBucket": new_bucket,
        "gsi1pk": f"{airport_code}#{new_bucket}",
        "trackingStatus": "live",
        "flightEtaUpdatedAt": now.isoformat().replace("+00:00", "Z"),
    })

    logger.info("admin_refresh_eta", tripId=trip_id, eta=new_flight_time)
    return success({"tripId": trip_id, "status": "live", "eta": new_flight_time}, origin)
