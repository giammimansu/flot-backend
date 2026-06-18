"""Flot — GET /flights/day

Whole-day flight browse for the "Find your flight" screen. AeroDataBox's FIDS
endpoint caps each request to a 12-hour window, so a full day is fetched as two
windows (00:00–12:00 and 12:00–24:00) server-side, then merged, de-duplicated
and sorted by scheduled time. The client makes a single request and filters by
time-of-day / airport entirely on the frontend.

Query params:
  direction  arrivals | departures   (default arrivals)
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

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext

from lib.http import app_handler, success
from handlers.flights.slot_flights import (
    RAPIDAPI_HOST,
    USER_AGENT,
    _get_api_key,
    _extract_flights,
    _parse_rows,
    _mock_rows,
)

logger = Logger()
tracer = Tracer()

# AeroDataBox FIDS allows up to a 12h window per request → 2 windows = one day.
WINDOW_HOURS = 12
FETCH_TIMEOUT_SECONDS = 6.0


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=False)
def handler(event: dict, context: LambdaContext) -> dict:
    params = event.get("queryStringParameters") or {}
    origin = event.get("_origin")

    direction = (params.get("direction") or "arrivals").strip().lower()
    if direction not in ("arrivals", "departures"):
        direction = "arrivals"
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
        logger.warning("flight_day_ssm_error", reason=str(exc))
        api_key = ""
    if not api_key:
        return success(_mock_rows(direction, airport), origin)

    try:
        day_start = datetime.fromisoformat(f"{date}T00:00:00")
    except ValueError:
        return success([], origin)

    windows = [
        (day_start, day_start + timedelta(hours=WINDOW_HOURS)),
        (day_start + timedelta(hours=WINDOW_HOURS), day_start + timedelta(hours=24)),
    ]

    seen: set[str] = set()
    merged: list[dict] = []
    for start, end in windows:
        try:
            raw = _fetch_window(direction, start, end, airport, api_key)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            logger.warning("flight_day_http_error", airport=airport,
                           window=start.isoformat(), reason=str(exc))
            continue
        except Exception as exc:
            logger.warning("flight_day_failed", airport=airport,
                           window=start.isoformat(), reason=str(exc))
            continue

        rows = _parse_rows(_extract_flights(raw, direction), direction, airport)
        for r in rows:
            key = f"{r['number']}-{r['scheduledTimeLocal']}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)

    merged.sort(key=lambda r: r.get("scheduledTimeLocal") or "")
    logger.info("flight_day_done", airport=airport, direction=direction,
                date=date, count=len(merged))
    return success(merged, origin)


def _fetch_window(direction: str, start: datetime, end: datetime,
                  airport: str, api_key: str) -> dict:
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
        headers={
            "X-RapidAPI-Key": api_key,
            "X-RapidAPI-Host": RAPIDAPI_HOST,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())
