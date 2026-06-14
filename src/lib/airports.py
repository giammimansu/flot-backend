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
    meeting_points: dict[str, MeetingPoint] = field(default_factory=dict)
    direction_labels: tuple[str, str] = ("TO_CITY", "FROM_CITY")

    # Live mode
    search_timeout_sec: int = 300
    max_wait_minutes: int = 20

    # Scheduled mode
    scheduled_match_window_min: int = 60
    scheduled_advance_days: int = 7

    # Shared
    match_threshold: float = 0.25
    active: bool = True

    # v4 — Elastic & Predictive
    max_detour_minutes: int = 15
    flight_tracker_provider: str = "mock"  # "aviation_edge" | "aerodatabox" | "mock"
    flight_tracker_fallback_provider: str = ""  # empty = no fallback
    places_provider: str = "google"  # "google" | "mock"

    # Sprint 5 — Payment Deadlock Resolution
    unlock_timeout_minutes: int = 120
    unlock_reminder_intervals: list[int] = field(default_factory=lambda: [30, 60, 90])
    unlock_repool_enabled: bool = True
    unlock_no_response_dissolve_hours: int = 12

    # P2 #10 — Reputation / anti-no-show
    trust_threshold: float = 0.4            # trips below this trustScore are excluded from matching
    trust_decrement_per_violation: float = 0.2  # subtracted on each no-show / no-response
    trust_ban_violations: int = 3           # hard ban after N violations

    # MVP feature-flag fields (default = no restriction / full behaviour)
    # to_airport_direction: the direction label that means "from city to airport".
    # Empty string = airport does not participate in MvpSingleRouteMode.
    to_airport_direction: str = ""
    # mvp_active_windows: list of (start_hour_inclusive, end_hour_exclusive) in airport.timezone.
    # Empty list = always active (no window gate).
    mvp_active_windows: list[tuple[int, int]] = field(default_factory=list)
    # max_origin_distance_km: gate — pairs whose origins are farther apart are excluded.
    max_origin_distance_km: float = 2.0
    # pickup_radius_m: gate — midpoint must be within this radius (metres) of each origin.
    pickup_radius_m: int = 750
    # pickup_buffer_minutes: minutes subtracted from the earliest flight departure to
    # produce the pick-up meeting time. Covers airport processing + taxi transit
    # city→airport. OUTPUT ONLY — never enters scoring/gates/threshold.
    # Opening value, to be tuned on real ride/traffic data.
    pickup_buffer_minutes: int = 0


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
        unlock_fee=199,
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
        meeting_points={
            "T1": MeetingPoint(label="Exit 4 · Arrivals", description="Ground floor · Taxi sharing stand", walk_minutes=8),
            "T2": MeetingPoint(label="Exit 2 · Arrivals", description="Ground floor · Taxi rank", walk_minutes=5),
        },
        direction_labels=("TO_MILAN", "FROM_MILAN"),
        search_timeout_sec=300,
        max_wait_minutes=20,
        scheduled_match_window_min=60,
        scheduled_advance_days=7,
        match_threshold=0.25,
        active=True,
        max_detour_minutes=15,
        flight_tracker_provider="aviation_edge",
        unlock_timeout_minutes=120,
        unlock_reminder_intervals=[30, 60, 90],
        unlock_repool_enabled=True,
        unlock_no_response_dissolve_hours=12,
        trust_threshold=0.4,
        trust_decrement_per_violation=0.2,
        trust_ban_violations=3,
        to_airport_direction="FROM_MILAN",
        mvp_active_windows=[(6, 9), (14, 17), (20, 23)],
        max_origin_distance_km=2.0,
        pickup_radius_m=750,
        # 180min = ~2h airport buffer + ~1h taxi Milano→MXP. Opening value, tune on data.
        pickup_buffer_minutes=180,
    ),
    # ── P2 #12 — Second real airport: Roma Fiumicino ─────────────────
    # Validates that nothing is hardcoded outside this registry. Same shape
    # as MXP; only the data differs (zones, terminals, direction labels).
    "FCO": AirportConfig(
        code="FCO",
        name="Roma Fiumicino",
        city="Roma",
        country="IT",
        currency="EUR",
        base_fare=11000,   # ~€110 fixed fare FCO → Roma centro
        unlock_fee=99,
        timezone="Europe/Rome",
        terminals=[
            Terminal(code="T1", label="Terminal 1"),
            Terminal(code="T3", label="Terminal 3"),
        ],
        zones=[
            Zone(code="centro",   label="Centro",    lat=41.8931, lng=12.4828, radius_km=2.5, landmarks=["Colosseo", "Termini"]),
            Zone(code="nord",     label="Nord",      lat=41.9300, lng=12.4900, radius_km=2.5, landmarks=["Parioli", "Flaminio"]),
            Zone(code="ovest",    label="Ovest",     lat=41.8990, lng=12.4400, radius_km=2.5, landmarks=["Aurelio", "Vaticano"]),
            Zone(code="sud",      label="Sud",       lat=41.8550, lng=12.4700, radius_km=2.5, landmarks=["EUR", "Ostiense"]),
            Zone(code="est",      label="Est",       lat=41.8900, lng=12.5400, radius_km=2.5, landmarks=["Pigneto", "Centocelle"]),
        ],
        meeting_points={
            "T1": MeetingPoint(label="Exit 1 · Arrivi", description="Piano terra · Taxi sharing", walk_minutes=6),
            "T3": MeetingPoint(label="Exit 3 · Arrivi", description="Piano terra · Posteggio taxi", walk_minutes=7),
        },
        direction_labels=("TO_ROME", "FROM_ROME"),
        search_timeout_sec=300,
        max_wait_minutes=20,
        scheduled_match_window_min=60,
        scheduled_advance_days=7,
        match_threshold=0.25,
        active=True,
        max_detour_minutes=15,
        flight_tracker_provider="aviation_edge",
        unlock_timeout_minutes=120,
        unlock_reminder_intervals=[30, 60, 90],
        unlock_repool_enabled=True,
        unlock_no_response_dissolve_hours=12,
        trust_threshold=0.4,
        trust_decrement_per_violation=0.2,
        trust_ban_violations=3,
    ),
    # ── Future airports (inactive until launch) ──────────────────────
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
        "matchThreshold": airport.match_threshold,
        "active": airport.active,
    }
