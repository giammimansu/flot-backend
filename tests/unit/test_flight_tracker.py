"""Flot — Unit tests for flight tracker client (v4 cascade)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import lib.flight_tracker as ft
from lib.flight_tracker import FlightTrackerError, fetch_flight_eta, _breaker_primary, _breaker_fallback


@pytest.fixture(autouse=True)
def reset_circuit_breakers():
    """Reset both circuit breakers between tests."""
    for b in (_breaker_primary, _breaker_fallback):
        b._failures = 0
        b._open_until = None
    yield
    for b in (_breaker_primary, _breaker_fallback):
        b._failures = 0
        b._open_until = None


# ── Mock provider ─────────────────────────────────────────────────────

def test_mock_provider_returns_noon_utc(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "mock")
    eta = fetch_flight_eta("AZ1234", "2026-05-10")
    assert eta == datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_mock_provider_correct_date(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "mock")
    eta = fetch_flight_eta("LH456", "2026-12-25")
    assert eta is not None
    assert eta.date().isoformat() == "2026-12-25"


# ── Circuit breaker ───────────────────────────────────────────────────

def test_primary_circuit_opens_after_3_failures(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "aviation_edge")
    monkeypatch.setenv("FLIGHT_TRACKER_FALLBACK_PROVIDER", "")
    monkeypatch.setenv("FLIGHT_TRACKER_API_KEY", "test-key")

    with patch("lib.flight_tracker._aviation_edge_fetch", side_effect=FlightTrackerError("timeout")):
        for _ in range(3):
            result = fetch_flight_eta("AZ1234", "2026-05-10")
            assert result is None  # degraded, not raised

    assert _breaker_primary.is_open()


def test_primary_circuit_open_skips_api_call(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "aviation_edge")
    monkeypatch.setenv("FLIGHT_TRACKER_FALLBACK_PROVIDER", "")
    monkeypatch.setenv("FLIGHT_TRACKER_API_KEY", "test-key")

    _breaker_primary._failures = 3
    _breaker_primary._open_until = datetime.now(timezone.utc) + timedelta(minutes=25)

    call_count = 0

    def fake_fetch(fn, fd):
        nonlocal call_count
        call_count += 1
        return datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

    with patch("lib.flight_tracker._aviation_edge_fetch", side_effect=fake_fetch):
        result = fetch_flight_eta("AZ1234", "2026-05-10")

    assert call_count == 0  # primary skipped
    assert result is None   # no fallback configured → degraded


def test_circuit_resets_after_blackout(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "mock")

    _breaker_primary._failures = 3
    _breaker_primary._open_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    eta = fetch_flight_eta("AZ1234", "2026-05-10")
    assert eta is not None
    assert not _breaker_primary.is_open()
    assert _breaker_primary._failures == 0


# ── Unknown provider ──────────────────────────────────────────────────

def test_unknown_provider_returns_none(monkeypatch):
    """Unknown provider: treated as failure, returns None (degraded)."""
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "nonexistent")
    monkeypatch.setenv("FLIGHT_TRACKER_FALLBACK_PROVIDER", "")
    result = fetch_flight_eta("AZ1234", "2026-05-10")
    assert result is None


# ── Fallback cascade ──────────────────────────────────────────────────

def test_fallback_used_when_primary_fails(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "aviation_edge")
    monkeypatch.setenv("FLIGHT_TRACKER_FALLBACK_PROVIDER", "aerodatabox")
    monkeypatch.setenv("FLIGHT_TRACKER_API_KEY", "test-key")

    fallback_eta = datetime(2026, 5, 10, 15, 30, tzinfo=timezone.utc)

    with patch("lib.flight_tracker._aviation_edge_fetch", side_effect=FlightTrackerError("timeout")), \
         patch("lib.flight_tracker._aerodatabox_fetch", return_value=fallback_eta):
        result = fetch_flight_eta("AZ1234", "2026-05-10")

    assert result == fallback_eta


def test_degraded_when_both_providers_fail(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "aviation_edge")
    monkeypatch.setenv("FLIGHT_TRACKER_FALLBACK_PROVIDER", "aerodatabox")
    monkeypatch.setenv("FLIGHT_TRACKER_API_KEY", "test-key")

    with patch("lib.flight_tracker._aviation_edge_fetch", side_effect=FlightTrackerError("timeout")), \
         patch("lib.flight_tracker._aerodatabox_fetch", side_effect=FlightTrackerError("timeout")):
        result = fetch_flight_eta("AZ1234", "2026-05-10")

    assert result is None


def test_no_fallback_configured_returns_none_on_primary_fail(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "aviation_edge")
    monkeypatch.setenv("FLIGHT_TRACKER_FALLBACK_PROVIDER", "")
    monkeypatch.setenv("FLIGHT_TRACKER_API_KEY", "test-key")

    with patch("lib.flight_tracker._aviation_edge_fetch", side_effect=FlightTrackerError("timeout")):
        result = fetch_flight_eta("AZ1234", "2026-05-10")

    assert result is None


# ── Aviation Edge — missing API key ───────────────────────────────────

def test_aviation_edge_missing_key_returns_none(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "aviation_edge")
    monkeypatch.setenv("FLIGHT_TRACKER_FALLBACK_PROVIDER", "")
    monkeypatch.delenv("FLIGHT_TRACKER_API_KEY", raising=False)
    result = fetch_flight_eta("AZ1234", "2026-05-10")
    assert result is None


# ── Degraded state propagates circuit failure ─────────────────────────

def test_primary_failure_increments_breaker(monkeypatch):
    monkeypatch.setenv("FLIGHT_TRACKER_PROVIDER", "aviation_edge")
    monkeypatch.setenv("FLIGHT_TRACKER_FALLBACK_PROVIDER", "")
    monkeypatch.setenv("FLIGHT_TRACKER_API_KEY", "test-key")

    with patch("lib.flight_tracker._aviation_edge_fetch", side_effect=FlightTrackerError("timeout")):
        fetch_flight_eta("AZ1234", "2026-05-10")

    assert _breaker_primary._failures >= 1
