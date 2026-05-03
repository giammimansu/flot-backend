"""Flot — Zone utilities (geofencing, adjacency).

All zone data is sourced from src/lib/airports.py. Never hardcode zones here.
"""
from __future__ import annotations

from .airports import Zone, get_airport
from .matching import haversine_km


def get_zone(airport_code: str, zone_code: str) -> Zone:
    """Lookup zone in given airport. Raises ValueError if not found."""
    airport = get_airport(airport_code)
    for z in airport.zones:
        if z.code == zone_code:
            return z
    raise ValueError(f"Zone {zone_code} not found at {airport_code}")


def is_valid_zone(airport_code: str, zone_code: str) -> bool:
    """True if zone exists at airport."""
    try:
        get_zone(airport_code, zone_code)
        return True
    except ValueError:
        return False


def is_valid_terminal(airport_code: str, terminal_code: str) -> bool:
    """True if terminal exists at airport."""
    airport = get_airport(airport_code)
    return any(t.code == terminal_code for t in airport.terminals)


def is_valid_direction(airport_code: str, direction: str) -> bool:
    """True if direction matches airport's direction_labels."""
    airport = get_airport(airport_code)
    return direction in airport.direction_labels


def coords_to_zone(airport_code: str, lat: float, lng: float) -> str | None:
    """Find the zone a point belongs to, or None if outside all zones."""
    airport = get_airport(airport_code)
    for z in airport.zones:
        if haversine_km(z.lat, z.lng, lat, lng) <= z.radius_km:
            return z.code
    return None


def point_in_zone(airport_code: str, zone_code: str, lat: float, lng: float) -> bool:
    """Check whether (lat, lng) falls inside the zone radius."""
    z = get_zone(airport_code, zone_code)
    return haversine_km(z.lat, z.lng, lat, lng) <= z.radius_km
