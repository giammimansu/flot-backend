import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from handlers.matching.matchmaker import process_airport, build_compatibility_matrix, find_optimal_assignments
from lib.airports import get_airport


def _mxp():
    return get_airport("MXP")


def _make_trip(trip_id, dest_lat, dest_lng, flight_hours_from_now=24, direction="TO_MILAN", status="scheduled"):
    now = datetime.now(timezone.utc)
    flight_time = (now + timedelta(hours=flight_hours_from_now)).isoformat().replace("+00:00", "Z")
    return {
        "tripId": trip_id,
        "pk": f"TRIP#{trip_id}",
        "userId": f"user-{trip_id}",
        "status": status,
        "flightTime": flight_time,
        "airportCode": "MXP",
        "direction": direction,
        "destLat": dest_lat,
        "destLng": dest_lng,
        "timeBucket": flight_time[:16] + ":00Z",
        "createdAt": now.isoformat().replace("+00:00", "Z"),
    }


# ── Existing tests (fixed for current AirportConfig signature) ────────

def test_process_airport_no_trips():
    with patch("handlers.matching.matchmaker.dynamo.query_gsi", return_value=[]):
        matches = process_airport(_mxp())
        assert matches == 0


def test_process_airport_with_matches():
    # Future flight times so trips are inside the matching window (not expired).
    now = datetime.now(timezone.utc)
    ft_a = (now + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    ft_b = (now + timedelta(days=1, minutes=15)).isoformat().replace("+00:00", "Z")
    created = now.isoformat().replace("+00:00", "Z")
    mock_trip_a = {
        "tripId": "1", "pk": "TRIP#1", "userId": "u1", "status": "scheduled",
        "flightTime": ft_a, "airportCode": "MXP", "createdAt": created,
    }
    mock_trip_b = {
        "tripId": "2", "pk": "TRIP#2", "userId": "u2", "status": "scheduled",
        "flightTime": ft_b, "airportCode": "MXP", "createdAt": created,
    }

    with patch("handlers.matching.matchmaker.dynamo.query_gsi", return_value=[mock_trip_a, mock_trip_b]), \
         patch("handlers.matching.matchmaker.dynamo.get_item", return_value={"gender": "M"}), \
         patch("handlers.matching.matchmaker.find_best_match") as mock_find, \
         patch("handlers.matching.matchmaker.create_match") as mock_create:

        mock_find.side_effect = [
            MagicMock(candidate=mock_trip_b, score=0.9),
            None,
        ]

        matches = process_airport(_mxp())
        assert matches == 1
        mock_create.assert_called_once()


# ── Sprint 2 tests ────────────────────────────────────────────────────

def test_build_compat_matrix_excludes_high_detour():
    """Pair with detour > max_detour_minutes must be excluded from matrix."""
    now = datetime.now(timezone.utc)
    airport = _mxp()

    # Trip A: Milan est, Trip B: Milan ovest — V-route, high detour
    trip_a = _make_trip("a", dest_lat=45.478, dest_lng=9.235)   # est
    trip_b = _make_trip("b", dest_lat=45.475, dest_lng=9.052)   # ovest (CityLife area)

    with patch("handlers.matching.matchmaker.dynamo.get_item", return_value={"lang": "it", "verified": True}):
        pairs = build_compatibility_matrix([trip_a, trip_b], airport, now)

    # detour should exceed max_detour_minutes=15 for this V-route
    for pair in pairs:
        detour = pair[4]
        assert detour <= airport.max_detour_minutes, f"Pair with detour {detour} should have been filtered"


def test_build_compat_matrix_includes_linear_route():
    """Pair with low detour and good score should appear in matrix."""
    now = datetime.now(timezone.utc)
    airport = _mxp()

    # Both trips near Milan centro — linear route
    trip_a = _make_trip("a", dest_lat=45.464, dest_lng=9.190)
    trip_b = _make_trip("b", dest_lat=45.467, dest_lng=9.193)

    with patch("handlers.matching.matchmaker.dynamo.get_item", return_value={"lang": "it", "verified": True}):
        pairs = build_compatibility_matrix([trip_a, trip_b], airport, now)

    assert len(pairs) >= 1
    _, _, score, _, detour = pairs[0]
    assert detour <= airport.max_detour_minutes
    assert score > 0.0


def test_build_compat_matrix_excludes_opposite_direction():
    """Trips with different direction must be excluded."""
    now = datetime.now(timezone.utc)
    trip_a = _make_trip("a", dest_lat=45.464, dest_lng=9.190, direction="TO_MILAN")
    trip_b = _make_trip("b", dest_lat=45.467, dest_lng=9.193, direction="FROM_MILAN")

    with patch("handlers.matching.matchmaker.dynamo.get_item", return_value={}):
        pairs = build_compatibility_matrix([trip_a, trip_b], _mxp(), now)

    assert len(pairs) == 0


def test_build_compat_matrix_dynamic_threshold_7days():
    """Trip 7 days away uses threshold 0.70 — mediocre score pair excluded."""
    now = datetime.now(timezone.utc)
    airport = _mxp()

    # Trips 7 days away, medium distance (~8 km) — score around 0.5, below 0.70 threshold
    trip_a = _make_trip("a", dest_lat=45.464, dest_lng=9.190, flight_hours_from_now=168)
    trip_b = _make_trip("b", dest_lat=45.505, dest_lng=9.290, flight_hours_from_now=168)

    with patch("handlers.matching.matchmaker.dynamo.get_item", return_value={"lang": "it", "verified": False}):
        pairs = build_compatibility_matrix([trip_a, trip_b], airport, now)

    # With threshold 0.70 and medium-distance/no-profile score, should be excluded
    for pair in pairs:
        assert pair[2] >= 0.70


def test_find_optimal_assignments_shadow_pool_rematch():
    """
    Pool: A-B tentative (score 0.60), but B-C scores 0.90.
    Optimal assignment: B-C first, then A alone (no pair).
    """
    pairs = [
        ("b", "c", 0.90, 0.5, 2.0),
        ("a", "b", 0.60, 1.0, 3.0),
        ("a", "c", 0.55, 1.2, 3.5),
    ]
    assignments = find_optimal_assignments(pairs)
    assigned_pairs = [(r[0], r[1]) for r in assignments]
    assert ("b", "c") in assigned_pairs
    assert ("a", "b") not in assigned_pairs  # b already taken
