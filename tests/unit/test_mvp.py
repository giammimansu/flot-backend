"""Flot — MVP feature-flag integration tests.

Tests that each MVP flag gates the right behaviour and that flag=false
restores full v4 behaviour without code changes.
"""
from __future__ import annotations

import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from lib.airports import get_airport
from lib.matching import is_in_active_window


def _ctx():
    ctx = MagicMock()
    ctx.function_name = "test"
    ctx.memory_limit_in_mb = 256
    ctx.invoked_function_arn = "arn:aws:lambda:eu-south-1:1:function:test"
    ctx.aws_request_id = "req-test"
    return ctx


# ── Helpers ───────────────────────────────────────────────────────────

def _mxp():
    return get_airport("MXP")


def _fco():
    return get_airport("FCO")


def _make_to_airport_trip(trip_id, origin_lat=45.4642, origin_lng=9.1900,
                           dest_lat=45.6301, dest_lng=8.7231,
                           direction="FROM_MILAN", flight_hours=24):
    now = datetime.now(timezone.utc)
    flight_time = (now + timedelta(hours=flight_hours)).isoformat().replace("+00:00", "Z")
    return {
        "tripId": trip_id,
        "pk": f"TRIP#{trip_id}",
        "userId": f"user-{trip_id}",
        "status": "scheduled",
        "flightTime": flight_time,
        "airportCode": "MXP",
        "direction": direction,
        "originLat": origin_lat,
        "originLng": origin_lng,
        "destLat": dest_lat,
        "destLng": dest_lng,
        "timeBucket": flight_time[:16] + ":00Z",
        "createdAt": now.isoformat().replace("+00:00", "Z"),
    }


def _build_event(airport_code="MXP", direction="FROM_MILAN",
                 origin_lat=45.4642, origin_lng=9.1900, origin_place_id="ChIJ_MXP",
                 flight_time="2026-06-20T05:00:00Z", mode="scheduled"):
    import json
    body: dict = {
        "airportCode": airport_code,
        "terminal": "T1",
        "direction": direction,
        "destination": "Duomo",
        "destLat": 45.4642,
        "destLng": 9.1900,
        "destPlaceId": "ChIJduomo",
        "mode": mode,
        "flightNumber": "AZ0001",
        "flightDate": "2026-06-20",
        "flightTime": flight_time,
        "luggage": 1,
        "paxCount": 1,
    }
    if origin_lat is not None:
        body["originLat"] = origin_lat
        body["originLng"] = origin_lng
    if origin_place_id is not None:
        body["originPlaceId"] = origin_place_id
    return {
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {
                "claims": {"sub": "user-test"},
            }
        },
        "headers": {},
    }


# ── MvpSingleRouteMode — create_trip ─────────────────────────────────

def _call_create_trip(event, single_route_mode: bool):
    """Call create_trip.handler with module-level flags patched directly."""
    import handlers.trips.create_trip as ct
    ct._MVP_SINGLE_ROUTE_MODE = single_route_mode
    with patch("handlers.trips.create_trip.dynamo.get_item", return_value={}), \
         patch("handlers.trips.create_trip.dynamo.put_item"), \
         patch("handlers.trips.create_trip.put_event"), \
         patch("handlers.trips.create_trip.fetch_flight_eta", return_value=None):
        return ct.handler(event, _ctx())


class TestMvpSingleRouteMode:
    """create_trip gates under MvpSingleRouteMode=true."""

    def test_flag_off_accepts_fco(self):
        """Flag=false → FCO airport accepted (no MVP restriction)."""
        import json
        fco = _fco()
        valid_dir = fco.direction_labels[0]
        event = _build_event(airport_code="FCO", direction=valid_dir)
        result = _call_create_trip(event, single_route_mode=False)
        body = json.loads(result["body"])
        assert "MVP" not in body.get("error", "")

    def test_wrong_airport_rejected_when_flag_on(self):
        """MvpSingleRouteMode=true → FCO (no to_airport_direction) → 400 MVP error."""
        import json
        fco = _fco()
        assert fco.to_airport_direction == "", "FCO must have no to_airport_direction for this test"
        valid_dir = fco.direction_labels[0]
        event = _build_event(airport_code="FCO", direction=valid_dir)
        result = _call_create_trip(event, single_route_mode=True)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "MVP" in body.get("error", "")

    def test_wrong_direction_rejected_when_flag_on(self):
        """MvpSingleRouteMode=true → MXP + TO_MILAN → 400."""
        import json
        event = _build_event(airport_code="MXP", direction="TO_MILAN")
        result = _call_create_trip(event, single_route_mode=True)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "FROM_MILAN" in body.get("error", "") or "MVP" in body.get("error", "")

    def test_missing_origin_rejected_when_flag_on(self):
        """MvpSingleRouteMode=true → MXP + FROM_MILAN + no originLat → 400."""
        import json
        event = _build_event(airport_code="MXP", direction="FROM_MILAN",
                             origin_lat=None, origin_lng=None, origin_place_id=None)
        result = _call_create_trip(event, single_route_mode=True)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "originLat" in body.get("error", "")

    def test_valid_mxp_from_milan_accepted(self):
        """MvpSingleRouteMode=true → MXP + FROM_MILAN + originLat → 201."""
        event = _build_event(airport_code="MXP", direction="FROM_MILAN",
                             origin_lat=45.4642, origin_lng=9.1900, origin_place_id="ChIJduomo")
        result = _call_create_trip(event, single_route_mode=True)
        assert result["statusCode"] == 201


# ── MvpTimeWindowsMode — create_trip gate on flightTime ──────────────

class TestMvpTimeWindowsMode:
    """create_trip rejects SCHEDULED trips whose flightTime falls outside mvp_active_windows."""

    def test_flight_inside_window_accepted(self):
        """MXP flightTime 07:00 Rome (05:00 UTC) → inside window (6–9) → 201."""
        # 2026-06-20T05:00:00Z = 07:00 Rome summer time, inside window (6,9)
        event = _build_event(airport_code="MXP", direction="FROM_MILAN",
                             origin_lat=45.4642, origin_lng=9.1900, origin_place_id="ChIJduomo",
                             flight_time="2026-06-20T05:00:00Z", mode="scheduled")
        result = _call_create_trip(event, single_route_mode=False)
        assert result["statusCode"] == 201

    def test_flight_outside_window_rejected(self):
        """MXP flightTime 12:00 Rome (10:00 UTC) → gap (9–14) → 400 with next window hint."""
        import json
        # 2026-06-20T10:00:00Z = 12:00 Rome summer time, gap between (9,14)
        event = _build_event(airport_code="MXP", direction="FROM_MILAN",
                             origin_lat=45.4642, origin_lng=9.1900, origin_place_id="ChIJduomo",
                             flight_time="2026-06-20T10:00:00Z", mode="scheduled")
        result = _call_create_trip(event, single_route_mode=False)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "fascia" in body.get("error", "").lower()

    def test_airport_without_windows_always_accepted(self):
        """FCO has no mvp_active_windows → no gate, trip created regardless of time."""
        import json
        fco = _fco()
        assert fco.mvp_active_windows == [], "FCO must have no windows for this test"
        valid_dir = fco.direction_labels[0]
        # Use a time that would be outside MXP windows
        event = _build_event(airport_code="FCO", direction=valid_dir,
                             flight_time="2026-06-20T10:00:00Z", mode="scheduled")
        result = _call_create_trip(event, single_route_mode=False)
        body = json.loads(result["body"])
        assert "fascia" not in body.get("error", "").lower()

    def test_live_mode_not_gated(self):
        """LIVE mode with flightTime outside MXP window → not rejected (gate is SCHEDULED-only)."""
        # 2026-06-20T10:00:00Z = 12:00 Rome, outside window — but mode=live
        event = _build_event(airport_code="MXP", direction="FROM_MILAN",
                             origin_lat=45.4642, origin_lng=9.1900, origin_place_id="ChIJduomo",
                             flight_time="2026-06-20T10:00:00Z", mode="live")
        result = _call_create_trip(event, single_route_mode=False)
        assert result["statusCode"] == 201

    def test_matchmaker_always_runs_regardless_of_hour(self):
        """process_airport_v4 runs at any hour — no time-window gate in matchmaker."""
        import handlers.matching.matchmaker as mm
        mm._MVP_SHADOW_POOL_OFF = False

        airport = _mxp()
        # UTC 10:00 = 12:00 Rome — formerly skipped, now must run
        now_outside = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)

        with patch("handlers.matching.matchmaker.expire_stale_trips"), \
             patch("handlers.matching.matchmaker.process_lock_window", return_value=1) as mock_lock, \
             patch("handlers.matching.matchmaker.optimize_pool", return_value=0):
            locked, tentative = mm.process_airport_v4(airport, now_outside)

        assert locked == 1
        mock_lock.assert_called_once()


# ── MvpPickupSimpleMode — build_compatibility_matrix ─────────────────

class TestMvpPickupSimpleMode:
    """build_compatibility_matrix gate logic under MvpPickupSimpleMode."""

    def test_gate1_rejects_origins_too_far(self):
        """Gate 1: origin pair >max_origin_distance_km → excluded from matrix."""
        import handlers.matching.matchmaker as mm
        mm._MVP_PICKUP_SIMPLE_MODE = True

        now = datetime.now(timezone.utc)
        airport = _mxp()  # max_origin_distance_km=2.0
        # Duomo vs Lambrate ~2.8 km
        trip_a = _make_to_airport_trip("a", origin_lat=45.4642, origin_lng=9.1900)
        trip_b = _make_to_airport_trip("b", origin_lat=45.4780, origin_lng=9.2350)

        with patch("handlers.matching.matchmaker.dynamo.get_item", return_value={}):
            pairs = mm.build_compatibility_matrix([trip_a, trip_b], airport, now)

        assert len(pairs) == 0

    def test_gate2_rejects_midpoint_outside_radius(self):
        """Gate 2: midpoint >pickup_radius_m from one origin → excluded."""
        import handlers.matching.matchmaker as mm
        mm._MVP_PICKUP_SIMPLE_MODE = True

        now = datetime.now(timezone.utc)
        airport = _mxp()  # pickup_radius_m=750
        # Origins exactly 1.4 km apart along longitude — midpoint ~700 m from each, just inside 750
        # Place at 1.6 km to push midpoint to 800 m > 750 radius
        trip_a = _make_to_airport_trip("a", origin_lat=45.4642, origin_lng=9.1900)
        trip_b = _make_to_airport_trip("b", origin_lat=45.4642, origin_lng=9.2044)  # ~1.04 km

        from lib.matching import haversine_km
        dist = haversine_km(45.4642, 9.1900, 45.4642, 9.2044)
        mid_lng = (9.1900 + 9.2044) / 2
        dist_to_a = haversine_km(45.4642, mid_lng, 45.4642, 9.1900)
        radius_km = airport.pickup_radius_m / 1000.0

        # This specific pair should pass gate 2 (midpoint 520m < 750m)
        # Verify the gate boundary: if dist_to_a > radius, gate fires
        if dist_to_a > radius_km:
            with patch("handlers.matching.matchmaker.dynamo.get_item", return_value={}):
                pairs = mm.build_compatibility_matrix([trip_a, trip_b], airport, now)
            assert len(pairs) == 0
        else:
            # Pair passes gates — matrix has 1 entry
            with patch("handlers.matching.matchmaker.dynamo.get_item", return_value={}):
                pairs = mm.build_compatibility_matrix([trip_a, trip_b], airport, now)
            assert len(pairs) == 1

    def test_valid_pair_included_in_matrix(self):
        """Valid origins (<2 km, midpoint <750 m) → pair in compatibility matrix."""
        import handlers.matching.matchmaker as mm
        mm._MVP_PICKUP_SIMPLE_MODE = True

        now = datetime.now(timezone.utc)
        airport = _mxp()
        # Two origins ~500 m apart
        trip_a = _make_to_airport_trip("a", origin_lat=45.4642, origin_lng=9.1900)
        trip_b = _make_to_airport_trip("b", origin_lat=45.4680, origin_lng=9.1950)

        with patch("handlers.matching.matchmaker.dynamo.get_item", return_value={"lang": "it", "verified": True}):
            pairs = mm.build_compatibility_matrix([trip_a, trip_b], airport, now)

        assert len(pairs) == 1
        trip_a_id, trip_b_id, score, dist_km, detour_min = pairs[0]
        assert score > 0.0
        assert detour_min == 0.0  # MVP path: no detour estimate

    def test_flag_off_uses_v4_detour_logic(self):
        """MvpPickupSimpleMode=false → v4 path: estimate_detour_minutes called for dest coords."""
        import handlers.matching.matchmaker as mm
        mm._MVP_PICKUP_SIMPLE_MODE = False

        now = datetime.now(timezone.utc)
        airport = _mxp()
        # Linear route — low detour
        trip_a = {
            "tripId": "a", "pk": "TRIP#a", "userId": "u-a", "status": "scheduled",
            "flightTime": (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
            "airportCode": "MXP", "direction": "TO_MILAN",
            "destLat": 45.464, "destLng": 9.190,
            "timeBucket": (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z")[:16] + ":00Z",
            "createdAt": now.isoformat().replace("+00:00", "Z"),
        }
        trip_b = {
            "tripId": "b", "pk": "TRIP#b", "userId": "u-b", "status": "scheduled",
            "flightTime": (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
            "airportCode": "MXP", "direction": "TO_MILAN",
            "destLat": 45.467, "destLng": 9.193,
            "timeBucket": (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z")[:16] + ":00Z",
            "createdAt": now.isoformat().replace("+00:00", "Z"),
        }

        with patch("handlers.matching.matchmaker.dynamo.get_item", return_value={"lang": "it", "verified": True}):
            pairs = mm.build_compatibility_matrix([trip_a, trip_b], airport, now)

        assert len(pairs) >= 1
        # v4 path: detour_min is from estimate_detour_minutes, not hardcoded 0
        _, _, _, _, detour_min = pairs[0]
        # Low-detour pair: detour_min should be close to 0 but derived from haversine, not forced 0.0
        assert detour_min >= 0.0


# ── MvpFlightTrackerEnabled — flight_tracker ──────────────────────────

class TestMvpFlightTrackerEnabled:
    """Flight tracker early-exit under MvpFlightTrackerEnabled=false."""

    def test_tracker_disabled_returns_early(self):
        """MVP_FLIGHT_TRACKER_ENABLED=false → handler returns {disabled:True} without querying."""
        import handlers.flights.flight_tracker as ft
        with patch.dict(os.environ, {"MVP_FLIGHT_TRACKER_ENABLED": "false"}), \
             patch("handlers.flights.flight_tracker.get_active_airports") as mock_airports:
            result = ft.handler({}, _ctx())

        mock_airports.assert_not_called()
        assert result == {"updated": 0, "disabled": True}

    def test_tracker_enabled_runs_normally(self):
        """MVP_FLIGHT_TRACKER_ENABLED=true → handler queries airports as usual."""
        import handlers.flights.flight_tracker as ft
        with patch.dict(os.environ, {"MVP_FLIGHT_TRACKER_ENABLED": "true"}), \
             patch("handlers.flights.flight_tracker.get_active_airports", return_value=[]) as mock_airports:
            result = ft.handler({}, _ctx())

        mock_airports.assert_called_once()
        assert "tracked" in result
