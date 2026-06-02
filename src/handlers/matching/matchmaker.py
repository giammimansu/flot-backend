"""Flot — background Matchmaker job (v4 — Shadow Pool).

Runs every 5 minutes:
  1. process_lock_window  — promote TentativeMatches past T-3h to definitive
  2. optimize_pool        — rematch all scheduled/tentative trips globally
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import ClientError

from lib import dynamo
from lib.airports import get_active_airports, AirportConfig
from lib.metrics import business_metrics
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

LOCK_HOURS_BEFORE = int(os.environ.get("LOCK_HOURS_BEFORE", "3"))


# ── Handler ───────────────────────────────────────────────────────────

@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict, context) -> dict:
    now = datetime.now(timezone.utc)
    airports = get_active_airports()
    locked_count = 0
    tentative_count = 0

    for airport in airports:
        locked, tentative = process_airport_v4(airport, now)
        locked_count += locked
        tentative_count += tentative

    return {"processed": len(airports), "locked": locked_count, "tentative": tentative_count}


def process_airport_v4(airport: AirportConfig, now: datetime) -> tuple[int, int]:
    expire_stale_trips(airport.code, now)
    locked = process_lock_window(airport, now)
    tentative = optimize_pool(airport, now)
    return locked, tentative


def expire_stale_trips(airport_code: str, now: datetime) -> None:
    """Expire scheduled/tentative_match trips whose flightTime <= now with no match."""
    now_iso = now.isoformat().replace("+00:00", "Z")
    expired_count = 0

    for status in ("scheduled", "tentative_match"):
        trips = dynamo.query_gsi(
            index_name="GSI5-TripStatus",
            pk_name="gsi5pk",
            pk_value=f"{airport_code}#{status}",
        )
        for trip in trips:
            if trip.get("flightTime", "") <= now_iso:
                tm_id = trip.get("tentativeMatchId")
                if tm_id:
                    tm = dynamo.get_item(f"TENTATIVE_MATCH#{tm_id}", "META")
                    if tm:
                        other_id = tm["tripId2"] if tm["tripId1"] == trip["tripId"] else tm["tripId1"]
                        other_trip = dynamo.get_item(f"TRIP#{other_id}", "META")
                        if other_trip:
                            dynamo.dissolve_tentative_match(tm_id, trip, other_trip)
                            put_event("match.tentative_dissolved", {
                                "matchId": tm_id,
                                "tripId1": trip["tripId"],
                                "tripId2": other_id,
                                "reason": "trip_expired",
                            })
                            # Re-fetch trip after dissolve (status was reset to scheduled)
                            trip = dynamo.get_item(trip["pk"], "META") or trip
                expire_trip(trip)
                expired_count += 1
                metrics.add_metric(name="TripsExpired", unit=MetricUnit.Count, value=1)

    if expired_count:
        logger.info("stale_trips_expired", airport=airport_code, count=expired_count)


# ── Step 1: Lock window ───────────────────────────────────────────────

def process_lock_window(airport: AirportConfig, now: datetime) -> int:
    """Promote TentativeMatches whose lockAt <= now to definitive matches."""
    ready = dynamo.query_tentative_matches_to_lock(airport.code, now)
    locked = 0

    for tm in ready:
        trip_a = dynamo.get_item(f"TRIP#{tm['tripId1']}", "META")
        trip_b = dynamo.get_item(f"TRIP#{tm['tripId2']}", "META")

        if not trip_a or not trip_b:
            continue
        if trip_a.get("status") != "tentative_match" or trip_b.get("status") != "tentative_match":
            # One trip was re-paired or cancelled — skip, let dissolve handle it
            continue

        promoted = _promote_tentative_to_match(tm, trip_a, trip_b, airport)
        if promoted:
            locked += 1
            metrics.add_metric(name="MatchesLocked", unit=MetricUnit.Count, value=1)

    return locked


def _promote_tentative_to_match(
    tm: dict,
    trip_a: dict,
    trip_b: dict,
    airport: AirportConfig,
) -> bool:
    """
    Atomically converts TentativeMatch → definitive Match.
    Condition expression on both trips ensures idempotency under concurrent runs.
    Returns True if match was created, False if skipped (already processed).
    """
    match_item = build_match_item(trip_a, trip_b, float(tm.get("score", 0)))
    table_name = os.environ["TABLE_NAME"]

    # Keep gsi5pk = "{airport}#matched" so dissolve/expire checker can find these
    # via GSI5 (gsi5sk = flightTime). Drop gsi1pk to remove from the active pool index.
    locked_a = {k: v for k, v in trip_a.items() if k not in ("gsi1pk",)}
    locked_a.update({
        "status": "matched",
        "tentativeMatchId": None,
        "matchId": match_item["matchId"],
        "gsi5pk": f"{airport.code}#matched",
    })

    locked_b = {k: v for k, v in trip_b.items() if k not in ("gsi1pk",)}
    locked_b.update({
        "status": "matched",
        "tentativeMatchId": None,
        "matchId": match_item["matchId"],
        "gsi5pk": f"{airport.code}#matched",
    })

    try:
        dynamo.transact_write([
            {"Put": {"Item": dynamo.to_ddb(match_item), "TableName": table_name}},
            {
                "Put": {
                    "Item": dynamo.to_ddb(locked_a),
                    "TableName": table_name,
                    "ConditionExpression": "#s = :expected",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {":expected": {"S": "tentative_match"}},
                }
            },
            {
                "Put": {
                    "Item": dynamo.to_ddb(locked_b),
                    "TableName": table_name,
                    "ConditionExpression": "#s = :expected",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {":expected": {"S": "tentative_match"}},
                }
            },
            {
                "Delete": {
                    "Key": dynamo.to_ddb({"pk": tm["pk"], "sk": "META"}),
                    "TableName": table_name,
                }
            },
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            logger.warning(
                "match_lock_skipped_idempotent",
                tmId=tm.get("matchId"),
                tripId1=tm.get("tripId1"),
                tripId2=tm.get("tripId2"),
            )
            return False
        raise

    put_event("match.found", {
        "matchId": match_item["matchId"],
        "airportCode": airport.code,
        "userId1": trip_a["userId"],
        "userId2": trip_b["userId"],
        "score": round(float(tm.get("score", 0)), 2),
        "savings": airport.base_fare // 2 / 100,
    })
    logger.info(
        "match_locked",
        matchId=match_item["matchId"],
        score=round(float(tm.get("score", 0)), 2),
    )

    # Business metrics: trip_matched + latency for both trips
    for trip in (trip_a, trip_b):
        business_metrics.record_trip_matched(airport.code)
        created_at = trip.get("createdAt")
        if created_at:
            try:
                from datetime import datetime, timezone
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                latency_min = (datetime.now(timezone.utc) - created_dt).total_seconds() / 60
                business_metrics.record_match_latency_minutes(airport.code, round(latency_min, 1))
            except Exception:
                pass

    return True


# ── Step 2: Global pool optimisation ─────────────────────────────────

def optimize_pool(airport: AirportConfig, now: datetime) -> int:
    """
    Globally optimises all active trips in the pool.
    Creates/replaces TentativeMatches. Does NOT notify users (silent).
    """
    pool = _query_active_pool(airport.code, now)
    if len(pool) < 2:
        return 0

    pairs = build_compatibility_matrix(pool, airport, now)
    assignments = find_optimal_assignments(pairs)
    tentative_created = 0

    for trip_a_id, trip_b_id, score, dist_km, detour_min in assignments:
        trip_a = next((t for t in pool if t["tripId"] == trip_a_id), None)
        trip_b = next((t for t in pool if t["tripId"] == trip_b_id), None)
        if not trip_a or not trip_b:
            continue

        # Already paired together → update score if significantly improved
        existing_tm = dynamo.get_tentative_match_between(trip_a_id, trip_b_id)
        if existing_tm:
            if score > float(existing_tm.get("score", 0)) + 0.05:
                dynamo.update_item(
                    existing_tm["pk"], "META",
                    {"score": round(score, 2)},
                )
                logger.info("tentative_match_score_updated", matchId=existing_tm.get("matchId"), newScore=round(score, 2))
            continue

        # Dissolve any different existing TentativeMatch for either trip
        trip_a, trip_b = _dissolve_stale_tentative(trip_a, trip_b, pool)

        # Compute lock window
        dt_a = datetime.fromisoformat(trip_a["flightTime"].replace("Z", "+00:00"))
        dt_b = datetime.fromisoformat(trip_b["flightTime"].replace("Z", "+00:00"))
        lock_at = min(dt_a, dt_b) - timedelta(hours=LOCK_HOURS_BEFORE)

        if lock_at <= now:
            # Past lock window — create definitive match directly
            _create_direct_match(trip_a, trip_b, score, airport)
            metrics.add_metric(name="MatchesLocked", unit=MetricUnit.Count, value=1)
            continue

        try:
            dynamo.create_tentative_match(
                trip_a, trip_b,
                score=score,
                dist_km=dist_km,
                detour_min=detour_min,
                lock_at=lock_at,
                airport_code=airport.code,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "TransactionCanceledException":
                # Concurrent optimize_pool already claimed one of these trips — skip.
                logger.info(
                    "tentative_match_concurrent_skip",
                    tripId1=trip_a_id,
                    tripId2=trip_b_id,
                )
                continue
            raise
        tentative_created += 1
        metrics.add_metric(name="TentativeMatchesCreated", unit=MetricUnit.Count, value=1)

    return tentative_created


def _query_active_pool(airport_code: str, now: datetime) -> list[dict]:
    """Returns scheduled + tentative_match trips not yet tracking_pending."""
    lock_cutoff = (now + timedelta(hours=LOCK_HOURS_BEFORE)).isoformat().replace("+00:00", "Z")

    scheduled = dynamo.query_gsi(
        index_name="GSI5-TripStatus",
        pk_name="gsi5pk",
        pk_value=f"{airport_code}#scheduled",
    )
    tentative = dynamo.query_gsi(
        index_name="GSI5-TripStatus",
        pk_name="gsi5pk",
        pk_value=f"{airport_code}#tentative_match",
    )
    now_iso = now.isoformat().replace("+00:00", "Z")
    # Exclude tentative trips already inside lock window (process_lock_window handles those)
    active_tentative = [t for t in tentative if t.get("flightTime", "") > lock_cutoff]
    pool = scheduled + active_tentative
    # tracking_pending trips have no valid ETA — exclude from matching
    # Exclude trips whose flight has already departed
    return [t for t in pool if t.get("status") != "tracking_pending" and t.get("flightTime", "") >= now_iso]


def _dissolve_stale_tentative(
    trip_a: dict,
    trip_b: dict,
    pool: list[dict],
) -> tuple[dict, dict]:
    """
    Dissolves any existing TentativeMatch that is NOT between trip_a and trip_b.
    Returns refreshed trip_a and trip_b dicts.
    """
    for trip in [trip_a, trip_b]:
        tm_id = trip.get("tentativeMatchId")
        if not tm_id:
            continue
        existing = dynamo.get_item(f"TENTATIVE_MATCH#{tm_id}", "META")
        if not existing:
            continue
        other_id = existing["tripId2"] if existing["tripId1"] == trip["tripId"] else existing["tripId1"]
        other_trip = dynamo.get_item(f"TRIP#{other_id}", "META")
        if other_trip:
            dynamo.dissolve_tentative_match(tm_id, trip, other_trip)
            put_event("match.tentative_dissolved", {
                "matchId": tm_id,
                "tripId1": trip["tripId"],
                "tripId2": other_id,
                "reason": "better_match_found",
            })
            # Refresh from pool or DB
            refreshed = dynamo.get_item(trip["pk"], "META") or trip
            if trip["tripId"] == trip_a["tripId"]:
                trip_a = refreshed
            else:
                trip_b = refreshed

    return trip_a, trip_b


def _create_direct_match(
    trip_a: dict,
    trip_b: dict,
    score: float,
    airport: AirportConfig,
) -> None:
    """Creates a definitive match directly (no TentativeMatch exists). Used when already past lock window."""
    table_name = os.environ["TABLE_NAME"]
    match_item = build_match_item(trip_a, trip_b, score)

    # Keep gsi5pk = "{airport}#matched" so dissolve/expire checker can find these
    # via GSI5 (gsi5sk = flightTime). Drop gsi1pk to remove from the active pool index.
    locked_a = {k: v for k, v in trip_a.items() if k not in ("gsi1pk",)}
    locked_a.update({
        "status": "matched",
        "tentativeMatchId": None,
        "matchId": match_item["matchId"],
        "gsi5pk": f"{airport.code}#matched",
    })

    locked_b = {k: v for k, v in trip_b.items() if k not in ("gsi1pk",)}
    locked_b.update({
        "status": "matched",
        "tentativeMatchId": None,
        "matchId": match_item["matchId"],
        "gsi5pk": f"{airport.code}#matched",
    })

    try:
        dynamo.transact_write([
            {"Put": {"Item": dynamo.to_ddb(match_item), "TableName": table_name}},
            {
                "Put": {
                    "Item": dynamo.to_ddb(locked_a),
                    "TableName": table_name,
                    "ConditionExpression": "#s IN (:sched, :tent)",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {
                        ":sched": {"S": "scheduled"},
                        ":tent": {"S": "tentative_match"},
                    },
                }
            },
            {
                "Put": {
                    "Item": dynamo.to_ddb(locked_b),
                    "TableName": table_name,
                    "ConditionExpression": "#s IN (:sched, :tent)",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {
                        ":sched": {"S": "scheduled"},
                        ":tent": {"S": "tentative_match"},
                    },
                }
            },
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            logger.warning("direct_match_skipped_idempotent", tripId1=trip_a["tripId"], tripId2=trip_b["tripId"])
            return
        raise

    put_event("match.found", {
        "matchId": match_item["matchId"],
        "airportCode": airport.code,
        "userId1": trip_a["userId"],
        "userId2": trip_b["userId"],
        "score": round(score, 2),
        "savings": airport.base_fare // 2 / 100,
    })
    logger.info("direct_match_created", matchId=match_item["matchId"], score=round(score, 2))


# ── Compatibility matrix + assignment (Sprint 2) ──────────────────────

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
            if trip_a["userId"] == trip_b["userId"]:
                continue
            if not can_match_direction(trip_a, trip_b):
                continue
            if "destLat" not in trip_a or "destLat" not in trip_b:
                continue
            if not trip_a.get("flightTime") or not trip_b.get("flightTime"):
                continue

            # Prevent re-matching with the same unresponsive partner
            if trip_b["userId"] in trip_a.get("previousMatchPartners", []) or \
               trip_a["userId"] in trip_b.get("previousMatchPartners", []):
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
                pairs.append((trip_a["tripId"], trip_b["tripId"], score, round(dist_km, 2), round(detour_min, 2)))

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


# ── Legacy helpers (kept for backward compat / v3 paths) ─────────────

def process_airport(airport: AirportConfig) -> int:
    """v3-style greedy matching. Superseded by process_airport_v4."""
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=airport.scheduled_advance_days)
    window_start = now - timedelta(hours=2)

    scheduled_trips = dynamo.query_gsi(
        index_name="GSI5-TripStatus",
        pk_name="gsi5pk",
        pk_value=f"{airport.code}#scheduled",
    )
    logger.info("matchmaker_scan_v3", airport=airport.code, trips=len(scheduled_trips))

    matched_ids: set[str] = set()
    matches_created = 0

    for trip in scheduled_trips:
        if trip["pk"] in matched_ids or trip.get("status") == "matched":
            continue
        flight_dt = datetime.fromisoformat(trip.get("flightTime", trip["createdAt"]).replace("Z", "+00:00"))
        if flight_dt + timedelta(hours=2) < now:
            expire_trip(trip)
            continue
        if not (window_start <= flight_dt <= window_end):
            continue

        c_user = dynamo.get_item(f"USER#{trip['userId']}", "PROFILE") or {}
        best_match = find_best_match(query_trip=trip, query_user=c_user)
        if best_match:
            candidate = best_match.candidate
            create_match(trip, candidate, best_match.score)
            matched_ids.add(trip["pk"])
            matched_ids.add(candidate["pk"])
            matches_created += 1

    return matches_created


def expire_trip(trip: dict) -> None:
    trip["status"] = "expired"
    for key in ("gsi5pk", "gsi5sk", "gsi1pk", "gsi1sk"):
        trip.pop(key, None)
    dynamo.put_item(trip)
    put_event("trip.expired", {"tripId": trip["tripId"], "airportCode": trip.get("airportCode")})


def create_match(trip_a: dict, trip_b: dict, score: float) -> None:
    match_item = build_match_item(trip_a, trip_b, score)
    table_name = os.environ["TABLE_NAME"]

    for t in [trip_a, trip_b]:
        t["status"] = "matched"
        t["matchId"] = match_item["matchId"]
        t.pop("gsi5pk", None)
        t.pop("gsi1pk", None)

    dynamo.transact_write([
        {"Put": {"Item": dynamo.to_ddb(match_item), "TableName": table_name}},
        {"Put": {"Item": dynamo.to_ddb(trip_a), "TableName": table_name}},
        {"Put": {"Item": dynamo.to_ddb(trip_b), "TableName": table_name}},
    ])
    put_event("match.found", {
        "matchId": match_item["matchId"],
        "airportCode": trip_a["airportCode"],
        "userId1": trip_a["userId"],
        "userId2": trip_b["userId"],
    })
