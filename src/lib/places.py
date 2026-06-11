"""Flot — Places client.

Snaps a raw coordinate to the nearest walkable address via Google Places
Nearby Search (or a mock provider for tests/dev).

Never raises — any failure returns None and logs a warning.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

from aws_lambda_powertools import Logger

if TYPE_CHECKING:
    from .airports import AirportConfig

logger = Logger(child=True)

_NEARBY_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
_TIMEOUT_SEC = 2


def snap_to_nearest_address(lat: float, lng: float, airport: "AirportConfig") -> dict | None:
    """Return nearest walkable address to (lat, lng), or None on any failure."""
    provider = getattr(airport, "places_provider", "google")
    try:
        if provider == "mock":
            return _mock_snap(lat, lng)
        if provider == "google":
            return _google_nearby_fetch(lat, lng)
        logger.warning("places_snap_failed", reason=f"unknown_provider:{provider}")
        return None
    except Exception as exc:
        logger.warning("places_snap_failed", reason=str(exc))
        return None


def _google_nearby_fetch(lat: float, lng: float) -> dict | None:
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        logger.warning("places_snap_failed", reason="missing_api_key")
        return None

    params = urllib.parse.urlencode({
        "location": f"{lat},{lng}",
        "rankby": "distance",
        "type": "street_address|establishment",
        "key": api_key,
    })
    url = f"{_NEARBY_SEARCH_URL}?{params}"

    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
        data = json.loads(resp.read())

    results = data.get("results") or []
    if not results:
        logger.warning("places_snap_failed", reason="no_results")
        return None

    top = results[0]
    loc = top["geometry"]["location"]
    return {
        "lat": loc["lat"],
        "lng": loc["lng"],
        "address": top.get("vicinity") or top.get("name", ""),
        "placeId": top.get("place_id", ""),
    }


def _mock_snap(lat: float, lng: float) -> dict:
    return {
        "lat": lat,
        "lng": lng,
        "address": "Mock Address, 1",
        "placeId": "mock_place_id",
    }
