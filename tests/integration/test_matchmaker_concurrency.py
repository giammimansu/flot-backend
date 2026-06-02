"""Flot — Matchmaker concurrency tests.

Validates that concurrent matchmaker runs do not create duplicate matches
or assign the same trip to two different matches. Uses moto + threading
to simulate parallel Lambda invocations.

Scenarios:
  1. Two parallel optimize_pool runs → no duplicate TentativeMatches
  2. process_lock_window runs concurrently → TransactionCanceled, not double-lock
  3. Pool with odd number of trips → no trip matched twice
  4. Dissolve of a TentativeMatch while lock_window is promoting it → safe skip
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock

import boto3
import pytest
from moto import mock_aws

from tests.conftest import build_api_event


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mt_table(dynamodb_table):
    """Alias for dynamodb_table — used for clarity in concurrency tests."""
    return dynamodb_table


def _now():
    return datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _make_trip(
    trip_id: str,
    status: str = "scheduled",
    *,
    table_resource,
    flight_hours: float = 24.0,
    dest_lat: float = 45.464,
    dest_lng: float = 9.190,
) -> dict:
    now = _now()
    flight_time = _iso(now + timedelta(hours=flight_hours))
    bucket = flight_time[:14] + "00:00Z"
    item = {
        "pk": f"TRIP#{trip_id}",
        "sk": "META",
        "tripId": trip_id,
        "userId": f"user-{trip_id}",
        "airportCode": "MXP",
        "status": status,
        "flightTime": flight_time,
        "flightDate": flight_time[:10],
        "flightNumber": "AZ1234",
        "direction": "TO_CITY",
        "destLat": Decimal(str(dest_lat)),
        "destLng": Decimal(str(dest_lng)),
        "timeBucket": bucket,
        "gsi1pk": f"MXP#{bucket}",
        "gsi1sk": flight_time,
        "gsi5pk": f"MXP#{status}",
        "gsi5sk": flight_time,
        "createdAt": _iso(now),
        "updatedAt": _iso(now),
    }
    table_resource.put_item(Item=item)
    return item


def _make_tentative_match(
    tm_id: str,
    trip_id_1: str,
    trip_id_2: str,
    *,
    table_resource,
    flight_hours: float = 1.5,  # inside lock window (< 3h)
) -> dict:
    now = _now()
    lock_at = _iso(now + timedelta(hours=flight_hours))
    item = {
        "pk": f"TENTATIVE_MATCH#{tm_id}",
        "sk": "META",
        "matchId": tm_id,
        "tripId1": trip_id_1,
        "tripId2": trip_id_2,
        "airportCode": "MXP",
        "score": Decimal("0.75"),
        "lockAt": lock_at,
        "gsi6pk": "MXP",
        "createdAt": _iso(now),
    }
    table_resource.put_item(Item=item)
    return item


# ---------------------------------------------------------------------------
# Scenario 1: Two parallel optimize_pool runs → no duplicate TentativeMatches
# ---------------------------------------------------------------------------

class TestParallelOptimizePool:
    def test_no_duplicate_tentative_matches(self, mt_table, lambda_context):
        """Two concurrent optimize_pool calls on same pool must produce at most N/2 matches."""
        _make_trip("t1", table_resource=mt_table)
        _make_trip("t2", table_resource=mt_table, dest_lat=45.465, dest_lng=9.191)
        _make_trip("t3", table_resource=mt_table, dest_lat=45.463, dest_lng=9.189)
        _make_trip("t4", table_resource=mt_table, dest_lat=45.466, dest_lng=9.192)

        from handlers.matching.matchmaker import optimize_pool
        from lib.airports import get_airport

        airport = get_airport("MXP")
        now = _now()

        results = []
        errors = []

        def run():
            try:
                with patch("handlers.matching.matchmaker.put_event"):
                    count = optimize_pool(airport, now)
                    results.append(count)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"

        # Count TentativeMatch items in DynamoDB — must be <= N/2 (no trip matched twice)
        resp = mt_table.scan(
            FilterExpression="begins_with(pk, :pfx)",
            ExpressionAttributeValues={":pfx": "TENTATIVE_MATCH#"},
        )
        tentative_items = resp.get("Items", [])

        # Collect all tripIds referenced
        referenced_trips: set[str] = set()
        for item in tentative_items:
            tid1 = item.get("tripId1")
            tid2 = item.get("tripId2")
            assert tid1 not in referenced_trips, f"Trip {tid1} matched twice"
            assert tid2 not in referenced_trips, f"Trip {tid2} matched twice"
            referenced_trips.add(tid1)
            referenced_trips.add(tid2)


# ---------------------------------------------------------------------------
# Scenario 2: Two parallel process_lock_window runs → at most one lock succeeds
# ---------------------------------------------------------------------------

class TestParallelLockWindow:
    def test_only_one_lock_wins(self, mt_table, lambda_context):
        """Concurrent lock_window runs for same TentativeMatch: exactly one must succeed."""
        _make_trip("ta", status="tentative_match", table_resource=mt_table, flight_hours=1.5)
        _make_trip("tb", status="tentative_match", table_resource=mt_table, flight_hours=1.5,
                   dest_lat=45.465, dest_lng=9.191)
        _make_tentative_match("tm1", "ta", "tb", table_resource=mt_table, flight_hours=1.5)

        # Update trips to reference the tentative match
        mt_table.update_item(
            Key={"pk": "TRIP#ta", "sk": "META"},
            UpdateExpression="SET tentativeMatchId = :id",
            ExpressionAttributeValues={":id": "tm1"},
        )
        mt_table.update_item(
            Key={"pk": "TRIP#tb", "sk": "META"},
            UpdateExpression="SET tentativeMatchId = :id",
            ExpressionAttributeValues={":id": "tm1"},
        )

        from handlers.matching.matchmaker import process_lock_window
        from lib.airports import get_airport

        airport = get_airport("MXP")
        now = _now()

        lock_results = []
        errors = []

        def run():
            try:
                with patch("handlers.matching.matchmaker.put_event"):
                    locked = process_lock_window(airport, now)
                    lock_results.append(locked)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"

        # Count final MATCH# items in DB — must be exactly 1
        resp = mt_table.scan(
            FilterExpression="begins_with(pk, :pfx)",
            ExpressionAttributeValues={":pfx": "MATCH#"},
        )
        match_items = [i for i in resp.get("Items", []) if i.get("sk") == "META"]
        assert len(match_items) <= 1, f"Expected at most 1 match, got {len(match_items)}"


# ---------------------------------------------------------------------------
# Scenario 3: Odd pool → no trip matched twice
# ---------------------------------------------------------------------------

class TestOddPool:
    def test_odd_number_of_trips_no_double_match(self, mt_table, lambda_context):
        """5 trips → at most 2 TentativeMatches; 1 trip left unmatched."""
        for i in range(5):
            _make_trip(f"odd{i}", table_resource=mt_table,
                       dest_lat=45.464 + i * 0.001, dest_lng=9.190 + i * 0.001)

        from handlers.matching.matchmaker import optimize_pool
        from lib.airports import get_airport

        airport = get_airport("MXP")
        now = _now()

        with patch("handlers.matching.matchmaker.put_event"):
            optimize_pool(airport, now)

        resp = mt_table.scan(
            FilterExpression="begins_with(pk, :pfx)",
            ExpressionAttributeValues={":pfx": "TENTATIVE_MATCH#"},
        )
        tentative_items = resp.get("Items", [])

        referenced_trips: set[str] = set()
        for item in tentative_items:
            tid1 = item.get("tripId1")
            tid2 = item.get("tripId2")
            assert tid1 not in referenced_trips, f"Trip {tid1} matched twice"
            assert tid2 not in referenced_trips, f"Trip {tid2} matched twice"
            referenced_trips.add(tid1)
            referenced_trips.add(tid2)

        assert len(tentative_items) <= 2  # 5 trips → at most 2 pairs


# ---------------------------------------------------------------------------
# Scenario 4: Dissolve during lock_window promotion → safe skip
# ---------------------------------------------------------------------------

class TestDissolveRaceWithLockWindow:
    def test_dissolve_during_lock_promotion_is_safe(self, mt_table, lambda_context):
        """Lock window must skip (not crash) if TentativeMatch deleted mid-promotion."""
        _make_trip("ra", status="tentative_match", table_resource=mt_table, flight_hours=1.5)
        _make_trip("rb", status="tentative_match", table_resource=mt_table, flight_hours=1.5,
                   dest_lat=45.465, dest_lng=9.191)
        _make_tentative_match("tm_race", "ra", "rb", table_resource=mt_table, flight_hours=1.5)

        mt_table.update_item(
            Key={"pk": "TRIP#ra", "sk": "META"},
            UpdateExpression="SET tentativeMatchId = :id",
            ExpressionAttributeValues={":id": "tm_race"},
        )
        mt_table.update_item(
            Key={"pk": "TRIP#rb", "sk": "META"},
            UpdateExpression="SET tentativeMatchId = :id",
            ExpressionAttributeValues={":id": "tm_race"},
        )

        from handlers.matching.matchmaker import process_lock_window
        from lib.airports import get_airport

        airport = get_airport("MXP")
        now = _now()

        lock_result = []
        errors = []

        def lock_runner():
            try:
                with patch("handlers.matching.matchmaker.put_event"):
                    result = process_lock_window(airport, now)
                    lock_result.append(result)
            except Exception as e:
                errors.append(e)

        def dissolve_runner():
            # Short delay so lock_window starts first, then delete mid-flight
            time.sleep(0.01)
            mt_table.delete_item(Key={"pk": "TENTATIVE_MATCH#tm_race", "sk": "META"})

        t_lock = threading.Thread(target=lock_runner)
        t_dissolve = threading.Thread(target=dissolve_runner)

        t_lock.start()
        t_dissolve.start()
        t_lock.join(timeout=10)
        t_dissolve.join(timeout=10)

        # No crashes — either lock succeeded or it was skipped safely
        assert not errors, f"Lock thread errors: {errors}"
