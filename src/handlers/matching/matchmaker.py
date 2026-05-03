"""Flot — background Matchmaker job.

Runs every 5 minutes to scan scheduled queues and resolve potential matches.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta
from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from lib import dynamo
from lib.airports import get_active_airports, AirportConfig
from lib.matching import (
    find_best_match,
    build_match_item,
    can_match_direction,
    compute_dynamic_threshold,
    compute_match_score,
    estimate_detour_minutes,
    apply_detour_penalty,
    haversine_km,
)
from lib.eventbridge import put_event

logger = Logger()
tracer = Tracer()
metrics = Metrics()

@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict, context) -> dict:
    """Scans all active airports for potential matches in scheduled queues."""
    airports = get_active_airports()
    matched_count = 0
    
    for airport in airports:
        matched_count += process_airport(airport)
        
    return {"processed": len(airports), "matched": matched_count}

def process_airport(airport: AirportConfig) -> int:
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=airport.scheduled_advance_days)
    window_start = now - timedelta(hours=2)

    # Query GSI5 for trips scheduled for this airport
    response = dynamo.query_gsi(
        index_name="GSI5-TripStatus",
        pk_name="gsi5pk",
        pk_value=f"{airport.code}#scheduled"
    )
    
    scheduled_trips = response
    logger.info("matchmaker_scan", airport=airport.code, trips=len(scheduled_trips))

    matched_ids = set()
    matches_created = 0

    for trip in scheduled_trips:
        trip_id = trip["tripId"]
        
        # Idempotency and processed checks
        if trip["pk"] in matched_ids or trip.get("status") == "matched":
            continue
            
        flight_dt = datetime.fromisoformat(trip.get("flightTime", trip["createdAt"]).replace("Z", "+00:00"))
        
        # Expire old trips
        if flight_dt + timedelta(hours=2) < now:
            expire_trip(trip)
            continue
            
        # Is within reasonable bound of scanning (e.g. up to advance window)?
        if not (window_start <= flight_dt <= window_end):
            continue

        c_user = dynamo.get_item(f"USER#{trip['userId']}", "PROFILE") or {}
        
        best_match = find_best_match(query_trip=trip, query_user=c_user)
        if best_match:
            candidate = best_match.candidate
            
            # They match!
            create_match(trip, candidate, best_match.score)
            
            matched_ids.add(trip["pk"])
            matched_ids.add(candidate["pk"])
            matches_created += 1

    return matches_created

def build_compatibility_matrix(
    pool: list[dict],
    airport: AirportConfig,
    now: datetime,
) -> list[tuple]:
    """
    Returns list[(tripId_a, tripId_b, score, dist_km, detour_min)] sorted by score desc.
    Applies dynamic threshold and detour filter per v4 spec.
    """
    pairs = []

    for i, trip_a in enumerate(pool):
        for j, trip_b in enumerate(pool):
            if j <= i:
                continue

            if not can_match_direction(trip_a, trip_b):
                continue

            if "destLat" not in trip_a or "destLat" not in trip_b:
                continue

            if not trip_a.get("flightTime") or not trip_b.get("flightTime"):
                continue

            dynamic_threshold = compute_dynamic_threshold(
                airport.match_threshold,
                trip_a["flightTime"],
                trip_b["flightTime"],
                now,
            )

            dist_km = haversine_km(
                trip_a["destLat"], trip_a["destLng"],
                trip_b["destLat"], trip_b["destLng"],
            )

            detour_min = estimate_detour_minutes(trip_a, trip_b, airport)

            if detour_min > airport.max_detour_minutes:
                continue

            user_a = dynamo.get_item(f"USER#{trip_a['userId']}", "PROFILE") or {}
            user_b = dynamo.get_item(f"USER#{trip_b['userId']}", "PROFILE") or {}
            score = compute_match_score(trip_a, trip_b, user_a, user_b, mode="scheduled")
            score = apply_detour_penalty(score, detour_min, airport.max_detour_minutes)

            if score >= dynamic_threshold:
                pairs.append((trip_a["tripId"], trip_b["tripId"], score, round(dist_km, 2), round(detour_min, 1)))

    pairs.sort(key=lambda x: x[2], reverse=True)
    return pairs


def find_optimal_assignments(pairs: list[tuple]) -> list[tuple]:
    """Greedy assignment by score. O(n²), sufficient for MVP pool sizes (<100 trips)."""
    assigned: set[str] = set()
    assignments = []

    for trip_a_id, trip_b_id, score, dist_km, detour_min in pairs:
        if trip_a_id in assigned or trip_b_id in assigned:
            continue
        assignments.append((trip_a_id, trip_b_id, score, dist_km, detour_min))
        assigned.add(trip_a_id)
        assigned.add(trip_b_id)

    return assignments


def expire_trip(trip: dict):
    trip["status"] = "expired"
    if "gsi5pk" in trip: del trip["gsi5pk"]
    if "gsi5sk" in trip: del trip["gsi5sk"]
    if "gsi1pk" in trip: del trip["gsi1pk"]
    if "gsi1sk" in trip: del trip["gsi1sk"]
    dynamo.put_item(trip)
    put_event("trip.expired", {"tripId": trip["tripId"], "airportCode": trip.get("airportCode")})

def create_match(trip_a: dict, trip_b: dict, score: float):
    # Lock them natively via DynamoDB transactions to avoid race conditions.
    match_item = build_match_item(trip_a, trip_b, score)
    
    # Update statuses
    trip_a["status"] = "matched"
    if "gsi5pk" in trip_a: del trip_a["gsi5pk"]
    if "gsi1pk" in trip_a: del trip_a["gsi1pk"]
    
    trip_b["status"] = "matched"
    if "gsi5pk" in trip_b: del trip_b["gsi5pk"]
    if "gsi1pk" in trip_b: del trip_b["gsi1pk"]

    dynamo.transact_write([
        {"Put": {"Item": dynamo.to_ddb(match_item), "TableName": os.environ["TABLE_NAME"]}},
        {"Put": {"Item": dynamo.to_ddb(trip_a), "TableName": os.environ["TABLE_NAME"]}},
        {"Put": {"Item": dynamo.to_ddb(trip_b), "TableName": os.environ["TABLE_NAME"]}},
    ])
    
    put_event("match.found", {
        "matchId": match_item["matchId"],
        "airportCode": trip_a["airportCode"],
        "userId1": trip_a["userId"],
        "userId2": trip_b["userId"]
    })
