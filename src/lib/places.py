"""Flot — Places client.

Snaps a raw coordinate to the nearest walkable address via Google Places
Nearby Search (or a mock provider for tests/dev).

API key read at runtime from SSM (GOOGLE_PLACES_API_KEY_PARAM env var),
cached in-memory across warm invocations. Pattern mirrors flight_lookup.py.

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

_ssm_client = None
_api_key_cache: str | None = None


def _get_api_key() -> str:
    global _ssm_client, _api_key_cache
    if _api_key_cache:
        return _api_key_cache
    param_name = os.environ.get("GOOGLE_PLACES_API_KEY_PARAM", "")
    if not param_name:
        return ""
    try:
        import boto3
        if _ssm_client is None:
            _ssm_client = boto3.client("ssm")
        resp = _ssm_client.get_parameter(Name=param_name, WithDecryption=True)
        _api_key_cache = resp["Parameter"]["Value"]
        return _api_key_cache
    except Exception as exc:
        logger.warning("places_ssm_fetch_failed", reason=str(exc))
        return ""


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
    api_key = _get_api_key()
    if not api_key:
        logger.warning("places_snap_failed", reason="missing_api_key")
        return None

    params = urllib.parse.urlencode({
        "location": f"{lat},{lng}",
        "rankby": "distance",
        "type": "establishment",
        "key": api_key,
    })
    url = f"{_NEARBY_SEARCH_URL}?{params}"

    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
        data = json.loads(resp.read())

    status = data.get("status", "")
    if status not in ("OK", "ZERO_RESULTS"):
        logger.warning(
            "places_snap_failed",
            reason=f"google_status:{status}",
            error_message=data.get("error_message", ""),
        )
        return None

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
