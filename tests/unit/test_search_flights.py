"""Tests for GET /flights/search handler."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_env():
    with patch.dict(os.environ, {"FLIGHT_TRACKER_PROVIDER": "mock", "STAGE": "dev"}, clear=False):
        yield


def _event(q: str = "", airport: str = "MXP") -> dict:
    return {
        "queryStringParameters": {"q": q, "airport": airport},
        "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "user-1"}}},
        "body": None,
    }


def _ctx():
    ctx = MagicMock()
    ctx.aws_request_id = "test-req"
    ctx.function_name = "flot-flight-search-test"
    ctx.function_version = "$LATEST"
    ctx.invoked_function_arn = "arn:aws:lambda:eu-south-1:000000000000:function:flot-flight-search-test"
    ctx.memory_limit_in_mb = 128
    return ctx


def test_short_query_returns_mock_results():
    from handlers.flights.search_flights import handler
    resp = handler(_event(q="FR"), _ctx())
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert isinstance(body, list)
    assert any(f["flightNumber"].startswith("FR") for f in body)
    assert all(f["destination"] == "MXP" for f in body)


def test_empty_query_returns_empty():
    from handlers.flights.search_flights import handler
    resp = handler(_event(q=""), _ctx())
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == []


def test_single_char_query_returns_empty():
    from handlers.flights.search_flights import handler
    resp = handler(_event(q="F"), _ctx())
    assert json.loads(resp["body"]) == []


def test_unknown_prefix_returns_empty():
    from handlers.flights.search_flights import handler
    resp = handler(_event(q="ZZ"), _ctx())
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == []


def test_api_failure_returns_empty_not_500():
    from handlers.flights import search_flights
    with patch.object(search_flights, "search_flights_by_prefix", side_effect=RuntimeError("boom")):
        resp = search_flights.handler(_event(q="FR"), _ctx())
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == []


def test_filter_respects_airport_code():
    from handlers.flights.search_flights import handler
    # All mock flights go to MXP, so FCO should return nothing.
    resp = handler(_event(q="FR", airport="FCO"), _ctx())
    assert json.loads(resp["body"]) == []


def test_uppercases_and_strips_query():
    from handlers.flights.search_flights import handler
    resp = handler(_event(q="  fr 33  "), _ctx())
    body = json.loads(resp["body"])
    assert all(f["flightNumber"].startswith("FR33") for f in body)
    assert len(body) >= 1


def test_suggestion_shape():
    from handlers.flights.search_flights import handler
    resp = handler(_event(q="AZ"), _ctx())
    body = json.loads(resp["body"])
    assert body, "expected at least one AZ flight in mock pool"
    f = body[0]
    assert set(f.keys()) == {"flightNumber", "origin", "destination", "scheduledArrival", "flightDate", "terminal"}


def test_cache_populated_on_real_provider():
    """On real provider: cache miss triggers fetch once; second call uses cache."""
    from handlers.flights import search_flights

    fake_flights = [
        search_flights.FlightSuggestion("FR3324", "FCO", "MXP", "2026-05-19T12:00:00Z", "2026-05-19", "T1"),
    ]

    with patch.dict(os.environ, {"FLIGHT_TRACKER_PROVIDER": "aerodatabox", "AERODATABOX_SSM_KEY": "/flot/test/key"}, clear=False):
        search_flights._cache.clear()
        search_flights._api_key_cache = "fake-key"  # bypass SSM call
        with patch.object(search_flights, "_fetch_arrivals_for_date", return_value=fake_flights) as mock_inner:
            r1 = search_flights.search_flights_by_prefix("FR", "MXP")
            # Cache is warm now — inner fetcher should NOT be called again.
            r2 = search_flights.search_flights_by_prefix("FR", "MXP")

        # Called CACHE_DAYS times on first call, zero times on second.
        assert mock_inner.call_count == search_flights.CACHE_DAYS
        assert r1 == r2
        assert all(f.flightNumber.startswith("FR") for f in r1)
    search_flights._api_key_cache = None  # cleanup


def test_parse_arrival_handles_spaced_flight_number():
    from handlers.flights.search_flights import _parse_arrival
    flight = {
        "number": "FR 3324",
        "arrival": {
            "scheduledTimeUtc": "2026-05-19 12:00Z",
            "terminal": "T1",
            "airport": {"iata": "MXP"},
        },
        "departure": {"airport": {"iata": "FCO"}},
    }
    result = _parse_arrival(flight, "MXP")
    assert result is not None
    assert result.flightNumber == "FR3324"
    assert result.terminal == "T1"


def test_parse_arrival_returns_none_on_missing_time():
    from handlers.flights.search_flights import _parse_arrival
    flight = {
        "number": "FR3324",
        "arrival": {"airport": {"iata": "MXP"}},
        "departure": {"airport": {"iata": "FCO"}},
    }
    assert _parse_arrival(flight, "MXP") is None
