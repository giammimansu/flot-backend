"""Flot — Unit tests for matching engine."""

import pytest
from datetime import datetime, timezone, timedelta
from lib.matching import (
    can_match_direction,
    can_match_modes,
    compute_match_score,
    distance_score,
    luggage_score,
    get_adjacent_slots,
    get_slot_bucket,
    haversine_km,
)


def test_haversine_duomo_centrale():
    # Duomo → Centrale: ~2.6 km
    dist = haversine_km(45.4642, 9.1900, 45.4854, 9.2040)
    assert 2.0 < dist < 3.0


def test_distance_score_boundaries():
    assert distance_score(1.0) == 1.0
    assert distance_score(3.0) == 0.8
    assert distance_score(7.0) == 0.5
    assert distance_score(15.0) == 0.2
    assert distance_score(25.0) == 0.0


def test_luggage_score():
    assert luggage_score(1, 2) == 1.0   # 3
    assert luggage_score(2, 2) == 0.8   # 4
    assert luggage_score(3, 2) == 0.4   # 5
    assert luggage_score(3, 3) == 0.0   # 6


def test_compute_match_score_valid():
    trip_a = {"destLat": 45.4642, "destLng": 9.1900, "luggage": 1, "pk": "a", "mode": "live"}
    trip_b = {"destLat": 45.4721, "destLng": 9.1878, "luggage": 1, "pk": "b", "mode": "live"}
    user_a = {"lang": "it", "verified": True}
    user_b = {"lang": "it", "verified": True}
    # dist is ~1km -> d_score=1.0. luggage=2 -> 1.0. profile=0.2.
    # final = 0.6*1.0 + 0.2*1.0 + 0.2*0.2 = 0.84
    score = compute_match_score(trip_a, trip_b, user_a, user_b)
    assert score > 0.8


def test_compute_match_score_too_far():
    # Duomo vs out of milan
    trip_a = {"destLat": 45.4642, "destLng": 9.1900, "luggage": 4, "pk": "a"}
    trip_b = {"destLat": 45.6200, "destLng": 9.0500, "luggage": 4, "pk": "b"}
    user_a = {"lang": "it", "verified": False}
    user_b = {"lang": "en", "verified": False}
    # dist is large -> d_score=0.0. luggage=8 -> 0.0. profile=0.0.
    score = compute_match_score(trip_a, trip_b, user_a, user_b)
    assert score == 0.0


def test_adjacent_slots_count():
    slots = get_adjacent_slots("2026-04-24T14:00:00Z", slot_duration_min=60, n=1)
    assert len(slots) == 3  # -1, 0, +1
    assert slots[0] == "2026-04-24T13:00:00Z"
    assert slots[1] == "2026-04-24T14:00:00Z"
    assert slots[2] == "2026-04-24T15:00:00Z"


def test_get_slot_bucket():
    assert get_slot_bucket("2026-04-24T14:23:00Z", 60) == "2026-04-24T14:00:00Z"
    assert get_slot_bucket("2026-04-24T14:45:00Z", 30) == "2026-04-24T14:30:00Z"


def test_direction_filter():
    trip_a = {"direction": "TO_MILAN"}
    trip_b = {"direction": "FROM_MILAN"}
    assert can_match_direction(trip_a, trip_b) is False


def test_can_match_modes():
    # Same mode
    assert can_match_modes({"mode": "live"}, {"mode": "live"}) is True
    assert can_match_modes({"mode": "scheduled"}, {"mode": "scheduled"}) is True

    # Mixed mode
    slot_start = "2026-04-24T14:00:00Z"
    
    # Live created exactly at slot start
    live_trip = {"mode": "live", "createdAt": slot_start}
    sched_trip = {"mode": "scheduled", "arrivalSlot": slot_start}
    assert can_match_modes(live_trip, sched_trip) is True

    # Live created 20 mins before slot start (within 30m tolerance)
    live_20m_early = {"mode": "live", "createdAt": "2026-04-24T13:40:00Z"}
    assert can_match_modes(live_20m_early, sched_trip) is True

    # Live created 40 mins before slot start (outside 30m tolerance)
    live_40m_early = {"mode": "live", "createdAt": "2026-04-24T13:20:00Z"}
    assert can_match_modes(live_40m_early, sched_trip) is False

    # Live created 1 hour after slot start (so at slot end, still inside)
    live_1h_late = {"mode": "live", "createdAt": "2026-04-24T15:00:00Z"}
    assert can_match_modes(live_1h_late, sched_trip) is True

    # Live created 1h 40m after slot start
    live_1h40m_late = {"mode": "live", "createdAt": "2026-04-24T15:40:00Z"}
    assert can_match_modes(live_1h40m_late, sched_trip) is False
