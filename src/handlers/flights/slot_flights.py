"""Flot — GET /flights/slot

Slot-browse proxy to AeroDataBox: lists arrivals/departures at the hub airport
around a time slot. Backs the "Find your flight" sheet in check-in.
API key stays server-side (SSM); never exposed to the client.

Query params:
  direction  arrivals | departures   (default arrivals)
  slot       HH:MM                    (local-ish wall time, default now)
  date       YYYY-MM-DD               (required)
  airport    IATA hub                 (default MXP)

Never raises 5xx — the frontend degrades gracefully on [].
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import boto3
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext

from lib.http import app_handler, success

logger = Logger()
tracer = Tracer()

RAPIDAPI_HOST = "aerodatabox.p.rapidapi.com"
FETCH_TIMEOUT_SECONDS = 4.0
DURATION_MINUTES = 120

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


def _pick_local_time(block: dict) -> str:
    for field in ("runwayTime", "revisedTime", "scheduledTime"):
        t = (block.get(field) or {}).get("local")
        if t:
            return t.replace(" ", "T")
    return ""


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context: LambdaContext) -> dict:
    params = event.get("queryStringParameters") or {}
    origin = event.get("_origin")

    direction = (params.get("direction") or "arrivals").strip().lower()
    if direction not in ("arrivals", "departures"):
        direction = "arrivals"
    slot = (params.get("slot") or "12:00").strip()
    date = (params.get("date") or "").strip()
    airport = (params.get("airport") or "MXP").strip().upper()

    if not date:
        return success([], origin)

    provider = os.environ.get("FLIGHT_TRACKER_PROVIDER", "mock")
    if provider == "mock":
        return success(_mock_rows(direction, airport), origin)

    try:
        api_key = _get_api_key()
    except Exception as exc:
        logger.warning("flight_slot_ssm_error", reason=str(exc))
        api_key = ""
    if not api_key:
        return success(_mock_rows(direction, airport), origin)

    try:
        rows = _fetch_slot(direction, slot, date, airport, api_key)
    except Exception as exc:
        logger.warning("flight_slot_failed", airport=airport, slot=slot, reason=str(exc))
        return success([], origin)

    return success(rows, origin)


def _fetch_slot(direction: str, slot: str, date: str, airport: str, api_key: str) -> list[dict]:
    try:
        start = datetime.fromisoformat(f"{date}T{slot}:00")
    except ValueError:
        return []
    end = start + timedelta(minutes=DURATION_MINUTES)
    from_local = start.strftime("%Y-%m-%dT%H:%M")
    to_local = end.strftime("%Y-%m-%dT%H:%M")
    dir_param = "Arrival" if direction == "arrivals" else "Departure"

    qs = urllib.parse.urlencode({
        "withLeg": "true",
        "direction": dir_param,
        "withCancelled": "true",
        "withCodeshared": "true",
        "withCargo": "false",
        "withPrivate": "false",
        "withLocation": "false",
    })
    path = f"/flights/airports/iata/{urllib.parse.quote(airport)}/{from_local}/{to_local}"
    url = f"https://{RAPIDAPI_HOST}{path}?{qs}"

    req = urllib.request.Request(
        url,
        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": RAPIDAPI_HOST},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
        raw = json.loads(resp.read())

    flights = raw.get(direction, []) if isinstance(raw, dict) else []
    rows: list[dict] = []
    for f in flights:
        arr = f.get("arrival", {}) or {}
        dep = f.get("departure", {}) or {}
        arr_ap = (arr.get("airport") or {})
        dep_ap = (dep.get("airport") or {})
        time_block = arr if direction == "arrivals" else dep
        number = f.get("number") or ""
        if not number:
            continue
        if direction == "arrivals":
            origin_iata, origin_name = dep_ap.get("iata", ""), dep_ap.get("name", "")
            dest_iata, dest_name = airport, airport
        else:
            origin_iata, origin_name = airport, airport
            dest_iata, dest_name = arr_ap.get("iata", ""), arr_ap.get("name", "")
        rows.append({
            "number": number,
            "originIata": origin_iata,
            "originName": origin_name,
            "destIata": dest_iata,
            "destName": dest_name,
            "scheduledTimeLocal": _pick_local_time(time_block),
            "status": f.get("status", ""),
        })
    return rows


def _mock_rows(direction: str, airport: str) -> list[dict]:
    base = [
        {"number": "FR3324", "other": ("FCO", "Roma Fiumicino")},
        {"number": "AZ610", "other": ("FCO", "Roma Fiumicino")},
        {"number": "U24820", "other": ("LGW", "London Gatwick")},
    ]
    rows = []
    for i, b in enumerate(base):
        oth_iata, oth_name = b["other"]
        if direction == "arrivals":
            o_i, o_n, d_i, d_n = oth_iata, oth_name, airport, airport
        else:
            o_i, o_n, d_i, d_n = airport, airport, oth_iata, oth_name
        rows.append({
            "number": b["number"],
            "originIata": o_i, "originName": o_n,
            "destIata": d_i, "destName": d_n,
            "scheduledTimeLocal": f"2026-06-10T{10 + i}:00:00",
            "status": "Scheduled",
        })
    return rows
