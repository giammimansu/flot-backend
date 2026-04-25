"""Flot — Airport Registry (single source of truth).

Every airport-specific value (zones, terminals, fares, directions) lives here.
NEVER hardcode airport data elsewhere. Always use get_airport(code).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Terminal:
    """Airport terminal."""

    code: str   # "T1", "T2"
    label: str  # "Terminal 1"


@dataclass(frozen=True)
class Zone:
    """Destination zone for matching."""

    code: str            # "centro", "nord", etc.
    label: str           # "Centro Storico"
    lat: float
    lng: float
    radius_km: float
    landmarks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MeetingPoint:
    """Where matched passengers meet at the airport."""

    label: str           # "Exit 4 · Arrivals"
    description: str     # "Ground floor · Taxi sharing stand"
    walk_minutes: int    # Estimated walk time from gate


@dataclass(frozen=True)
class AirportConfig:
    """Full configuration for a supported airport."""

    code: str                # IATA code: "MXP", "FCO"
    name: str                # "Milano Malpensa"
    city: str                # "Milano"
    country: str             # "IT"
    currency: str            # "EUR"
    base_fare: int           # Full taxi fare in cents (e.g. 12000 = €120)
    unlock_fee: int          # Trip Pass price in cents (e.g. 99 = €0.99)
    timezone: str            # "Europe/Rome"
    terminals: list[Terminal] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    adjacent_zones: dict[str, list[str]] = field(default_factory=dict)
    meeting_points: dict[str, MeetingPoint] = field(default_factory=dict)
    direction_labels: tuple[str, str] = ("TO_CITY", "FROM_CITY")
    search_timeout_sec: int = 300  # 5 min default
    active: bool = True


# ─────────────────────────────────────────────────────────────────────
# Airport Registry
# Adding a new airport = adding a new entry. No other code changes.
# ─────────────────────────────────────────────────────────────────────

AIRPORTS: dict[str, AirportConfig] = {
    "MXP": AirportConfig(
        code="MXP",
        name="Milano Malpensa",
        city="Milano",
        country="IT",
        currency="EUR",
        base_fare=12000,
        unlock_fee=99,
        timezone="Europe/Rome",
        terminals=[
            Terminal(code="T1", label="Terminal 1"),
            Terminal(code="T2", label="Terminal 2"),
        ],
        zones=[
            Zone(code="centro", label="Centro",  lat=45.4642, lng=9.1900, radius_km=2.5, landmarks=["Duomo", "Navigli"]),
            Zone(code="nord",   label="Nord",    lat=45.4854, lng=9.2040, radius_km=2.5, landmarks=["Stazione Centrale", "Isola"]),
            Zone(code="ovest",  label="Ovest",   lat=45.4750, lng=9.1520, radius_km=2.5, landmarks=["CityLife", "Fiera"]),
            Zone(code="sud",    label="Sud",     lat=45.4500, lng=9.1900, radius_km=2.5, landmarks=["Bocconi", "Porta Romana"]),
            Zone(code="est",    label="Est",     lat=45.4780, lng=9.2350, radius_km=2.5, landmarks=["Lambrate", "Città Studi"]),
        ],
        adjacent_zones={
            "centro": ["nord", "ovest", "sud", "est"],
            "nord":   ["centro", "est"],
            "ovest":  ["centro"],
            "sud":    ["centro", "est"],
            "est":    ["centro", "nord", "sud"],
        },
        meeting_points={
            "T1": MeetingPoint(label="Exit 4 · Arrivals", description="Ground floor · Taxi sharing stand", walk_minutes=8),
            "T2": MeetingPoint(label="Exit 2 · Arrivals", description="Ground floor · Taxi rank", walk_minutes=5),
        },
        direction_labels=("TO_MILAN", "FROM_MILAN"),
        search_timeout_sec=300,
        active=True,
    ),
    # ── Future airports (inactive until launch) ──────────────────────
    # "FCO": AirportConfig(code="FCO", name="Roma Fiumicino", city="Roma", ...),
    # "CDG": AirportConfig(code="CDG", name="Paris Charles de Gaulle", city="Paris", ...),
    # "LHR": AirportConfig(code="LHR", name="London Heathrow", city="London", ...),
}


def get_airport(code: str) -> AirportConfig:
    """Get airport config. Raises ValueError if not found or inactive."""
    airport = AIRPORTS.get(code)
    if not airport or not airport.active:
        raise ValueError(f"Airport {code} not available")
    return airport


def get_active_airports() -> list[AirportConfig]:
    """Return all active airports for the airport picker."""
    return [a for a in AIRPORTS.values() if a.active]


def airport_to_dict(airport: AirportConfig) -> dict:
    """Serialize airport config to API-friendly dict."""
    return {
        "code": airport.code,
        "name": airport.name,
        "city": airport.city,
        "country": airport.country,
        "currency": airport.currency,
        "baseFare": airport.base_fare,
        "unlockFee": airport.unlock_fee,
        "timezone": airport.timezone,
        "terminals": [{"code": t.code, "label": t.label} for t in airport.terminals],
        "zones": [
            {
                "code": z.code,
                "label": z.label,
                "lat": z.lat,
                "lng": z.lng,
                "radiusKm": z.radius_km,
                "landmarks": z.landmarks,
            }
            for z in airport.zones
        ],
        "adjacentZones": airport.adjacent_zones,
        "meetingPoints": {
            k: {
                "label": mp.label,
                "description": mp.description,
                "walkMinutes": mp.walk_minutes,
            }
            for k, mp in airport.meeting_points.items()
        },
        "directionLabels": list(airport.direction_labels),
        "searchTimeoutSec": airport.search_timeout_sec,
    }
