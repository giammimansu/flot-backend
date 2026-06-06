"""Flot — Multi-airport isolation (P2 #12).

Verifies the architecture is genuinely multi-airport: a second active airport
(FCO) coexists with MXP and trips from different airports never match. GSI5
partitions trips by `airportCode#status`, so each pool is queried in isolation.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

if "firebase_admin" not in sys.modules:
    sys.modules["firebase_admin"] = MagicMock()
    sys.modules["firebase_admin.credentials"] = MagicMock()
    sys.modules["firebase_admin.messaging"] = MagicMock()
    sys.modules["firebase_admin.exceptions"] = MagicMock()


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _put_trip(table, trip_id, airport, user_id, direction, lat, lng, flight_time):
    table.put_item(Item={
        "pk": f"TRIP#{trip_id}", "sk": "META",
        "tripId": trip_id, "userId": user_id, "airportCode": airport,
        "terminal": "T1", "direction": direction,
        "destination": "x", "destLat": Decimal(str(lat)), "destLng": Decimal(str(lng)),
        "destPlaceId": "p", "destZone": "centro", "mode": "scheduled",
        "flightNumber": "AZ100", "flightDate": "2026-06-08",
        "flightTime": flight_time, "timeBucket": "2026-06-08T10:00:00Z",
        "luggage": 1, "paxCount": 1, "status": "scheduled",
        "tentativeMatchId": None, "verified": False,
        "createdAt": _iso(datetime.now(timezone.utc)),
        "gsi5pk": f"{airport}#scheduled", "gsi5sk": flight_time,
    })


# ── Config ────────────────────────────────────────────────────────────

class TestAirportRegistry:
    def test_fco_active_and_distinct(self):
        from lib.airports import get_airport, get_active_airports
        codes = {a.code for a in get_active_airports()}
        assert {"MXP", "FCO"}.issubset(codes)
        assert get_airport("FCO").direction_labels == ("TO_ROME", "FROM_ROME")
        assert get_airport("MXP").direction_labels == ("TO_MILAN", "FROM_MILAN")


# ── Matching isolation ────────────────────────────────────────────────

class TestCrossAirportIsolation:
    def test_mxp_and_fco_never_match(self, dynamodb_table, lambda_context):
        ft = _iso(datetime.now(timezone.utc) + timedelta(hours=30))
        # Same destination coords + time on purpose — only the airport differs.
        _put_trip(dynamodb_table, "mxp1", "MXP", "ua", "TO_MILAN", 45.46, 9.19, ft)
        _put_trip(dynamodb_table, "fco1", "FCO", "ub", "TO_ROME", 45.46, 9.19, ft)

        from handlers.matching import matchmaker
        with patch.object(matchmaker, "put_event"):
            matchmaker.handler({}, lambda_context)

        # Neither trip should have a tentative match — each airport pool had <2 trips.
        for tid in ("mxp1", "fco1"):
            item = dynamodb_table.get_item(Key={"pk": f"TRIP#{tid}", "sk": "META"}).get("Item")
            assert item["status"] == "scheduled"
            assert item.get("tentativeMatchId") is None

    def test_same_airport_pair_matches_while_other_airport_idle(self, dynamodb_table, lambda_context):
        ft = _iso(datetime.now(timezone.utc) + timedelta(hours=30))
        _put_trip(dynamodb_table, "mxpA", "MXP", "ua", "TO_MILAN", 45.464, 9.190, ft)
        _put_trip(dynamodb_table, "mxpB", "MXP", "ub", "TO_MILAN", 45.465, 9.191, ft)
        _put_trip(dynamodb_table, "fcoX", "FCO", "uc", "TO_ROME", 41.89, 12.48, ft)

        from handlers.matching import matchmaker
        with patch.object(matchmaker, "put_event"):
            matchmaker.handler({}, lambda_context)

        a = dynamodb_table.get_item(Key={"pk": "TRIP#mxpA", "sk": "META"}).get("Item")
        b = dynamodb_table.get_item(Key={"pk": "TRIP#mxpB", "sk": "META"}).get("Item")
        x = dynamodb_table.get_item(Key={"pk": "TRIP#fcoX", "sk": "META"}).get("Item")

        assert a["status"] == "tentative_match"
        assert b["status"] == "tentative_match"
        assert a["tentativeMatchId"] == b["tentativeMatchId"]
        # FCO trip stayed idle — no cross-airport bleed.
        assert x["status"] == "scheduled"
