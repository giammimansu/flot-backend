"""Flot — EventBridge handler for trip.expired.

Fires when a scheduled trip expires without ever being matched.
Emits the TripExpiredNoMatch business metric (cold-start indicator)
and notifies the user.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib.dynamo import get_trip, get_user
from lib.i18n import tr, user_lang
from lib.metrics import business_metrics
from lib.notifications import deliver

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> None:
    detail = event.get("detail", {})
    trip_id = detail.get("tripId")
    airport_code = detail.get("airportCode", "UNKNOWN")

    # Emit business metric — TripExpiredNoMatch is a cold-start indicator
    business_metrics.record_trip_expired_no_match(airport_code)

    if not trip_id:
        logger.warning("trip_expired_missing_trip_id")
        return

    trip = get_trip(trip_id)
    if not trip:
        logger.warning("trip_expired_trip_not_found", tripId=trip_id)
        return

    user_id = trip.get("userId")
    if not user_id:
        return

    lang = user_lang(get_user(user_id))
    deliver(
        user_id,
        tr("trip_expired.title", lang),
        tr("trip_expired.body", lang),
        {"type": "trip_expired", "tripId": trip_id, "airportCode": airport_code},
    )

    logger.info("trip_expired_notified", tripId=trip_id, airportCode=airport_code, userId=user_id)
