import pytest
from unittest.mock import MagicMock, patch
from handlers.matching.matchmaker import process_airport
from lib.airports import AirportConfig

def test_process_airport_no_trips():
    with patch("handlers.matching.matchmaker.dynamo.query_gsi", return_value=[]):
        airport = AirportConfig(code="MXP", name="Malpensa", timezone="Europe/Rome", coords=[45.63, 8.72], live_matching_radius_km=1.0, scheduled_advance_days=3)
        matches = process_airport(airport)
        assert matches == 0

def test_process_airport_with_matches():
    mock_trip_a = {"tripId": "1", "pk": "TRIP#1", "userId": "u1", "status": "scheduled", "flightTime": "2026-05-05T10:00:00Z", "airportCode": "MXP", "createdAt": "2026-05-01T10:00:00Z"}
    mock_trip_b = {"tripId": "2", "pk": "TRIP#2", "userId": "u2", "status": "scheduled", "flightTime": "2026-05-05T10:15:00Z", "airportCode": "MXP", "createdAt": "2026-05-01T10:00:00Z"}
    
    with patch("handlers.matching.matchmaker.dynamo.query_gsi", return_value=[mock_trip_a, mock_trip_b]), \
         patch("handlers.matching.matchmaker.dynamo.get_item", return_value={"gender": "M"}), \
         patch("handlers.matching.matchmaker.find_best_match") as mock_find, \
         patch("handlers.matching.matchmaker.create_match") as mock_create:
        
        mock_find.side_effect = [
            MagicMock(candidate=mock_trip_b, score=0.9), # match for A
            None # B already processed or no match
        ]
        
        airport = AirportConfig(code="MXP", name="Malpensa", timezone="Europe/Rome", coords=[45.63, 8.72], live_matching_radius_km=1.0, scheduled_advance_days=3)
        matches = process_airport(airport)
        assert matches == 1
        mock_create.assert_called_once()
