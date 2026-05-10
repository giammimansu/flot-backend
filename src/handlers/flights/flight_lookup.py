"""Flot — GET /flights/lookup

Proxy to AeroDataBox: resolves ETA + status for a single flight.
Used by the frontend autocomplete before POST /trips.
API key never exposed to the client.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext

from lib.http import app_handler, success, AppError

logger = Logger()
tracer = Tracer()

RAPIDAPI_HOST = "aerodatabox.p.rapidapi.com"


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context: LambdaContext) -> dict:
    params = event.get("queryStringParameters") or {}
    flight_number = (params.get("number") or "").strip().upper()
    flight_date = (params.get("date") or "").strip()

    if not flight_number or not flight_date:
        raise AppError(400, "missing_params", details={"required": ["number", "date"]})

    api_key = os.environ.get("AERODATABOX_API_KEY", "")
    if not api_key:
        # In dev/mock mode return a plausible fake response
        provider = os.environ.get("FLIGHT_TRACKER_PROVIDER", "mock")
        if provider == "mock":
            return success(_mock_response(flight_number, flight_date), event.get("_origin"))
        raise AppError(503, "flight_lookup_unavailable")

    try:
        data = _fetch_aerodatabox(flight_number, flight_date, api_key)
    except _NotFoundError:
        raise AppError(404, "flight_not_found")
    except _UpstreamError as exc:
        logger.warning("aerodatabox_error", flight=flight_number, reason=str(exc))
        raise AppError(502, "upstream_error")

    logger.info("flight_lookup_ok", flight=flight_number, date=flight_date)
    return success(data, event.get("_origin"))


# ── AeroDataBox ───────────────────────────────────────────────────────


def _fetch_aerodatabox(flight_number: str, flight_date: str, api_key: str) -> dict:
    encoded = urllib.parse.quote(flight_number)
    url = f"https://{RAPIDAPI_HOST}/flights/number/{encoded}/{flight_date}"

    req = urllib.request.Request(
        url,
        headers={
            "X-RapidAPI-Key": api_key,
            "X-RapidAPI-Host": RAPIDAPI_HOST,
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _NotFoundError()
        raise _UpstreamError(f"http_{e.code}")
    except OSError as e:
        raise _UpstreamError(f"network:{e}")

    if not isinstance(raw, list) or not raw:
        raise _NotFoundError()

    flight = raw[0]
    arrival = flight.get("arrival", {})
    departure = flight.get("departure", {})

    arrival_time_local = (
        arrival.get("actualTimeLocal")
        or arrival.get("estimatedTimeLocal")
        or arrival.get("scheduledTimeLocal")
    )
    arrival_time_utc = (
        arrival.get("actualTimeUtc")
        or arrival.get("estimatedTimeUtc")
        or arrival.get("scheduledTimeUtc")
    )

    if not arrival_time_utc:
        raise _NotFoundError()

    airline_raw = flight.get("airline", {})

    return {
        "flightNumber": flight_number,
        "arrivalTimeLocal": arrival_time_local,
        "arrivalTimeUtc": arrival_time_utc,
        "status": flight.get("status", "Unknown"),
        "delayMinutes": arrival.get("delay"),
        "origin": departure.get("airport", {}).get("iata"),
        "airline": {
            "iata": airline_raw.get("iata", ""),
            "name": airline_raw.get("name", ""),
            "nameIT": airline_raw.get("name", ""),
        },
    }


def _mock_response(flight_number: str, flight_date: str) -> dict:
    prefix = flight_number[:2].upper()
    airline_names = {
        "AZ": "ITA Airways", "FR": "Ryanair", "U2": "easyJet",
        "LH": "Lufthansa", "EK": "Emirates", "TK": "Turkish Airlines",
    }
    airline_name = airline_names.get(prefix, "Unknown Airline")
    return {
        "flightNumber": flight_number,
        "arrivalTimeLocal": f"{flight_date}T12:00:00",
        "arrivalTimeUtc": f"{flight_date}T11:00:00Z",
        "status": "Scheduled",
        "delayMinutes": None,
        "origin": "FCO",
        "airline": {"iata": prefix, "name": airline_name, "nameIT": airline_name},
    }


class _NotFoundError(Exception):
    pass


class _UpstreamError(Exception):
    pass
