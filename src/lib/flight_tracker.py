"""Flot — Flight Tracker client (v4).

Resolves real ETA for a flight number + date.

Cascade strategy:
  1. Primary provider (FLIGHT_TRACKER_PROVIDER env var, default "mock")
  2. Fallback provider (FLIGHT_TRACKER_FALLBACK_PROVIDER env var, optional)
  3. Static degrade — returns None; caller sets trip.trackingStatus="degraded"

Each provider has its own circuit breaker (3 failures → 30 min blackout).
When the primary breaker opens, a CloudWatch metric is emitted so an alarm
can fire. Fallback does the same when it opens.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import urllib.request
import urllib.parse
import json

import boto3
from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

logger = Logger(child=True)
metrics = Metrics(namespace=os.environ.get("POWERTOOLS_METRICS_NAMESPACE", "Flot"))


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

    def record_failure(self, provider_name: str = "") -> None:
        self._failures += 1
        if self._failures >= self.FAILURE_THRESHOLD:
            self._open_until = datetime.now(timezone.utc) + timedelta(minutes=self.BLACKOUT_MINUTES)
            logger.warning(
                "flight_tracker_circuit_open",
                provider=provider_name,
                open_until=self._open_until.isoformat(),
            )
            # Emit CloudWatch metric so an alarm can fire
            try:
                metrics.add_metric(
                    name="FlightTrackerCircuitOpen",
                    unit=MetricUnit.Count,
                    value=1,
                )
            except Exception:
                pass  # metrics failure must never block the tracker

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = None


_breaker_primary = _CircuitBreaker()
_breaker_fallback = _CircuitBreaker()


def _call_provider(provider: str, flight_number: str, flight_date: str) -> datetime:
    if provider == "mock":
        return _mock_fetch(flight_number, flight_date)
    if provider == "aviation_edge":
        return _aviation_edge_fetch(flight_number, flight_date)
    if provider == "aerodatabox":
        return _aerodatabox_fetch(flight_number, flight_date)
    raise FlightTrackerError(f"unknown_provider:{provider}")


def fetch_flight_eta(flight_number: str, flight_date: str) -> datetime | None:
    """Return actual ETA (UTC) for a flight, or None if all providers fail (degraded).

    Cascade:
      1. Primary provider (FLIGHT_TRACKER_PROVIDER)
      2. Fallback provider (FLIGHT_TRACKER_FALLBACK_PROVIDER), if set
      3. Returns None — caller must use static flightTime and set trackingStatus="degraded"

    flight_date format: "YYYY-MM-DD"
    """
    primary = os.environ.get("FLIGHT_TRACKER_PROVIDER", "mock")
    fallback = os.environ.get("FLIGHT_TRACKER_FALLBACK_PROVIDER", "")

    # ── Primary ──────────────────────────────────────────────────────
    if not _breaker_primary.is_open():
        try:
            eta = _call_provider(primary, flight_number, flight_date)
            _breaker_primary.record_success()
            return eta
        except FlightTrackerError as exc:
            _breaker_primary.record_failure(primary)
            logger.warning("flight_tracker_primary_failed", provider=primary, error=str(exc))
        except Exception as exc:
            _breaker_primary.record_failure(primary)
            logger.warning("flight_tracker_primary_unexpected", provider=primary, error=str(exc))
    else:
        logger.info("flight_tracker_primary_circuit_open", provider=primary)

    # ── Fallback ─────────────────────────────────────────────────────
    if fallback and not _breaker_fallback.is_open():
        try:
            eta = _call_provider(fallback, flight_number, flight_date)
            _breaker_fallback.record_success()
            logger.info("flight_tracker_fallback_used", provider=fallback)
            return eta
        except FlightTrackerError as exc:
            _breaker_fallback.record_failure(fallback)
            logger.warning("flight_tracker_fallback_failed", provider=fallback, error=str(exc))
        except Exception as exc:
            _breaker_fallback.record_failure(fallback)
            logger.warning("flight_tracker_fallback_unexpected", provider=fallback, error=str(exc))
    elif fallback and _breaker_fallback.is_open():
        logger.info("flight_tracker_fallback_circuit_open", provider=fallback)

    # ── Degraded ─────────────────────────────────────────────────────
    logger.error("flight_tracker_all_providers_failed", primary=primary, fallback=fallback or "none")
    return None


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


def _aerodatabox_fetch(flight_number: str, flight_date: str) -> datetime:
    """AeroDataBox (RapidAPI) flight status. Timeout: 3s."""
    api_key = os.environ.get("FLIGHT_TRACKER_API_KEY", "")
    if not api_key:
        raise FlightTrackerError("missing_api_key")

    host = "aerodatabox.p.rapidapi.com"
    encoded = urllib.parse.quote(flight_number)
    url = f"https://{host}/flights/number/{encoded}/{flight_date}"

    req = urllib.request.Request(
        url,
        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": host},
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FlightTrackerError("flight_not_found_for_date")
        raise FlightTrackerError(f"http_{e.code}")
    except OSError as exc:
        raise FlightTrackerError(f"network_error:{exc}") from exc

    if not isinstance(raw, list) or not raw:
        raise FlightTrackerError("flight_not_found_for_date")

    arrival = raw[0].get("arrival", {})
    arrival_time_utc = (
        arrival.get("actualTimeUtc")
        or arrival.get("estimatedTimeUtc")
        or arrival.get("scheduledTimeUtc")
    )
    if not arrival_time_utc:
        raise FlightTrackerError("no_arrival_time")

    try:
        return datetime.fromisoformat(arrival_time_utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FlightTrackerError(f"parse_error:{arrival_time_utc}") from exc
