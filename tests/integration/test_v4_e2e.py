"""Flot — Integration Test E2E for v4 Shadow Pool."""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from handlers.trips.create_trip import handler as create_trip_handler
from handlers.matching.matchmaker import handler as matchmaker_handler
from handlers.flights.flight_tracker import handler as flight_tracker_handler
from lib import dynamo


def test_v4_shadow_pool_e2e(dynamodb_table, lambda_context):
    """
    E2E flow:
    1. Create trip A and trip B with flight in 24 hours.
    2. flight_tracker resolves ETA (mocked) and puts them in scheduled.
    3. matchmaker creates a TentativeMatch.
    4. Fast forward time to T-2h (past lock window).
    5. matchmaker locks the match and notifies.
    """
    now_base = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    flight_date_str = "2026-06-02"
    flight_eta = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)  # 24h later

    # Create users
    dynamo.put_item({"pk": "USER#u1", "sk": "PROFILE", "email": "1@a.com", "lang": "it"})
    dynamo.put_item({"pk": "USER#u2", "sk": "PROFILE", "email": "2@a.com", "lang": "it"})

    # 1. Create Trip A
    with patch("handlers.trips.create_trip.datetime") as mock_dt, \
         patch("handlers.trips.create_trip.fetch_flight_eta", return_value=flight_eta):
        mock_dt.now.return_value = now_base
        mock_dt.fromisoformat = datetime.fromisoformat

        res_a = create_trip_handler({
            "httpMethod": "POST",
            "body": json.dumps({
                "airportCode": "MXP",
                "terminal": "T1",
                "direction": "FROM_AIRPORT",
                "destination": "Duomo, Milano",
                "destLat": 45.464,
                "destLng": 9.190,
                "destPlaceId": "place_1",
                "flightNumber": "AZ123",
                "flightDate": flight_date_str,
                "paxCount": 1,
                "luggage": 0,
            }),
            "requestContext": {"authorizer": {"claims": {"sub": "u1"}}}
        }, lambda_context)
    
    assert res_a["statusCode"] == 201
    trip_a = json.loads(res_a["body"])
    assert trip_a["status"] == "scheduled"
    
    # 2. Create Trip B
    with patch("handlers.trips.create_trip.datetime") as mock_dt, \
         patch("handlers.trips.create_trip.fetch_flight_eta", return_value=flight_eta):
        mock_dt.now.return_value = now_base
        mock_dt.fromisoformat = datetime.fromisoformat

        res_b = create_trip_handler({
            "httpMethod": "POST",
            "body": json.dumps({
                "airportCode": "MXP",
                "terminal": "T1",
                "direction": "FROM_AIRPORT",
                "destination": "Stazione Centrale, Milano",
                "destLat": 45.484,
                "destLng": 9.203,
                "destPlaceId": "place_2",
                "flightNumber": "AZ124",
                "flightDate": flight_date_str,
                "paxCount": 1,
                "luggage": 0,
            }),
            "requestContext": {"authorizer": {"claims": {"sub": "u2"}}}
        }, lambda_context)

    assert res_b["statusCode"] == 201
    trip_b = json.loads(res_b["body"])

    # 3. Matchmaker run 1: creates TentativeMatch
    with patch("handlers.matching.matchmaker.datetime") as mock_dt:
        mock_dt.now.return_value = now_base
        mock_dt.fromisoformat = datetime.fromisoformat
        
        matchmaker_handler({}, lambda_context)
    
    # Verify TentativeMatch created
    tm = dynamo.get_tentative_match_between(trip_a["tripId"], trip_b["tripId"])
    assert tm is not None
    assert tm["status"] == "tentative_match"

    trip_a_db = dynamo.get_item(f"TRIP#{trip_a['tripId']}", "META")
    assert trip_a_db["status"] == "tentative_match"
    assert trip_a_db["tentativeMatchId"] == tm["matchId"]

    # 4. Fast forward to T-2h
    lock_now = flight_eta - timedelta(hours=2)

    # 5. Matchmaker run 2: lock window
    with patch("handlers.matching.matchmaker.datetime") as mock_dt, \
         patch("handlers.matching.matchmaker.put_event") as mock_evt:
        mock_dt.now.return_value = lock_now
        mock_dt.fromisoformat = datetime.fromisoformat
        
        matchmaker_handler({}, lambda_context)
    
    # Verify match locked
    trip_a_locked = dynamo.get_item(f"TRIP#{trip_a['tripId']}", "META")
    assert trip_a_locked["status"] == "matched"
    assert trip_a_locked.get("tentativeMatchId") is None

    # Verify event emitted
    mock_evt.assert_called()
    called_events = [c[0][0] for c in mock_evt.call_args_list]
    assert "match.found" in called_events
