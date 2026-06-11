"""Unit tests for lib.places — snap_to_nearest_address."""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from lib.places import snap_to_nearest_address


def _mock_airport(provider="google"):
    cfg = MagicMock()
    cfg.places_provider = provider
    return cfg


# ── Mock provider ─────────────────────────────────────────────────────

def test_mock_provider_returns_same_coords():
    airport = _mock_airport(provider="mock")
    result = snap_to_nearest_address(45.4642, 9.1900, airport)
    assert result is not None
    assert result["lat"] == 45.4642
    assert result["lng"] == 9.1900
    assert result["address"]
    assert result["placeId"]


# ── Google provider — success ─────────────────────────────────────────

def _places_response(lat=45.4650, lng=9.1910, name="Via Test, 1", place_id="abc123"):
    return json.dumps({
        "results": [{
            "geometry": {"location": {"lat": lat, "lng": lng}},
            "vicinity": name,
            "place_id": place_id,
        }]
    }).encode()


def test_google_snap_success():
    airport = _mock_airport(provider="google")
    mock_resp = MagicMock()
    mock_resp.read.return_value = _places_response(45.4650, 9.1910, "Via Roma, 5", "pid_001")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("lib.places._get_api_key", return_value="test-key"):
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = snap_to_nearest_address(45.4642, 9.1900, airport)

    assert result is not None
    assert result["lat"] == 45.4650
    assert result["lng"] == 9.1910
    assert result["address"] == "Via Roma, 5"
    assert result["placeId"] == "pid_001"


def test_google_snap_no_results_returns_none():
    airport = _mock_airport(provider="google")
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"results": []}).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("lib.places._get_api_key", return_value="test-key"):
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = snap_to_nearest_address(45.4642, 9.1900, airport)

    assert result is None


# ── Failure cases — never propagate ──────────────────────────────────

def test_timeout_returns_none():
    airport = _mock_airport(provider="google")
    with patch("lib.places._get_api_key", return_value="test-key"):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = snap_to_nearest_address(45.4642, 9.1900, airport)
    assert result is None


def test_http_error_returns_none():
    airport = _mock_airport(provider="google")
    with patch("lib.places._get_api_key", return_value="test-key"):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = snap_to_nearest_address(45.4642, 9.1900, airport)
    assert result is None


def test_missing_api_key_returns_none():
    airport = _mock_airport(provider="google")
    with patch("lib.places._get_api_key", return_value=""):
        result = snap_to_nearest_address(45.4642, 9.1900, airport)
    assert result is None


def test_ssm_fetch_failure_returns_none():
    """SSM call fails at runtime → _get_api_key returns "" → snap degrades to raw_midpoint."""
    airport = _mock_airport(provider="google")
    with patch("lib.places._get_api_key", return_value=""):
        result = snap_to_nearest_address(45.4642, 9.1900, airport)
    assert result is None


def test_unknown_provider_returns_none():
    airport = _mock_airport(provider="other_maps")
    result = snap_to_nearest_address(45.4642, 9.1900, airport)
    assert result is None


# ── Always returns nearest (first result) regardless of distance ──────

def test_returns_first_result_regardless_of_distance():
    airport = _mock_airport(provider="google")
    mock_resp = MagicMock()
    mock_resp.read.return_value = _places_response(45.5000, 9.2500, "Far Place", "far_id")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("lib.places._get_api_key", return_value="test-key"):
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = snap_to_nearest_address(45.4642, 9.1900, airport)

    assert result is not None
    assert result["lat"] == 45.5000
    assert result["placeId"] == "far_id"
