"""Flot — GET /flights/search

Autocomplete endpoint for check-in UI.
Strategy:
  - Fetches all arrivals at the target airport for the next CACHE_DAYS days
    from AeroDataBox and caches them in Lambda memory for CACHE_TTL_SECONDS.
  - Prefix query filters the in-memory cache → instant results on warm Lambda.
  - In dev / mock mode returns a static list for UI development.
  - Never raises 5xx — frontend degrades gracefully.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import boto3

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext

from lib.http import app_handler, success

logger = Logger()
tracer = Tracer()

RAPIDAPI_HOST = "aerodatabox.p.rapidapi.com"
FETCH_TIMEOUT_SECONDS = 4.0
MAX_RESULTS = 10
MIN_QUERY_LENGTH = 2
CACHE_TTL_SECONDS = 1800   # 30 min
CACHE_DAYS = 5             # fetch arrivals for today + next 4 days

# IATA → ICAO mapping for airports supported by Flot.
_ICAO: dict[str, str] = {
    "MXP": "LIMC",
    "FCO": "LIRF",
    "LIN": "LIML",
}


@dataclass
class FlightSuggestion:
    flightNumber: str
    origin: str
    destination: str
    scheduledArrival: str   # ISO UTC, e.g. "2026-05-19T12:00:00Z"
    flightDate: str         # YYYY-MM-DD
    terminal: str | None


# ── SSM key resolver (cached per Lambda instance) ────────────────────

_ssm_client = None
_api_key_cache: str | None = None


def _get_api_key() -> str:
    global _ssm_client, _api_key_cache
    if _api_key_cache:
        return _api_key_cache
    param_name = os.environ.get("AERODATABOX_SSM_KEY", "")
    if not param_name:
        return ""
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    resp = _ssm_client.get_parameter(Name=param_name, WithDecryption=True)
    _api_key_cache = resp["Parameter"]["Value"]
    return _api_key_cache


# ── In-memory cache ───────────────────────────────────────────────────

class _CacheEntry(NamedTuple):
    flights: list[FlightSuggestion]
    expires_at: datetime


# keyed by airport_code
_cache: dict[str, _CacheEntry] = {}


def _get_cached(airport_code: str) -> list[FlightSuggestion] | None:
    entry = _cache.get(airport_code)
    if entry and datetime.now(timezone.utc) < entry.expires_at:
        return entry.flights
    return None


def _set_cache(airport_code: str, flights: list[FlightSuggestion]) -> None:
    _cache[airport_code] = _CacheEntry(
        flights=flights,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=CACHE_TTL_SECONDS),
    )


# ── Mock pool ─────────────────────────────────────────────────────────

_MOCK_FLIGHTS: list[FlightSuggestion] = [
    FlightSuggestion("FR3324", "FCO", "MXP", "2026-05-19T12:00:00Z", "2026-05-19", "T1"),
    FlightSuggestion("FR3320", "FCO", "MXP", "2026-05-20T08:30:00Z", "2026-05-20", "T1"),
    FlightSuggestion("U24820", "LGW", "MXP", "2026-05-19T14:15:00Z", "2026-05-19", "T2"),
    FlightSuggestion("AZ610",  "FCO", "MXP", "2026-05-19T10:45:00Z", "2026-05-19", "T1"),
    FlightSuggestion("LH9422", "FRA", "MXP", "2026-05-19T16:00:00Z", "2026-05-19", "T2"),
]


# ── Handler ───────────────────────────────────────────────────────────

@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context: LambdaContext) -> dict:
    params = event.get("queryStringParameters") or {}
    q = (params.get("q") or "").strip().upper().replace(" ", "")
    airport_code = (params.get("airport") or "MXP").strip().upper()
    origin = event.get("_origin")

    if len(q) < MIN_QUERY_LENGTH:
        return success([], origin)

    try:
        results = search_flights_by_prefix(q, airport_code)
    except Exception as exc:
        logger.warning("flight_search_failed", q=q, airport=airport_code, reason=str(exc))
        return success([], origin)

    return success([asdict(f) for f in results[:MAX_RESULTS]], origin)


# ── Core search ───────────────────────────────────────────────────────

def search_flights_by_prefix(query: str, airport_code: str) -> list[FlightSuggestion]:
    """Filter arrivals at airport_code by flight number prefix.

    Uses in-memory cache; fetches from AeroDataBox on cache miss.
    Falls back to mock pool in dev / when API key is absent.
    """
    provider = os.environ.get("FLIGHT_TRACKER_PROVIDER", "mock")

    if provider == "mock":
        return _filter(query, [f for f in _MOCK_FLIGHTS if f.destination == airport_code])

    try:
        api_key = _get_api_key()
    except Exception as exc:
        logger.warning("flight_search_ssm_error", reason=str(exc))
        api_key = ""
    if not api_key:
        logger.warning("flight_search_no_api_key", airport=airport_code)
        return _filter(query, [f for f in _MOCK_FLIGHTS if f.destination == airport_code])

    pool = _get_cached(airport_code)
    if pool is None:
        pool = _fetch_and_cache(airport_code, api_key)

    return _filter(query, pool)


def _filter(query: str, pool: list[FlightSuggestion]) -> list[FlightSuggestion]:
    return [f for f in pool if f.flightNumber.startswith(query)]


# ── AeroDataBox airport arrivals ──────────────────────────────────────

def _fetch_and_cache(airport_code: str, api_key: str = "") -> list[FlightSuggestion]:
    """Fetch arrivals for the next CACHE_DAYS days and populate the cache."""
    icao = _ICAO.get(airport_code)
    if not icao:
        logger.warning("flight_search_unknown_icao", airport=airport_code)
        return []

    today = datetime.now(timezone.utc).date()
    all_flights: list[FlightSuggestion] = []

    for day_offset in range(CACHE_DAYS):
        date = today + timedelta(days=day_offset)
        try:
            flights = _fetch_arrivals_for_date(icao, date.isoformat(), airport_code, api_key)
            all_flights.extend(flights)
        except Exception as exc:
            logger.warning("aerodatabox_arrivals_failed", icao=icao, date=date.isoformat(), reason=str(exc))
            # Partial failure is acceptable — use whatever was fetched so far.

    logger.info("flight_search_cache_populated", airport=airport_code, count=len(all_flights))
    _set_cache(airport_code, all_flights)
    return all_flights


def _fetch_arrivals_for_date(
    icao: str, flight_date: str, airport_code: str, api_key: str
) -> list[FlightSuggestion]:
    """Fetch all arrivals at icao airport for a single date from AeroDataBox."""
    # AeroDataBox: GET /airports/{icao}/flights/{isoDateTime}
    # isoDateTime format: "2026-05-19T00:00" — returns a 12h window; fetch twice for full day.
    results: list[FlightSuggestion] = []
    for hour in ("00:00", "12:00"):
        iso_dt = urllib.parse.quote(f"{flight_date}T{hour}")
        url = (
            f"https://{RAPIDAPI_HOST}/airports/{icao}/flights/{iso_dt}"
            f"?withLeg=true&withCancelled=false&filterFlightType=Passenger&direction=Arrival"
        )
        req = urllib.request.Request(
            url,
            headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": RAPIDAPI_HOST},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                raw = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (404, 204):
                continue
            raise
        except OSError:
            raise

        arrivals = raw if isinstance(raw, list) else raw.get("arrivals", []) or []
        for flight in arrivals:
            s = _parse_arrival(flight, airport_code)
            if s:
                results.append(s)

    # Deduplicate by flightNumber+flightDate (the two 12h windows may overlap).
    seen: set[tuple[str, str]] = set()
    deduped: list[FlightSuggestion] = []
    for f in results:
        key = (f.flightNumber, f.flightDate)
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    return deduped


def _parse_arrival(flight: dict, airport_code: str) -> FlightSuggestion | None:
    arrival = flight.get("arrival", {}) or {}
    departure = flight.get("departure", {}) or {}
    number_obj = flight.get("number") or flight.get("iataNumber") or ""

    # AeroDataBox returns number as e.g. "FR 3324" or nested {"iata": "FR3324"}
    if isinstance(number_obj, dict):
        flight_number = (number_obj.get("iata") or "").replace(" ", "").upper()
    else:
        flight_number = str(number_obj).replace(" ", "").upper()

    if not flight_number:
        return None

    arrival_utc = (
        arrival.get("scheduledTimeUtc")
        or arrival.get("estimatedTimeUtc")
        or arrival.get("actualTimeUtc")
    )
    if not arrival_utc:
        return None

    normalized = arrival_utc.replace(" ", "T")
    if not normalized.endswith("Z") and "+" not in normalized:
        normalized += "Z"

    try:
        f_date = datetime.fromisoformat(normalized.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None

    origin_iata = (departure.get("airport", {}) or {}).get("iata", "")
    terminal = arrival.get("terminal")

    return FlightSuggestion(
        flightNumber=flight_number,
        origin=origin_iata,
        destination=airport_code,
        scheduledArrival=normalized,
        flightDate=f_date,
        terminal=terminal,
    )
