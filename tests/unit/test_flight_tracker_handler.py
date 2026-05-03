"""Flot — Sprint 5 tests: FlightTrackerFunction + on_flight_delayed."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, call

from lib.flight_tracker import FlightTrackerError
from lib.matching import check_time_compatibility
from lib.airports import get_airport


# ── Helpers ───────────────────────────────────────────────────────────

def _now():
    return datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _mxp():
    return get_airport("MXP")


def _make_trip(trip_id, status="scheduled", flight_hours=6, has_tentative=False):
    now = _now()
    flight_time = _iso(now + timedelta(hours=flight_hours))
    return {
        "pk": f"TRIP#{trip_id}",
        "sk": "META",
        "tripId": trip_id,
        "userId": f"user-{trip_id}",
        "airportCode": "MXP",
        "status": status,
        "flightNumber": "AZ1234",
        "flightDate": "2026-05-03",
        "flightTime": flight_time,
        "timeBucket": flight_time[:16] + ":00Z",
        "tentativeMatchId": "tm-1" if has_tentative else None,
        "gsi5pk": f"MXP#{status}",
        "createdAt": _iso(now),
    }


def _make_tm(tm_id, trip_id_1, trip_id_2):
    return {
        "pk": f"TENTATIVE_MATCH#{tm_id}",
        "sk": "META",
        "matchId": tm_id,
        "tripId1": trip_id_1,
        "tripId2": trip_id_2,
        "userId1": f"user-{trip_id_1}",
        "userId2": f"user-{trip_id_2}",
        "airportCode": "MXP",
        "status": "tentative_match",
        "score": 0.75,
    }


# ── check_time_compatibility ──────────────────────────────────────────

def test_check_time_compatible_within_window():
    now = _now()
    trip_a = {"flightTime": _iso(now + timedelta(hours=6))}
    trip_b = {"flightTime": _iso(now + timedelta(hours=6, minutes=30))}  # 30 min delta
    airport = _mxp()  # scheduled_match_window_min=60
    assert check_time_compatibility(trip_a, trip_b, airport) is True


def test_check_time_incompatible_outside_window():
    now = _now()
    trip_a = {"flightTime": _iso(now + timedelta(hours=6))}
    trip_b = {"flightTime": _iso(now + timedelta(hours=7, minutes=30))}  # 90 min delta
    airport = _mxp()
    assert check_time_compatibility(trip_a, trip_b, airport) is False


def test_check_time_exactly_at_boundary():
    now = _now()
    trip_a = {"flightTime": _iso(now + timedelta(hours=6))}
    trip_b = {"flightTime": _iso(now + timedelta(hours=7))}  # exactly 60 min
    airport = _mxp()
    assert check_time_compatibility(trip_a, trip_b, airport) is True


def test_check_time_missing_flight_time():
    trip_a = {"flightTime": None}
    trip_b = {"flightTime": _iso(_now() + timedelta(hours=6))}
    assert check_time_compatibility(trip_a, trip_b, _mxp()) is False


# ── FlightTrackerFunction handler ─────────────────────────────────────

def test_flight_tracker_updates_eta_on_delta():
    from handlers.flights.flight_tracker import _update_flight_eta

    now = _now()
    trip = _make_trip("a", flight_hours=6)
    # ETA shifted by 45 min (> MIN_DELTA_MIN=10)
    new_eta = datetime.fromisoformat(trip["flightTime"].replace("Z", "+00:00")) + timedelta(minutes=45)

    with patch("handlers.flights.flight_tracker.fetch_flight_eta", return_value=new_eta), \
         patch("handlers.flights.flight_tracker.dynamo.update_item") as mock_update, \
         patch("handlers.flights.flight_tracker.put_event") as mock_evt:

        result = _update_flight_eta(trip, now)

    assert result is True
    mock_update.assert_called_once()
    update_kwargs = mock_update.call_args[0]
    assert "flightTime" in update_kwargs[2]
    assert update_kwargs[2]["flightTime"] == new_eta.isoformat().replace("+00:00", "Z")


def test_flight_tracker_skips_small_delta():
    from handlers.flights.flight_tracker import _update_flight_eta

    now = _now()
    trip = _make_trip("a", flight_hours=6)
    # ETA only 3 min off (< MIN_DELTA_MIN=10)
    new_eta = datetime.fromisoformat(trip["flightTime"].replace("Z", "+00:00")) + timedelta(minutes=3)

    with patch("handlers.flights.flight_tracker.fetch_flight_eta", return_value=new_eta), \
         patch("handlers.flights.flight_tracker.dynamo.update_item") as mock_update:

        result = _update_flight_eta(trip, now)

    assert result is False
    mock_update.assert_not_called()


def test_flight_tracker_emits_delayed_for_tentative_trip():
    from handlers.flights.flight_tracker import _update_flight_eta

    now = _now()
    trip = _make_trip("a", status="tentative_match", flight_hours=6, has_tentative=True)
    new_eta = datetime.fromisoformat(trip["flightTime"].replace("Z", "+00:00")) + timedelta(minutes=30)

    with patch("handlers.flights.flight_tracker.fetch_flight_eta", return_value=new_eta), \
         patch("handlers.flights.flight_tracker.dynamo.update_item"), \
         patch("handlers.flights.flight_tracker.put_event") as mock_evt:

        _update_flight_eta(trip, now)

    mock_evt.assert_called_once()
    evt_name, evt_detail = mock_evt.call_args[0]
    assert evt_name == "flight.delayed"
    assert evt_detail["tripId"] == "a"
    assert evt_detail["matchId"] == "tm-1"
    assert evt_detail["deltaMinutes"] == 30.0


def test_flight_tracker_no_event_for_scheduled_trip():
    from handlers.flights.flight_tracker import _update_flight_eta

    now = _now()
    trip = _make_trip("a", status="scheduled", flight_hours=6)
    new_eta = datetime.fromisoformat(trip["flightTime"].replace("Z", "+00:00")) + timedelta(minutes=30)

    with patch("handlers.flights.flight_tracker.fetch_flight_eta", return_value=new_eta), \
         patch("handlers.flights.flight_tracker.dynamo.update_item"), \
         patch("handlers.flights.flight_tracker.put_event") as mock_evt:

        _update_flight_eta(trip, now)

    mock_evt.assert_not_called()


def test_flight_tracker_handles_api_error_silently():
    from handlers.flights.flight_tracker import _update_flight_eta

    now = _now()
    trip = _make_trip("a", flight_hours=6)

    with patch("handlers.flights.flight_tracker.fetch_flight_eta", side_effect=FlightTrackerError("timeout")), \
         patch("handlers.flights.flight_tracker.dynamo.update_item") as mock_update:

        result = _update_flight_eta(trip, now)

    assert result is False
    mock_update.assert_not_called()


def test_flight_tracker_skips_trip_without_flight_number():
    from handlers.flights.flight_tracker import _update_flight_eta

    now = _now()
    trip = _make_trip("a")
    trip.pop("flightNumber")

    with patch("handlers.flights.flight_tracker.fetch_flight_eta") as mock_fetch:
        result = _update_flight_eta(trip, now)

    assert result is False
    mock_fetch.assert_not_called()


# ── on_flight_delayed handler ─────────────────────────────────────────

def test_on_flight_delayed_invalidates_incompatible_match():
    from handlers.events.on_flight_delayed import handler

    now = _now()
    # trip_a at now+6h, trip_b at now+8h → 120 min delta → outside MXP 60 min window
    trip_a = _make_trip("a", status="tentative_match", flight_hours=6)
    trip_b = _make_trip("b", status="tentative_match", flight_hours=8)
    tm = _make_tm("tm-1", "a", "b")

    event = {
        "detail": {
            "tripId": "a",
            "matchId": "tm-1",
            "oldFlightTime": trip_a["flightTime"],
            "newFlightTime": trip_a["flightTime"],
            "deltaMinutes": 90.0,
        }
    }

    def fake_get_item(pk, sk):
        if "TENTATIVE_MATCH" in pk:
            return tm
        if "TRIP#a" in pk:
            return trip_a
        if "TRIP#b" in pk:
            return trip_b
        return None

    with patch("handlers.events.on_flight_delayed.dynamo.get_item", side_effect=fake_get_item), \
         patch("handlers.events.on_flight_delayed.dynamo.dissolve_tentative_match") as mock_dissolve, \
         patch("handlers.events.on_flight_delayed.put_event") as mock_evt:

        handler(event, MagicMock())

    mock_dissolve.assert_called_once_with("tm-1", trip_a, trip_b)
    mock_evt.assert_called_once()
    evt_name, evt_detail = mock_evt.call_args[0]
    assert evt_name == "match.invalidated"
    assert evt_detail["reason"] == "flight_delay"


def test_on_flight_delayed_keeps_compatible_match():
    from handlers.events.on_flight_delayed import handler

    now = _now()
    # Both trips 30 min apart — within 60 min window
    trip_a = _make_trip("a", status="tentative_match", flight_hours=6)
    trip_b = _make_trip("b", status="tentative_match", flight_hours=6)
    trip_b["flightTime"] = _iso(now + timedelta(hours=6, minutes=30))
    tm = _make_tm("tm-1", "a", "b")

    event = {
        "detail": {
            "tripId": "a",
            "matchId": "tm-1",
            "deltaMinutes": 15.0,
        }
    }

    def fake_get_item(pk, sk):
        if "TENTATIVE_MATCH" in pk:
            return tm
        if "TRIP#a" in pk:
            return trip_a
        if "TRIP#b" in pk:
            return trip_b
        return None

    with patch("handlers.events.on_flight_delayed.dynamo.get_item", side_effect=fake_get_item), \
         patch("handlers.events.on_flight_delayed.dynamo.dissolve_tentative_match") as mock_dissolve, \
         patch("handlers.events.on_flight_delayed.put_event") as mock_evt:

        handler(event, MagicMock())

    mock_dissolve.assert_not_called()
    mock_evt.assert_not_called()


def test_on_flight_delayed_no_match_id():
    from handlers.events.on_flight_delayed import handler

    event = {"detail": {"tripId": "a"}}

    with patch("handlers.events.on_flight_delayed.dynamo.get_item") as mock_get:
        handler(event, MagicMock())

    mock_get.assert_not_called()


def test_on_flight_delayed_match_already_confirmed():
    from handlers.events.on_flight_delayed import handler

    tm_confirmed = {**_make_tm("tm-1", "a", "b"), "status": "matched"}
    event = {"detail": {"tripId": "a", "matchId": "tm-1", "deltaMinutes": 60.0}}

    with patch("handlers.events.on_flight_delayed.dynamo.get_item", return_value=tm_confirmed), \
         patch("handlers.events.on_flight_delayed.dynamo.dissolve_tentative_match") as mock_dissolve:

        handler(event, MagicMock())

    mock_dissolve.assert_not_called()


# ── on_match_invalidated handler ──────────────────────────────────────

def test_on_match_invalidated_notifies_both_users():
    from handlers.events.on_match_invalidated import handler

    event = {
        "detail": {
            "matchId": "match-1",
            "userId1": "u1",
            "userId2": "u2",
            "reason": "flight_delay",
            "deltaMinutes": 90.0,
        }
    }

    def fake_get_item(pk, sk):
        return {"pushToken": f"token-{pk.split('#')[1]}", "email": "user@test.com"}

    with patch("handlers.events.on_match_invalidated.dynamo.get_item", side_effect=fake_get_item), \
         patch("handlers.events.on_match_invalidated.save_notification") as mock_save, \
         patch("handlers.events.on_match_invalidated.send_push_notification") as mock_push:

        handler(event, MagicMock())

    assert mock_save.call_count == 2
    assert mock_push.call_count == 2
    # Both users notified
    saved_users = {c[0][0] for c in mock_save.call_args_list}
    assert saved_users == {"u1", "u2"}


def test_on_match_invalidated_missing_fields():
    from handlers.events.on_match_invalidated import handler

    event = {"detail": {"matchId": "match-1"}}  # missing userId1, userId2

    with patch("handlers.events.on_match_invalidated.save_notification") as mock_save:
        handler(event, MagicMock())

    mock_save.assert_not_called()
