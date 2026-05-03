"""Flot — Flight Tracker client (v4).

Resolves real ETA for a flight number + date.
Supports aviation_edge, flightaware, and mock providers.
Includes in-memory circuit breaker: 3 consecutive failures → 30 min blackout.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import urllib.request
import urllib.parse
import json

from aws_lambda_powertools import Logger

logger = Logger(child=True)


class FlightTrackerError(Exception):
    pass


class _CircuitBreaker:
    """In-memory circuit breaker per provider. Resets across cold starts."""

    FAILURE_THRESHOLD = 3
    BLACKOUT_MINUTES = 30

    def __init__(self) -> None:
        self._failures = 0
        self._open_until: datetime | None = None

    def is_open(self) -> bool:
        if self._open_until is None:
            return False
        if datetime.now(timezone.utc) >= self._open_until:
            self._failures = 0
            self._open_until = None
            return False
        return True

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.FAILURE_THRESHOLD:
            self._open_until = datetime.now(timezone.utc) + timedelta(minutes=self.BLACKOUT_MINUTES)
            logger.warning("flight_tracker_circuit_open", open_until=self._open_until.isoformat())

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = None


_breaker = _CircuitBreaker()


def fetch_flight_eta(flight_number: str, flight_date: str) -> datetime:
    """Return actual ETA (UTC) for a flight. Raises FlightTrackerError on failure.

    flight_date format: "YYYY-MM-DD"
    """
    if _breaker.is_open():
        raise FlightTrackerError("circuit_open")

    provider = os.environ.get("FLIGHT_TRACKER_PROVIDER", "mock")

    try:
        if provider == "mock":
            eta = _mock_fetch(flight_number, flight_date)
        elif provider == "aviation_edge":
            eta = _aviation_edge_fetch(flight_number, flight_date)
        else:
            raise FlightTrackerError(f"unknown_provider:{provider}")

        _breaker.record_success()
        return eta

    except FlightTrackerError:
        _breaker.record_failure()
        raise
    except Exception as exc:
        _breaker.record_failure()
        raise FlightTrackerError(f"unexpected:{exc}") from exc


# ── Provider implementations ──────────────────────────────────────────


def _mock_fetch(flight_number: str, flight_date: str) -> datetime:
    """Mock provider — returns noon UTC on the flight date. Only for tests/dev."""
    return datetime.fromisoformat(f"{flight_date}T12:00:00+00:00")


def _aviation_edge_fetch(flight_number: str, flight_date: str) -> datetime:
    """AviationEdge flight status API. Timeout: 2s."""
    api_key = os.environ.get("FLIGHT_TRACKER_API_KEY", "")
    if not api_key:
        raise FlightTrackerError("missing_api_key")

    # AviationEdge real-time flight status endpoint
    params = urllib.parse.urlencode({
        "key": api_key,
        "flightIata": flight_number,
    })
    url = f"https://aviation-edge.com/v2/public/flights?{params}"

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
    except OSError as exc:
        raise FlightTrackerError(f"http_error:{exc}") from exc

    if not isinstance(data, list) or not data:
        raise FlightTrackerError("no_flights_found")

    # Find the flight matching flightDate
    for flight in data:
        arrival = flight.get("arrival", {})
        scheduled = arrival.get("scheduledTime") or arrival.get("estimatedTime")
        if not scheduled:
            continue
        try:
            eta = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
        except ValueError:
            continue
        if eta.date().isoformat() == flight_date:
            actual = arrival.get("actualTime")
            if actual:
                try:
                    return datetime.fromisoformat(actual.replace("Z", "+00:00"))
                except ValueError:
                    pass
            return eta

    raise FlightTrackerError("flight_not_found_for_date")
