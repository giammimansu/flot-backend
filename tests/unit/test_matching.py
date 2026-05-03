"""Flot — Unit tests for matching engine."""

import pytest
from datetime import datetime, timezone, timedelta
from lib.matching import (
    can_match_direction,
    can_match_modes,
    compute_dynamic_threshold,
    compute_match_score,
    distance_score,
    estimate_detour_minutes,
    apply_detour_penalty,
    luggage_score,
    get_adjacent_slots,
    get_slot_bucket,
    haversine_km,
)
from handlers.matching.matchmaker import find_optimal_assignments
from lib.airports import get_airport


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
