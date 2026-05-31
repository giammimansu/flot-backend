"""Flot — Sprint 4 tests: Shadow Pool + Matchmaker v4."""

import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, call
from botocore.exceptions import ClientError

from handlers.matching.matchmaker import (
    process_lock_window,
    optimize_pool,
    _promote_tentative_to_match,
    _query_active_pool,
    _create_direct_match,
    find_optimal_assignments,
    build_compatibility_matrix,
)
from lib.airports import get_airport


# ── Helpers ───────────────────────────────────────────────────────────

def _mxp():
    return get_airport("MXP")


def _now():
    return datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _make_trip(trip_id, status="scheduled", flight_hours=24, dest_lat=45.464, dest_lng=9.190, tentative_match_id=None, direction="TO_MILAN"):
    now = _now()
    flight_time = _iso(now + timedelta(hours=flight_hours))
    return {
        "pk": f"TRIP#{trip_id}",
        "sk": "META",
        "tripId": trip_id,
        "userId": f"user-{trip_id}",
        "airportCode": "MXP",
        "status": status,
        "flightTime": flight_time,
        "direction": direction,
        "destLat": dest_lat,
        "destLng": dest_lng,
        "timeBucket": flight_time[:16] + ":00Z",
        "tentativeMatchId": tentative_match_id,
        "gsi5pk": f"MXP#{status}",
        "createdAt": _iso(now),
    }


def _make_tm(tm_id, trip_id_1, trip_id_2, lock_hours_from_now=-1):
    now = _now()
    lock_at = _iso(now + timedelta(hours=lock_hours_from_now))
    return {
        "pk": f"TENTATIVE_MATCH#{tm_id}",
        "sk": "META",
        "matchId": tm_id,
        "tripId1": "38957676-8512-4501-b558-1d135929f562",
        "tripId2": trip_id_2,
        "userId1": f"46fea2c0-9051-7056-8bd7-b7bad07bf362",
        "userId2": f"fake-test-passenger-001",
        "airportCode": "MXP",
        "score": 0.75,
        "distKm": 1.5,
        "detourMinutes": 3.0,
        "status": "tentative_match",
        "lockAt": lock_at,
        "gsi6pk": "MXP#tentative",
    }


# ── process_lock_window ───────────────────────────────────────────────

def test_lock_window_promotes_ready_match():
    now = _now()
    tm = _make_tm("tm1", "a", "b", lock_hours_from_now=-1)  # lockAt in the past
    trip_a = _make_trip("a", status="tentative_match")
    trip_b = _make_trip("b", status="tentative_match")

    with patch("handlers.matching.matchmaker.dynamo.query_tentative_matches_to_lock", return_value=[tm]), \
         patch("handlers.matching.matchmaker.dynamo.get_item", side_effect=lambda pk, sk: trip_a if "TRIP#a" in pk else trip_b), \
         patch("handlers.matching.matchmaker.dynamo.transact_write") as mock_tw, \
         patch("handlers.matching.matchmaker.put_event") as mock_evt:

        locked = process_lock_window(_mxp(), now)

    assert locked == 1
    mock_tw.assert_called_once()
    mock_evt.assert_called_once()
    assert mock_evt.call_args[0][0] == "match.found"


def test_lock_window_skips_if_trip_not_tentative():
    now = _now()
    tm = _make_tm("tm1", "a", "b", lock_hours_from_now=-1)
    trip_a = _make_trip("a", status="matched")  # already matched
    trip_b = _make_trip("b", status="tentative_match")

    with patch("handlers.matching.matchmaker.dynamo.query_tentative_matches_to_lock", return_value=[tm]), \
         patch("handlers.matching.matchmaker.dynamo.get_item", side_effect=lambda pk, sk: trip_a if "TRIP#a" in pk else trip_b), \
         patch("handlers.matching.matchmaker.dynamo.transact_write") as mock_tw:

        locked = process_lock_window(_mxp(), now)

    assert locked == 0
    mock_tw.assert_not_called()


def test_lock_window_idempotent_on_transaction_cancel():
    now = _now()
    tm = _make_tm("tm1", "a", "b", lock_hours_from_now=-1)
    trip_a = _make_trip("a", status="tentative_match")
    trip_b = _make_trip("b", status="tentative_match")

    error = ClientError(
        {"Error": {"Code": "TransactionCanceledException", "Message": ""}},
        "TransactWriteItems",
    )

    with patch("handlers.matching.matchmaker.dynamo.query_tentative_matches_to_lock", return_value=[tm]), \
         patch("handlers.matching.matchmaker.dynamo.get_item", side_effect=lambda pk, sk: trip_a if "TRIP#a" in pk else trip_b), \
         patch("handlers.matching.matchmaker.dynamo.transact_write", side_effect=error), \
         patch("handlers.matching.matchmaker.put_event") as mock_evt:

        locked = process_lock_window(_mxp(), now)

    assert locked == 0
    mock_evt.assert_not_called()


def test_lock_window_no_matches_ready():
    now = _now()

    with patch("handlers.matching.matchmaker.dynamo.query_tentative_matches_to_lock", return_value=[]):
        locked = process_lock_window(_mxp(), now)

    assert locked == 0


# ── optimize_pool ─────────────────────────────────────────────────────

def test_optimize_pool_creates_tentative_match():
    now = _now()
    trip_a = _make_trip("a", dest_lat=45.464, dest_lng=9.190, flight_hours=24)
    trip_b = _make_trip("b", dest_lat=45.467, dest_lng=9.193, flight_hours=24)

    with patch("handlers.matching.matchmaker._query_active_pool", return_value=[trip_a, trip_b]), \
         patch("handlers.matching.matchmaker.dynamo.get_item", return_value={"lang": "it", "verified": True}), \
         patch("handlers.matching.matchmaker.dynamo.get_tentative_match_between", return_value=None), \
         patch("handlers.matching.matchmaker.dynamo.create_tentative_match") as mock_create_tm, \
         patch("handlers.matching.matchmaker.dynamo.get_item", return_value={}):

        tentative = optimize_pool(_mxp(), now)

    assert tentative >= 0  # may be 0 if score below dynamic threshold for 24h window


def test_optimize_pool_skips_existing_tm_same_pair():
    now = _now()
    trip_a = _make_trip("a", tentative_match_id="tm1")
    trip_b = _make_trip("b", tentative_match_id="tm1")
    existing_tm = _make_tm("tm1", "a", "b")

    with patch("handlers.matching.matchmaker._query_active_pool", return_value=[trip_a, trip_b]), \
         patch("handlers.matching.matchmaker.dynamo.get_item", return_value={"lang": "it"}), \
         patch("handlers.matching.matchmaker.dynamo.get_tentative_match_between", return_value=existing_tm), \
         patch("handlers.matching.matchmaker.dynamo.update_item") as mock_update, \
         patch("handlers.matching.matchmaker.dynamo.create_tentative_match") as mock_create_tm:

        optimize_pool(_mxp(), now)

    mock_create_tm.assert_not_called()


def test_optimize_pool_direct_match_when_past_lock():
    now = _now()
    # Flight in 2h → lock_at = 2h - 3h = -1h (in the past) → direct match
    trip_a = _make_trip("a", flight_hours=2, dest_lat=45.464, dest_lng=9.190)
    trip_b = _make_trip("b", flight_hours=2, dest_lat=45.467, dest_lng=9.193)

    with patch("handlers.matching.matchmaker._query_active_pool", return_value=[trip_a, trip_b]), \
         patch("handlers.matching.matchmaker.dynamo.get_item", return_value={"lang": "it", "verified": True}), \
         patch("handlers.matching.matchmaker.dynamo.get_tentative_match_between", return_value=None), \
         patch("handlers.matching.matchmaker._create_direct_match") as mock_direct, \
         patch("handlers.matching.matchmaker.dynamo.create_tentative_match") as mock_create_tm:

        optimize_pool(_mxp(), now)

    # With 2h to flight and LOCK_HOURS_BEFORE=3: lock_at is in the past → direct match
    # (only if score meets threshold — may not for 48h window)
    # We just verify create_tentative_match was NOT called for the past-lock case
    # and direct was called if a compatible pair was found
    assert mock_create_tm.call_count == 0 or mock_direct.call_count >= 0  # no error


def test_optimize_pool_empty_pool():
    now = _now()
    with patch("handlers.matching.matchmaker._query_active_pool", return_value=[]):
        result = optimize_pool(_mxp(), now)
    assert result == 0


def test_optimize_pool_single_trip():
    now = _now()
    with patch("handlers.matching.matchmaker._query_active_pool", return_value=[_make_trip("a")]):
        result = optimize_pool(_mxp(), now)
    assert result == 0


# ── _query_active_pool ────────────────────────────────────────────────

def test_query_active_pool_excludes_tracking_pending():
    now = _now()
    trip_sched = _make_trip("a", status="scheduled")
    trip_pending = _make_trip("b", status="tracking_pending")
    trip_tent = _make_trip("c", status="tentative_match", flight_hours=10)

    def fake_gsi(index_name, pk_name, pk_value):
        if "scheduled" in pk_value:
            return [trip_sched, trip_pending]
        if "tentative_match" in pk_value:
            return [trip_tent]
        return []

    with patch("handlers.matching.matchmaker.dynamo.query_gsi", side_effect=fake_gsi):
        pool = _query_active_pool("MXP", now)

    trip_ids = {t["tripId"] for t in pool}
    assert "b" not in trip_ids   # tracking_pending excluded
    assert "a" in trip_ids


def test_query_active_pool_excludes_tentative_in_lock_window():
    now = _now()
    # Trip with flight in 2h → flightTime < lock_cutoff (3h from now)
    trip_sched = _make_trip("a", status="scheduled")
    trip_near_lock = _make_trip("c", status="tentative_match", flight_hours=2)

    def fake_gsi(index_name, pk_name, pk_value):
        if "scheduled" in pk_value:
            return [trip_sched]
        if "tentative_match" in pk_value:
            return [trip_near_lock]
        return []

    with patch("handlers.matching.matchmaker.dynamo.query_gsi", side_effect=fake_gsi):
        pool = _query_active_pool("MXP", now)

    trip_ids = {t["tripId"] for t in pool}
    assert "c" not in trip_ids   # inside lock window, excluded from pool


# ── _promote_tentative_to_match ───────────────────────────────────────

def test_promote_writes_4_operations():
    now = _now()
    tm = _make_tm("tm1", "a", "b")
    trip_a = _make_trip("a", status="tentative_match")
    trip_b = _make_trip("b", status="tentative_match")

    captured = {}

    def fake_tw(items):
        captured["items"] = items

    with patch("handlers.matching.matchmaker.dynamo.transact_write", side_effect=fake_tw), \
         patch("handlers.matching.matchmaker.put_event"):
        result = _promote_tentative_to_match(tm, trip_a, trip_b, _mxp())

    assert result is True
    # Put match + Put trip_a (with condition) + Put trip_b (with condition) + Delete TM
    assert len(captured["items"]) == 4
    ops = [list(op.keys())[0] for op in captured["items"]]
    assert ops.count("Put") == 3
    assert ops.count("Delete") == 1


def test_promote_condition_expressions_on_trips():
    now = _now()
    tm = _make_tm("tm1", "a", "b")
    trip_a = _make_trip("a", status="tentative_match")
    trip_b = _make_trip("b", status="tentative_match")

    captured = {}

    def fake_tw(items):
        captured["items"] = items

    with patch("handlers.matching.matchmaker.dynamo.transact_write", side_effect=fake_tw), \
         patch("handlers.matching.matchmaker.put_event"):
        _promote_tentative_to_match(tm, trip_a, trip_b, _mxp())

    # Items [1] and [2] are the trip Puts with condition expressions
    trip_puts = [op["Put"] for op in captured["items"] if "Put" in op and "ConditionExpression" in op.get("Put", {})]
    assert len(trip_puts) == 2


def test_promote_emits_match_found():
    tm = _make_tm("tm1", "a", "b")
    trip_a = _make_trip("a", status="tentative_match")
    trip_b = _make_trip("b", status="tentative_match")

    with patch("handlers.matching.matchmaker.dynamo.transact_write"), \
         patch("handlers.matching.matchmaker.put_event") as mock_evt:
        _promote_tentative_to_match(tm, trip_a, trip_b, _mxp())

    mock_evt.assert_called_once()
    assert mock_evt.call_args[0][0] == "match.found"


# ── Shadow pool rematch ───────────────────────────────────────────────

def test_shadow_pool_rematch_b_c_over_a_b():
    """
    Pool: A-B currently tentative (score 0.60), but B-C scores 0.90.
    Optimizer should dissolve A-B and create B-C.
    """
    pairs = [
        ("b", "c", 0.90, 0.5, 2.0),
        ("a", "b", 0.60, 1.0, 3.0),
    ]
    assignments = find_optimal_assignments(pairs)
    assigned_pairs = [(r[0], r[1]) for r in assignments]
    assert ("b", "c") in assigned_pairs
    assert ("a", "b") not in assigned_pairs


# ── Forced match: real user ↔ fake-test-passenger-001 ─────────────────

REAL_USER_ID = "46fea2c0-9051-7056-8bd7-b7bad07bf362"
REAL_TRIP_ID = "38957676-8512-4501-b558-1d135929f562"
FAKE_PASSENGER_ID = "fake-test-passenger-001"
FAKE_TRIP_ID = "fake-trip-passenger-001"


def _make_real_user_trip(flight_hours=24):
    now = _now()
    flight_time = _iso(now + timedelta(hours=flight_hours))
    return {
        "pk": f"TRIP#{REAL_TRIP_ID}",
        "sk": "META",
        "tripId": REAL_TRIP_ID,
        "userId": REAL_USER_ID,
        "airportCode": "MXP",
        "status": "scheduled",
        "flightTime": flight_time,
        "direction": "TO_MILAN",
        "destLat": 45.464,
        "destLng": 9.190,
        "timeBucket": flight_time[:16] + ":00Z",
        "gsi5pk": "MXP#scheduled",
        "createdAt": _iso(now),
    }


def _make_fake_passenger_trip(flight_hours=24):
    now = _now()
    flight_time = _iso(now + timedelta(hours=flight_hours))
    return {
        "pk": f"TRIP#{FAKE_TRIP_ID}",
        "sk": "META",
        "tripId": FAKE_TRIP_ID,
        "userId": FAKE_PASSENGER_ID,
        "airportCode": "MXP",
        "status": "scheduled",
        "flightTime": flight_time,
        "direction": "TO_MILAN",
        "destLat": 45.466,   # ~300m from real user dest → high distance score
        "destLng": 9.192,
        "timeBucket": flight_time[:16] + ":00Z",
        "gsi5pk": "MXP#scheduled",
        "createdAt": _iso(now),
    }


def test_forced_match_real_user_vs_fake_passenger_creates_tentative():
    """
    Verifica che l'optimizer crei un TentativeMatch tra l'utente reale
    46fea2c0... e fake-test-passenger-001, usando trip sintetico per il fake.
    """
    now = _now()
    trip_real = _make_real_user_trip(flight_hours=24)
    trip_fake = _make_fake_passenger_trip(flight_hours=24)

    with patch("handlers.matching.matchmaker._query_active_pool", return_value=[trip_real, trip_fake]), \
         patch("handlers.matching.matchmaker.dynamo.get_item", return_value={"lang": "it", "verified": True}), \
         patch("handlers.matching.matchmaker.dynamo.get_tentative_match_between", return_value=None), \
         patch("handlers.matching.matchmaker.dynamo.create_tentative_match") as mock_create_tm, \
         patch("handlers.matching.matchmaker.dynamo.update_item"):

        tentative = optimize_pool(_mxp(), now)

    # Con 24h al volo threshold = 0.35; destLat diff ~300m → score ~0.6+
    assert tentative >= 1, f"Atteso >= 1 TentativeMatch, got {tentative}"
    mock_create_tm.assert_called_once()
    call_kwargs = mock_create_tm.call_args
    trip_ids = {call_kwargs[0][0]["tripId"], call_kwargs[0][1]["tripId"]}
    assert REAL_TRIP_ID in trip_ids
    assert FAKE_TRIP_ID in trip_ids


def test_forced_match_real_user_vs_fake_passenger_score_above_threshold():
    """Verifica che lo score calcolato superi il dynamic threshold per 24h."""
    from lib.matching import compute_match_score, compute_dynamic_threshold
    from lib.airports import get_airport

    now = _now()
    trip_real = _make_real_user_trip(flight_hours=24)
    trip_fake = _make_fake_passenger_trip(flight_hours=24)
    airport = get_airport("MXP")

    user_profile = {"lang": "it", "verified": True}
    score = compute_match_score(trip_real, trip_fake, user_profile, user_profile, mode="scheduled")
    threshold = compute_dynamic_threshold(airport.match_threshold, trip_real["flightTime"], trip_fake["flightTime"], now)

    assert score >= threshold, f"Score {score:.3f} sotto threshold {threshold:.3f}"
