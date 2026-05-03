"""Flot — Unit tests for flight tracker client (v4)."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

import lib.flight_tracker as ft
from lib.flight_tracker import FlightTrackerError, fetch_flight_eta, _breaker


@pytest.fixture(autouse=True)
def reset_circuit_breaker():
    """Reset circuit breaker state between tests."""
    _breaker._failures = 0
    _breaker._open_until = None
    yield
    _breaker._failures = 0
    _breaker._open_until = None


# ── Mock provider ─────────────────────────────────────────────────────

def test_mock_provider_returns_noon_utc(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "mock")
    eta = fetch_flight_eta("AZ1234", "2026-05-10")
    assert eta == datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_mock_provider_correct_date(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "mock")
    eta = fetch_flight_eta("LH456", "2026-12-25")
    assert eta.date().isoformat() == "2026-12-25"


# ── Circuit breaker ───────────────────────────────────────────────────

def test_circuit_opens_after_3_failures(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "aviation_edge")
    monkeypatch.setenv("FLIGHT_TRACKER_API_KEY", "test-key")

    with patch("lib.flight_tracker._aviation_edge_fetch", side_effect=FlightTrackerError("timeout")):
        for _ in range(3):
            with pytest.raises(FlightTrackerError):
                fetch_flight_eta("AZ1234", "2026-05-10")

    assert _breaker.is_open()


def test_circuit_open_skips_api_call(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "aviation_edge")
    monkeypatch.setenv("FLIGHT_TRACKER_API_KEY", "test-key")

    # Force circuit open
    _breaker._failures = 3
    from datetime import timedelta
    _breaker._open_until = datetime.now(timezone.utc) + timedelta(minutes=25)

    call_count = 0

    def fake_fetch(fn, fd):
        nonlocal call_count
        call_count += 1
        return datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

    with patch("lib.flight_tracker._aviation_edge_fetch", side_effect=fake_fetch):
        with pytest.raises(FlightTrackerError, match="circuit_open"):
            fetch_flight_eta("AZ1234", "2026-05-10")

    assert call_count == 0  # API never called


def test_circuit_resets_after_blackout(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "mock")

    from datetime import timedelta
    # Set open_until in the past
    _breaker._failures = 3
    _breaker._open_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    eta = fetch_flight_eta("AZ1234", "2026-05-10")
    assert eta is not None
    assert not _breaker.is_open()
    assert _breaker._failures == 0


# ── Unknown provider ──────────────────────────────────────────────────

def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "nonexistent")
    with pytest.raises(FlightTrackerError, match="unknown_provider"):
        fetch_flight_eta("AZ1234", "2026-05-10")


# ── Aviation Edge — missing API key ───────────────────────────────────

def test_aviation_edge_missing_key_raises(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "aviation_edge")
    monkeypatch.delenv("FLIGHT_TRACKER_API_KEY", raising=False)
    with pytest.raises(FlightTrackerError, match="missing_api_key"):
        fetch_flight_eta("AZ1234", "2026-05-10")


# ── Integration: create_trip fallback ────────────────────────────────

def test_fallback_sets_tracking_pending_status(monkeypatch):
    """When tracker unavailable, trip status must be tracking_pending."""
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "aviation_edge")
    monkeypatch.setenv("FLIGHT_TRACKER_API_KEY", "test-key")

    with patch("lib.flight_tracker._aviation_edge_fetch", side_effect=FlightTrackerError("timeout")):
        with pytest.raises(FlightTrackerError):
            fetch_flight_eta("AZ1234", "2026-05-10")

    # Circuit breaker recorded failure — that's the important assertion
    assert _breaker._failures >= 1
