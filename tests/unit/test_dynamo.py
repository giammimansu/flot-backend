"""Flot — Unit tests for DynamoDB TentativeMatch helpers (Sprint 3)."""

import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from lib.dynamo import (
    create_tentative_match,
    dissolve_tentative_match,
    get_tentative_match_between,
    query_tentative_matches_to_lock,
)


# ── Fixtures ──────────────────────────────────────────────────────────

def _make_trip(trip_id: str, airport: str = "MXP", status: str = "scheduled") -> dict:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "pk": f"TRIP#{trip_id}",
        "sk": "META",
        "tripId": trip_id,
        "userId": f"user-{trip_id}",
        "airportCode": airport,
        "status": status,
        "tentativeMatchId": None,
        "gsi5pk": f"{airport}#{status}",
        "flightTime": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
        "createdAt": now,
    }


def _lock_at(hours_from_now: float = 3.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours_from_now)


# ── create_tentative_match ────────────────────────────────────────────

def test_create_tentative_match_item_shape():
    trip_a = _make_trip("a")
    trip_b = _make_trip("b")
    lock_at = _lock_at()

    with patch("lib.dynamo.transact_write") as mock_tw:
        result = create_tentative_match(trip_a, trip_b, score=0.75, dist_km=2.5, detour_min=4.2, lock_at=lock_at, airport_code="MXP")

    assert result["status"] == "tentative_match"
    assert result["tripId1"] == "a"
    assert result["tripId2"] == "b"
    assert result["score"] == 0.75
    assert result["distKm"] == 2.5
    assert result["detourMinutes"] == 4.2
    assert result["gsi6pk"] == "MXP#tentative"
    assert result["lockAt"] == lock_at.isoformat().replace("+00:00", "Z")
    assert result["pk"].startswith("TENTATIVE_MATCH#")


def test_create_tentative_match_calls_transact_write():
    trip_a = _make_trip("a")
    trip_b = _make_trip("b")

    with patch("lib.dynamo.transact_write") as mock_tw:
        create_tentative_match(trip_a, trip_b, score=0.75, dist_km=2.5, detour_min=4.2, lock_at=_lock_at(), airport_code="MXP")

    mock_tw.assert_called_once()
    items = mock_tw.call_args[0][0]
    # 3 operations: Put TentativeMatch + Put trip_a + Put trip_b
    assert len(items) == 3
    assert "Put" in items[0]
    assert "Put" in items[1]
    assert "Put" in items[2]


def test_create_tentative_match_rounds_floats():
    trip_a = _make_trip("a")
    trip_b = _make_trip("b")

    with patch("lib.dynamo.transact_write"):
        result = create_tentative_match(
            trip_a, trip_b,
            score=0.756789,
            dist_km=2.5678,
            detour_min=4.2345,
            lock_at=_lock_at(),
            airport_code="MXP",
        )

    assert result["score"] == 0.76
    assert result["distKm"] == 2.57
    assert result["detourMinutes"] == 4.23


# ── dissolve_tentative_match ──────────────────────────────────────────

def test_dissolve_tentative_match_resets_trips():
    trip_a = _make_trip("a", status="tentative_match")
    trip_a["tentativeMatchId"] = "tm-123"
    trip_b = _make_trip("b", status="tentative_match")
    trip_b["tentativeMatchId"] = "tm-123"

    captured = {}

    def fake_transact(items):
        captured["items"] = items

    with patch("lib.dynamo.transact_write", side_effect=fake_transact):
        dissolve_tentative_match("tm-123", trip_a, trip_b)

    items = captured["items"]
    assert len(items) == 3  # Delete + Put trip_a + Put trip_b
    assert "Delete" in items[0]


def test_dissolve_tentative_match_trips_back_to_scheduled():
    trip_a = _make_trip("a", status="tentative_match")
    trip_a["tentativeMatchId"] = "tm-123"
    trip_b = _make_trip("b", status="tentative_match")
    trip_b["tentativeMatchId"] = "tm-123"

    written_trips = []

    def fake_transact(items):
        for op in items:
            if "Put" in op:
                written_trips.append(op["Put"]["Item"])

    with patch("lib.dynamo.transact_write", side_effect=fake_transact):
        dissolve_tentative_match("tm-123", trip_a, trip_b)

    # Both put items should be back to scheduled
    for item in written_trips:
        status_val = item.get("status") or item.get("status", {}).get("S")
        # item is already marshalled via to_ddb — check S key
        if isinstance(status_val, dict):
            assert status_val.get("S") == "scheduled"
        else:
            assert status_val == "scheduled"


# ── query_tentative_matches_to_lock ───────────────────────────────────

def test_query_tentative_matches_to_lock_calls_gsi6():
    now = datetime.now(timezone.utc)

    with patch("lib.dynamo.query_gsi") as mock_query:
        mock_query.return_value = []
        query_tentative_matches_to_lock("MXP", now)

    mock_query.assert_called_once()
    call_kwargs = mock_query.call_args[1] if mock_query.call_args[1] else mock_query.call_args[0][0] if isinstance(mock_query.call_args[0][0], dict) else None
    call_args = mock_query.call_args
    assert call_args[1].get("index_name") == "GSI6-TentativeMatch" or \
           (call_args[0] and call_args[0][0] == "GSI6-TentativeMatch")


def test_query_tentative_matches_to_lock_pk_format():
    now = datetime.now(timezone.utc)

    with patch("lib.dynamo.query_gsi") as mock_query:
        mock_query.return_value = []
        query_tentative_matches_to_lock("MXP", now)

    kwargs = mock_query.call_args.kwargs
    assert kwargs["pk_value"] == "MXP#tentative"


# ── get_tentative_match_between ───────────────────────────────────────

def test_get_tentative_match_between_returns_match():
    trip_a_record = {**_make_trip("a"), "tentativeMatchId": "tm-999"}
    tm_record = {
        "pk": "TENTATIVE_MATCH#tm-999",
        "sk": "META",
        "matchId": "tm-999",
        "tripId1": "a",
        "tripId2": "b",
        "status": "tentative_match",
    }

    def fake_get_item(pk, sk):
        if pk == "TRIP#a":
            return trip_a_record
        if pk == "TENTATIVE_MATCH#tm-999":
            return tm_record
        return None

    with patch("lib.dynamo.get_item", side_effect=fake_get_item):
        result = get_tentative_match_between("a", "b")

    assert result is not None
    assert result["matchId"] == "tm-999"


def test_get_tentative_match_between_wrong_pair_returns_none():
    trip_a_record = {**_make_trip("a"), "tentativeMatchId": "tm-999"}
    tm_record = {
        "pk": "TENTATIVE_MATCH#tm-999",
        "sk": "META",
        "matchId": "tm-999",
        "tripId1": "a",
        "tripId2": "c",   # different pair
        "status": "tentative_match",
    }

    def fake_get_item(pk, sk):
        if pk == "TRIP#a":
            return trip_a_record
        if pk == "TENTATIVE_MATCH#tm-999":
            return tm_record
        return None

    with patch("lib.dynamo.get_item", side_effect=fake_get_item):
        result = get_tentative_match_between("a", "b")

    assert result is None


def test_get_tentative_match_between_no_match_id():
    trip_a_record = _make_trip("a")  # tentativeMatchId = None

    with patch("lib.dynamo.get_item", return_value=trip_a_record):
        result = get_tentative_match_between("a", "b")

    assert result is None


def test_get_tentative_match_between_trip_not_found():
    with patch("lib.dynamo.get_item", return_value=None):
        result = get_tentative_match_between("a", "b")

    assert result is None
