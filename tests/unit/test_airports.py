"""Unit tests for src/lib/airports.py — Airport Registry."""
from __future__ import annotations

import pytest

from lib.airports import (
    AIRPORTS,
    AirportConfig,
    MeetingPoint,
    Terminal,
    Zone,
    airport_to_dict,
    get_active_airports,
    get_airport,
)


class TestGetAirport:
    """Tests for get_airport()."""

    def test_returns_mxp_config(self):
        """MXP should be returned with correct fields."""
        airport = get_airport("MXP")

        assert airport.code == "MXP"
        assert airport.name == "Milano Malpensa"
        assert airport.city == "Milano"
        assert airport.country == "IT"
        assert airport.currency == "EUR"
        assert airport.base_fare == 12000
        assert airport.unlock_fee == 199
        assert airport.timezone == "Europe/Rome"
        assert airport.active is True

    def test_mxp_has_terminals(self):
        """MXP should have T1 and T2."""
        airport = get_airport("MXP")

        assert len(airport.terminals) == 2
        codes = [t.code for t in airport.terminals]
        assert "T1" in codes
        assert "T2" in codes

    def test_mxp_has_zones(self):
        """MXP should have 5 destination zones."""
        airport = get_airport("MXP")

        assert len(airport.zones) == 5
        zone_codes = [z.code for z in airport.zones]
        assert set(zone_codes) == {"centro", "nord", "ovest", "sud", "est"}

    def test_mxp_has_meeting_points(self):
        """MXP should have meeting points for T1 and T2."""
        airport = get_airport("MXP")

        assert "T1" in airport.meeting_points
        assert "T2" in airport.meeting_points
        assert airport.meeting_points["T1"].walk_minutes == 8
        assert airport.meeting_points["T2"].walk_minutes == 5

    def test_mxp_direction_labels(self):
        """MXP direction labels should be TO_MILAN / FROM_MILAN."""
        airport = get_airport("MXP")

        assert airport.direction_labels == ("TO_MILAN", "FROM_MILAN")

    def test_invalid_code_raises_value_error(self):
        """Invalid code should raise ValueError."""
        with pytest.raises(ValueError, match="Airport XXX not available"):
            get_airport("XXX")

    def test_empty_code_raises_value_error(self):
        """Empty code should raise ValueError."""
        with pytest.raises(ValueError, match="Airport  not available"):
            get_airport("")

    def test_none_code_raises_value_error(self):
        """None should raise ValueError (dict.get returns None)."""
        with pytest.raises(ValueError):
            get_airport(None)  # type: ignore


class TestGetActiveAirports:
    """Tests for get_active_airports()."""

    def test_returns_list(self):
        """Should return a list."""
        airports = get_active_airports()
        assert isinstance(airports, list)

    def test_contains_mxp(self):
        """Active airports should include MXP."""
        airports = get_active_airports()
        codes = [a.code for a in airports]
        assert "MXP" in codes

    def test_only_active(self):
        """All returned airports should have active=True."""
        airports = get_active_airports()
        for airport in airports:
            assert airport.active is True


class TestAirportToDict:
    """Tests for airport_to_dict()."""

    def test_serializes_mxp(self):
        """MXP should serialize to a valid dict."""
        airport = get_airport("MXP")
        data = airport_to_dict(airport)

        assert data["code"] == "MXP"
        assert data["name"] == "Milano Malpensa"
        assert data["baseFare"] == 12000
        assert data["unlockFee"] == 199

    def test_serializes_terminals(self):
        """Terminals should be a list of dicts."""
        airport = get_airport("MXP")
        data = airport_to_dict(airport)

        assert len(data["terminals"]) == 2
        assert data["terminals"][0]["code"] in ("T1", "T2")
        assert "label" in data["terminals"][0]

    def test_serializes_zones(self):
        """Zones should include lat, lng, landmarks."""
        airport = get_airport("MXP")
        data = airport_to_dict(airport)

        centro = next(z for z in data["zones"] if z["code"] == "centro")
        assert centro["lat"] == 45.4642
        assert centro["lng"] == 9.1900
        assert "Duomo" in centro["landmarks"]

    def test_serializes_meeting_points(self):
        """Meeting points should be keyed by terminal code."""
        airport = get_airport("MXP")
        data = airport_to_dict(airport)

        assert "T1" in data["meetingPoints"]
        assert data["meetingPoints"]["T1"]["walkMinutes"] == 8

    def test_camel_case_keys(self):
        """API output should use camelCase keys."""
        airport = get_airport("MXP")
        data = airport_to_dict(airport)

        assert "baseFare" in data      # Not base_fare
        assert "unlockFee" in data     # Not unlock_fee
        assert "radiusKm" in data["zones"][0]  # Not radius_km
        assert "searchTimeoutSec" in data
