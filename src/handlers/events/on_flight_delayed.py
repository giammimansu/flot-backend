"""Flot — EventBridge handler for flight.delayed / flight.advanced (v4).

Re-evaluates time compatibility of the affected TentativeMatch.
If delay breaks compatibility → dissolves match + emits match.invalidated.
Silent if match is still compatible.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib import dynamo
from lib.airports import get_airport
from lib.eventbridge import put_event
from lib.matching import check_time_compatibility

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> None:
    detail = event.get("detail", {})
    trip_id = detail.get("tripId")
    match_id = detail.get("matchId")
    delta_min = detail.get("deltaMinutes", 0)

    if not match_id:
        logger.info("flight_delayed_no_match", tripId=trip_id)
        return

    tm = dynamo.get_item(f"TENTATIVE_MATCH#{match_id}", "META")
    if not tm or tm.get("status") != "tentative_match":
        logger.info("flight_delayed_match_not_tentative", matchId=match_id)
        return

    trip_a = dynamo.get_item(f"TRIP#{tm['tripId1']}", "META")
    trip_b = dynamo.get_item(f"TRIP#{tm['tripId2']}", "META")

    if not trip_a or not trip_b:
        logger.warning("flight_delayed_trips_not_found", matchId=match_id)
        return

    try:
        airport = get_airport(tm["airportCode"])
    except ValueError:
        logger.error("flight_delayed_unknown_airport", airportCode=tm.get("airportCode"))
        return

    compatible = check_time_compatibility(trip_a, trip_b, airport, mode="scheduled")

    if compatible:
        logger.info("match_still_valid_after_delay", matchId=match_id, deltaMin=round(delta_min, 1))
        return

    # Delay broke compatibility — dissolve TentativeMatch and return trips to pool
    dynamo.dissolve_tentative_match(match_id, trip_a, trip_b)

    put_event("match.invalidated", {
        "matchId": match_id,
        "tripId1": tm["tripId1"],
        "tripId2": tm["tripId2"],
        "userId1": tm["userId1"],
        "userId2": tm["userId2"],
        "reason": "flight_delay",
        "deltaMinutes": round(delta_min, 1),
    })

    logger.info(
        "match_invalidated_by_delay",
        matchId=match_id,
        deltaMin=round(delta_min, 1),
    )
