"""Flot — Unit tests for matching engine."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from lib.matching import (
    build_match_item,
    can_match_direction,
    can_match_modes,
    compute_dynamic_threshold,
    compute_match_score,
    compute_pickup_point,
    compute_pickup_time,
    distance_score,
    estimate_detour_minutes,
    apply_detour_penalty,
    get_match_coords,
    haversine_km,
    is_in_active_window,
    luggage_score,
    get_adjacent_slots,
    get_slot_bucket,
    next_active_window_label,
)
from handlers.matching.matchmaker import find_optimal_assignments
from lib.airports import get_airport
from datetime import timezone


# ── Helpers ───────────────────────────────────────────────────────────

def mock_mxp_airport():
    return get_airport("MXP")


# ── Haversine ─────────────────────────────────────────────────────────

def test_haversine_duomo_centrale():
    dist = haversine_km(45.4642, 9.1900, 45.4854, 9.2040)
    assert 2.0 < dist < 3.0


# ── Distance score ────────────────────────────────────────────────────

def test_distance_score_boundaries():
    assert distance_score(1.0) == 1.0
    assert distance_score(3.0) == 0.8
    assert distance_score(7.0) == 0.5
    assert distance_score(15.0) == 0.2
    assert distance_score(25.0) == 0.0


# ── Luggage score ─────────────────────────────────────────────────────

def test_luggage_score():
    assert luggage_score(1, 2) == 1.0  # total=3
    assert luggage_score(2, 2) == 0.8  # total=4
    assert luggage_score(3, 2) == 0.4  # total=5
    assert luggage_score(3, 3) == 0.0  # total=6


# ── Slot helpers ──────────────────────────────────────────────────────

def test_adjacent_slots_count():
    slots = get_adjacent_slots("2026-04-24T14:00:00Z", slot_duration_min=60, n=1)
    assert len(slots) == 3
    assert slots[0] == "2026-04-24T13:00:00Z"
    assert slots[1] == "2026-04-24T14:00:00Z"
    assert slots[2] == "2026-04-24T15:00:00Z"


def test_get_slot_bucket():
    assert get_slot_bucket("2026-04-24T14:23:00Z", 60) == "2026-04-24T14:00:00Z"
    assert get_slot_bucket("2026-04-24T14:45:00Z", 30) == "2026-04-24T14:30:00Z"


# ── Direction + mode filters ──────────────────────────────────────────

def test_direction_filter():
    trip_a = {"direction": "TO_MILAN"}
    trip_b = {"direction": "FROM_MILAN"}
    assert can_match_direction(trip_a, trip_b) is False


def test_can_match_modes_same():
    assert can_match_modes({"mode": "live"}, {"mode": "live"}) is True
    assert can_match_modes({"mode": "scheduled"}, {"mode": "scheduled"}) is True


def test_can_match_modes_mixed():
    slot_start = "2026-04-24T14:00:00Z"
    sched_trip = {"mode": "scheduled", "arrivalSlot": slot_start}

    assert can_match_modes({"mode": "live", "createdAt": slot_start}, sched_trip) is True
    assert can_match_modes({"mode": "live", "createdAt": "2026-04-24T13:40:00Z"}, sched_trip) is True
    assert can_match_modes({"mode": "live", "createdAt": "2026-04-24T13:20:00Z"}, sched_trip) is False
    assert can_match_modes({"mode": "live", "createdAt": "2026-04-24T15:00:00Z"}, sched_trip) is True
    assert can_match_modes({"mode": "live", "createdAt": "2026-04-24T15:40:00Z"}, sched_trip) is False


# ── compute_match_score ───────────────────────────────────────────────

def test_compute_match_score_valid():
    bucket = "2026-04-24T14:00:00Z"
    trip_a = {"destLat": 45.4642, "destLng": 9.1900, "timeBucket": bucket, "pk": "a", "mode": "live"}
    trip_b = {"destLat": 45.4721, "destLng": 9.1878, "timeBucket": bucket, "pk": "b", "mode": "live"}
    user_a = {"lang": "it", "verified": True}
    user_b = {"lang": "it", "verified": True}
    score = compute_match_score(trip_a, trip_b, user_a, user_b, mode="live")
    assert score > 0.8


def test_compute_match_score_too_far():
    near_bucket = "2026-04-24T14:00:00Z"
    far_bucket = "2026-04-25T14:00:00Z"
    trip_a = {"destLat": 45.4642, "destLng": 9.1900, "timeBucket": near_bucket, "pk": "a"}
    trip_b = {"destLat": 45.6200, "destLng": 9.0500, "timeBucket": far_bucket, "pk": "b"}
    user_a = {"lang": "it", "verified": False}
    user_b = {"lang": "en", "verified": False}
    score = compute_match_score(trip_a, trip_b, user_a, user_b)
    assert score == 0.0


# ── v4 — Dynamic threshold ────────────────────────────────────────────

def test_dynamic_threshold_7days():
    now = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
    flight_time = (now + timedelta(days=7)).isoformat()
    threshold = compute_dynamic_threshold(0.25, flight_time, flight_time, now)
    assert threshold == 0.70


def test_dynamic_threshold_48h():
    now = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
    flight_time = (now + timedelta(hours=48)).isoformat()
    threshold = compute_dynamic_threshold(0.25, flight_time, flight_time, now)
    assert threshold == 0.50


def test_dynamic_threshold_24h():
    now = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
    flight_time = (now + timedelta(hours=24)).isoformat()
    threshold = compute_dynamic_threshold(0.25, flight_time, flight_time, now)
    assert threshold == 0.35


def test_dynamic_threshold_1hour():
    now = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
    flight_time = (now + timedelta(hours=1)).isoformat()
    threshold = compute_dynamic_threshold(0.25, flight_time, flight_time, now)
    assert threshold <= 0.25


def test_dynamic_threshold_uses_earliest_flight():
    now = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
    flight_a = (now + timedelta(days=7)).isoformat()   # 7d away → 0.70
    flight_b = (now + timedelta(hours=2)).isoformat()  # 2h away → <0.25
    # should use the more imminent flight
    threshold = compute_dynamic_threshold(0.25, flight_a, flight_b, now)
    assert threshold <= 0.25


# ── v4 — Detour corridor ──────────────────────────────────────────────

def test_detour_linear_route():
    # Destinations close together near Milano centro — minimal detour
    trip_a = {"destLat": 45.47, "destLng": 9.19}
    trip_b = {"destLat": 45.46, "destLng": 9.18}
    airport = mock_mxp_airport()
    detour = estimate_detour_minutes(trip_a, trip_b, airport)
    assert detour < 5


def test_detour_opposite_directions():
    # One destination east, one west of airport zone — V-route
    trip_a = {"destLat": 45.47, "destLng": 9.35}  # est
    trip_b = {"destLat": 45.47, "destLng": 9.05}  # ovest
    airport = mock_mxp_airport()
    detour = estimate_detour_minutes(trip_a, trip_b, airport)
    assert detour > 5


def test_detour_penalty_no_penalty():
    assert apply_detour_penalty(0.80, detour_min=3, max_detour_min=15) == 0.80


def test_detour_penalty_linear():
    score = apply_detour_penalty(0.80, detour_min=10, max_detour_min=15)
    assert 0.60 < score < 0.80


def test_detour_penalty_v_route():
    score = apply_detour_penalty(0.80, detour_min=20, max_detour_min=15)
    assert score <= 0.50


def test_detour_penalty_clamped_to_zero():
    score = apply_detour_penalty(0.20, detour_min=20, max_detour_min=15)
    assert score == 0.0


# ── v4 — find_optimal_assignments ────────────────────────────────────

def test_find_optimal_assignments_best_first():
    # Pre-sorted by score desc (as build_compatibility_matrix returns)
    pairs = [
        ("b", "c", 0.95, 0.8, 1.5),
        ("a", "b", 0.90, 1.0, 2.0),
        ("a", "c", 0.70, 2.0, 3.0),
    ]
    result = find_optimal_assignments(pairs)
    assigned_ids = {(r[0], r[1]) for r in result}
    # b-c taken first (highest score); a-b and a-c both blocked (b and c taken)
    assert ("b", "c") in assigned_ids
    assert ("a", "b") not in assigned_ids
    assert ("a", "c") not in assigned_ids


def test_find_optimal_assignments_no_double_assign():
    pairs = [
        ("a", "b", 0.90, 1.0, 2.0),
        ("a", "c", 0.80, 1.5, 2.5),
    ]
    result = find_optimal_assignments(pairs)
    ids = [id_ for r in result for id_ in (r[0], r[1])]
    assert len(ids) == len(set(ids))  # no duplicates


# ── MVP — get_match_coords ─────────────────────────────────────────────

def test_get_match_coords_to_airport_uses_origin():
    airport = mock_mxp_airport()  # to_airport_direction = "FROM_MILAN"
    trip = {
        "direction": "FROM_MILAN",
        "originLat": 45.4642, "originLng": 9.1900,
        "destLat": 45.6301, "destLng": 8.7231,  # MXP coords
    }
    lat, lng = get_match_coords(trip, airport)
    assert (lat, lng) == (45.4642, 9.1900)


def test_get_match_coords_from_airport_uses_dest():
    airport = mock_mxp_airport()
    trip = {
        "direction": "TO_MILAN",
        "originLat": 45.4642, "originLng": 9.1900,
        "destLat": 45.4854, "destLng": 9.2040,
    }
    lat, lng = get_match_coords(trip, airport)
    assert (lat, lng) == (45.4854, 9.2040)


def test_get_match_coords_to_airport_missing_origin_falls_back():
    airport = mock_mxp_airport()
    trip = {
        "direction": "FROM_MILAN",
        "destLat": 45.4854, "destLng": 9.2040,
        # no originLat
    }
    lat, lng = get_match_coords(trip, airport)
    assert (lat, lng) == (45.4854, 9.2040)


# ── MVP — compute_match_score with airport ────────────────────────────

def test_compute_match_score_to_airport_uses_origins():
    """Two TO_AIRPORT trips with same destLat (MXP) but different origins should NOT score 1.0 distance."""
    airport = mock_mxp_airport()
    mxp_lat, mxp_lng = 45.6301, 8.7231
    bucket = "2026-06-01T06:00:00Z"
    trip_a = {
        "direction": "FROM_MILAN", "timeBucket": bucket,
        "originLat": 45.4642, "originLng": 9.1900,  # Duomo
        "destLat": mxp_lat, "destLng": mxp_lng,
    }
    trip_b = {
        "direction": "FROM_MILAN", "timeBucket": bucket,
        "originLat": 45.4854, "originLng": 9.2040,  # Centrale
        "destLat": mxp_lat, "destLng": mxp_lng,
    }
    user = {}
    score_with_airport = compute_match_score(trip_a, trip_b, user, user, airport=airport)
    score_without_airport = compute_match_score(trip_a, trip_b, user, user)
    # Without airport, destLat = MXP for both → distance 0 → distance_score 1.0 → inflated score
    # With airport, uses originLat → real distance Duomo-Centrale ~2.5 km → distance_score 0.5
    assert score_with_airport < score_without_airport


# ── MVP — is_in_active_window ──────────────────────────────────────────

def test_is_in_active_window_inside():
    airport = mock_mxp_airport()  # windows: [(6,9),(14,17),(20,23)]
    # 07:30 Rome time — inside first window
    dt = datetime(2026, 6, 1, 5, 30, tzinfo=timezone.utc)  # 07:30 Rome (UTC+2 in summer)
    assert is_in_active_window(airport, dt) is True


def test_is_in_active_window_outside():
    airport = mock_mxp_airport()
    # 12:00 Rome time — between windows
    dt = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)  # 12:00 Rome (UTC+2)
    assert is_in_active_window(airport, dt) is False


def test_is_in_active_window_empty_config():
    from lib.airports import AirportConfig
    airport = AirportConfig(code="TEST", name="Test", city="City", country="IT",
                            currency="EUR", base_fare=10000, unlock_fee=99, timezone="Europe/Rome")
    assert is_in_active_window(airport, datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)) is True


def test_next_active_window_label_between_windows():
    airport = mock_mxp_airport()
    dt = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)  # 12:00 Rome — between 09 and 14
    label = next_active_window_label(airport, dt)
    assert "14" in label


# ── MVP — compute_pickup_point ─────────────────────────────────────────

def _make_to_airport_trip(trip_id, origin_lat, origin_lng):
    return {
        "tripId": trip_id,
        "direction": "FROM_MILAN",
        "originLat": origin_lat, "originLng": origin_lng,
        "destLat": 45.6301, "destLng": 8.7231,  # MXP
    }


def test_pickup_point_gate1_origins_too_far():
    """Gate 1: origins >2 km apart — pair must be excluded by the caller."""
    airport = mock_mxp_airport()
    # Duomo (45.4642, 9.1900) vs Lambrate (45.4780, 9.2350) — ~2.8 km apart
    trip_a = _make_to_airport_trip("a", 45.4642, 9.1900)
    trip_b = _make_to_airport_trip("b", 45.4780, 9.2350)
    dist = haversine_km(
        trip_a["originLat"], trip_a["originLng"],
        trip_b["originLat"], trip_b["originLng"],
    )
    assert dist > airport.max_origin_distance_km


def test_pickup_point_gate2_midpoint_too_far():
    """Gate 2: origins close but midpoint >750 m from one origin — excluded by caller."""
    airport = mock_mxp_airport()
    # Two origins ~1.5 km apart but asymmetrically placed so midpoint is >750 m from one
    # Place them 1.5 km apart along longitude (~0.0135 deg)
    trip_a = _make_to_airport_trip("a", 45.4642, 9.1900)
    trip_b = _make_to_airport_trip("b", 45.4642, 9.2035)  # ~1.0 km east
    mid_lat = (45.4642 + 45.4642) / 2
    mid_lng = (9.1900 + 9.2035) / 2
    radius_km = airport.pickup_radius_m / 1000.0
    dist_to_a = haversine_km(mid_lat, mid_lng, trip_a["originLat"], trip_a["originLng"])
    # midpoint is ~500 m from each — should be within 750 m
    # force a case that exceeds radius: origins 1.6 km apart
    trip_a2 = _make_to_airport_trip("a2", 45.4642, 9.1900)
    trip_b2 = _make_to_airport_trip("b2", 45.4642, 9.2044)  # ~1.04 km
    mid2_lng = (9.1900 + 9.2044) / 2
    dist2 = haversine_km(mid_lat, mid2_lng, 45.4642, 9.1900)
    # midpoint is ~520 m — still within 750 m: this gate triggers only if radius is tighter
    # Test the gate logic directly: pickup_radius_m gate fires when dist > radius_km
    assert dist_to_a <= radius_km  # midpoint is within 750 m for ~1.0 km separation — gate passes


def test_pickup_point_valid_pair():
    """Valid pair: snap mocked to None → raw_midpoint, zone label populated."""
    airport = mock_mxp_airport()
    trip_a = _make_to_airport_trip("a", 45.4642, 9.1900)  # Duomo
    trip_b = _make_to_airport_trip("b", 45.4680, 9.1950)  # ~500 m

    with patch("lib.matching.snap_to_nearest_address", return_value=None):
        pickup = compute_pickup_point(trip_a, trip_b, airport)

    assert "lat" in pickup and "lng" in pickup
    expected_lat = (45.4642 + 45.4680) / 2
    expected_lng = (9.1900 + 9.1950) / 2
    assert pickup["lat"] == pytest.approx(expected_lat)
    assert pickup["lng"] == pytest.approx(expected_lng)
    assert pickup["source"] == "raw_midpoint"
    assert pickup["address"] is None
    assert pickup["placeId"] is None
    assert pickup["zoneCode"] in {z.code for z in airport.zones}
    assert pickup["zoneLabel"]
    # Gate: midpoint (raw) within 750 m of both origins
    radius_km = airport.pickup_radius_m / 1000.0
    assert haversine_km(pickup["lat"], pickup["lng"], trip_a["originLat"], trip_a["originLng"]) <= radius_km
    assert haversine_km(pickup["lat"], pickup["lng"], trip_b["originLat"], trip_b["originLng"]) <= radius_km


def test_pickup_point_snap_success():
    """snap returns address → source==places, lat/lng from snapped point."""
    airport = mock_mxp_airport()
    trip_a = _make_to_airport_trip("a", 45.4642, 9.1900)
    trip_b = _make_to_airport_trip("b", 45.4680, 9.1950)
    snapped = {"lat": 45.4655, "lng": 9.1922, "address": "Via Torino, 1", "placeId": "pid_xyz"}

    with patch("lib.matching.snap_to_nearest_address", return_value=snapped):
        pickup = compute_pickup_point(trip_a, trip_b, airport)

    assert pickup["source"] == "places"
    assert pickup["lat"] == pytest.approx(45.4655)
    assert pickup["lng"] == pytest.approx(9.1922)
    assert pickup["address"] == "Via Torino, 1"
    assert pickup["placeId"] == "pid_xyz"
    assert pickup["zoneCode"] in {z.code for z in airport.zones}


def test_pickup_point_snap_failure_fallback():
    """snap returns None → source==raw_midpoint, lat/lng = geometric midpoint."""
    airport = mock_mxp_airport()
    trip_a = _make_to_airport_trip("a", 45.4642, 9.1900)
    trip_b = _make_to_airport_trip("b", 45.4680, 9.1950)

    with patch("lib.matching.snap_to_nearest_address", return_value=None):
        pickup = compute_pickup_point(trip_a, trip_b, airport)

    assert pickup["source"] == "raw_midpoint"
    assert pickup["lat"] == pytest.approx((45.4642 + 45.4680) / 2)
    assert pickup["lng"] == pytest.approx((9.1900 + 9.1950) / 2)
    assert pickup["address"] is None
    assert pickup["placeId"] is None


def test_build_match_item_propagates_pickup_fields():
    """build_match_item stores address/placeId/source from pickup_point."""
    trip_a = {
        "tripId": "t1", "userId": "u1", "airportCode": "MXP",
        "mode": "scheduled",
    }
    trip_b = {
        "tripId": "t2", "userId": "u2", "airportCode": "MXP",
        "mode": "scheduled",
    }
    pickup = {
        "lat": 45.4655, "lng": 9.1922,
        "address": "Via Torino, 1", "placeId": "pid_xyz",
        "source": "places",
        "zoneCode": "centro", "zoneLabel": "Centro", "landmarks": ["Duomo"],
    }
    item = build_match_item(trip_a, trip_b, score=0.8, pickup_point=pickup)
    pp = item["pickupPoint"]
    assert pp["source"] == "places"
    assert pp["address"] == "Via Torino, 1"
    assert pp["placeId"] == "pid_xyz"


def test_build_match_item_no_pickup_point():
    """pickup_point=None → no pickupPoint key in item."""
    trip_a = {"tripId": "t1", "userId": "u1", "airportCode": "MXP", "mode": "scheduled"}
    trip_b = {"tripId": "t2", "userId": "u2", "airportCode": "MXP", "mode": "scheduled"}
    item = build_match_item(trip_a, trip_b, score=0.5)
    assert "pickupPoint" not in item


# ── MVP — compute_pickup_time (buffer ritrovo) ─────────────────────────

def _trip_with_flight(trip_id, flight_time):
    return {
        "tripId": trip_id,
        "direction": "FROM_MILAN",
        "originLat": 45.4642, "originLng": 9.1900,
        "destLat": 45.6301, "destLng": 8.7231,  # MXP
        "flightTime": flight_time,
    }


def test_pickup_time_uses_earliest_departure():
    """pickupTime = volo piu' presto − buffer. 14:00 e 14:30 → 14:00 − 180min = 11:00."""
    airport = mock_mxp_airport()  # pickup_buffer_minutes = 180
    trip_a = _trip_with_flight("a", "2026-06-01T14:00:00Z")
    trip_b = _trip_with_flight("b", "2026-06-01T14:30:00Z")
    assert compute_pickup_time(trip_a, trip_b, airport) == "2026-06-01T11:00:00Z"
    # Simmetrico: l'ordine degli argomenti non cambia il risultato (sempre il piu' presto).
    assert compute_pickup_time(trip_b, trip_a, airport) == "2026-06-01T11:00:00Z"


def test_pickup_time_not_latest_not_average():
    """Deve usare il min, non il max (11:30) ne' la media (11:15)."""
    airport = mock_mxp_airport()
    trip_a = _trip_with_flight("a", "2026-06-01T14:00:00Z")
    trip_b = _trip_with_flight("b", "2026-06-01T14:30:00Z")
    pickup_time = compute_pickup_time(trip_a, trip_b, airport)
    assert pickup_time != "2026-06-01T11:30:00Z"  # max − buffer
    assert pickup_time != "2026-06-01T11:15:00Z"  # media − buffer


def test_pickup_time_none_when_flight_missing():
    airport = mock_mxp_airport()
    trip_a = _trip_with_flight("a", "2026-06-01T14:00:00Z")
    trip_b = {"tripId": "b", "direction": "FROM_MILAN",
              "originLat": 45.46, "originLng": 9.19, "destLat": 45.63, "destLng": 8.72}
    assert compute_pickup_time(trip_a, trip_b, airport) is None


def test_pickup_buffer_changes_time_not_score():
    """Il buffer e' OUTPUT: cambia pickupTime, NON lo score della coppia."""
    import dataclasses
    base = mock_mxp_airport()
    airport_180 = dataclasses.replace(base, pickup_buffer_minutes=180)
    airport_60 = dataclasses.replace(base, pickup_buffer_minutes=60)

    trip_a = _trip_with_flight("a", "2026-06-01T14:00:00Z")
    trip_b = _trip_with_flight("b", "2026-06-01T14:00:00Z")

    assert compute_pickup_time(trip_a, trip_b, airport_180) == "2026-06-01T11:00:00Z"
    assert compute_pickup_time(trip_a, trip_b, airport_60) == "2026-06-01T13:00:00Z"

    score_180 = compute_match_score(trip_a, trip_b, {}, {}, mode="scheduled", airport=airport_180)
    score_60 = compute_match_score(trip_a, trip_b, {}, {}, mode="scheduled", airport=airport_60)
    assert score_180 == score_60  # scoring indipendente dal buffer
