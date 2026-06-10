import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import atan2, cos, radians, sin, sqrt
from typing import Any
from zoneinfo import ZoneInfo

from aws_lambda_powertools import Logger

from . import dynamo
from .airports import get_airport, AirportConfig

logger = Logger(child=True)

BUCKET_MINUTES = 10
_LIVE_TOLERANCE_MIN = 30
_SLOT_DURATION_MIN = 60

# ── Bucket helpers ────────────────────────────────────────────────────

def get_time_bucket(flight_time: str) -> str:
    dt = datetime.fromisoformat(flight_time.replace("Z", "+00:00"))
    minutes = (dt.minute // BUCKET_MINUTES) * BUCKET_MINUTES
    dt = dt.replace(minute=minutes, second=0, microsecond=0)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def get_slot_bucket(flight_time: str, slot_duration_min: int) -> str:
    dt = datetime.fromisoformat(flight_time.replace("Z", "+00:00"))
    total_min = dt.hour * 60 + dt.minute
    floored = (total_min // slot_duration_min) * slot_duration_min
    dt = dt.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def get_adjacent_buckets(bucket: str, n: int = 2) -> list[str]:
    dt = datetime.fromisoformat(bucket.replace("Z", "+00:00"))
    buckets = []
    for i in range(-n, n + 1):
        shifted = dt + timedelta(minutes=BUCKET_MINUTES * i)
        buckets.append(shifted.isoformat().replace("+00:00", "Z"))
    return buckets

def get_adjacent_slots(bucket: str, slot_duration_min: int, n: int = 1) -> list[str]:
    dt = datetime.fromisoformat(bucket.replace("Z", "+00:00"))
    return [
        (dt + timedelta(minutes=slot_duration_min * i)).isoformat().replace("+00:00", "Z")
        for i in range(-n, n + 1)
    ]

def get_adjacent_buckets_for_mode(bucket: str, mode: str, airport: AirportConfig) -> list[str]:
    if mode == "scheduled":
        n = airport.scheduled_match_window_min // BUCKET_MINUTES
    else:
        n = airport.max_wait_minutes // BUCKET_MINUTES
    return get_adjacent_buckets(bucket, n=n)

# ── Distance ─────────────────────────────────────────────────────────

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    lat1, lng1, lat2, lng2 = float(lat1), float(lng1), float(lat2), float(lng2)
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

# ── Score components ──────────────────────────────────────────────────

def distance_score(dist_km: float) -> float:
    if dist_km <= 2:   return 1.0
    if dist_km <= 5:   return 0.8
    if dist_km <= 10:  return 0.5
    if dist_km <= 20:  return 0.2
    return 0.0

def luggage_score(luggage_a: int, luggage_b: int) -> float:
    total = luggage_a + luggage_b
    if total <= 3: return 1.0
    if total <= 4: return 0.8
    if total <= 5: return 0.4
    return 0.0

def time_score(bucket_a: str, bucket_b: str) -> float:
    if not bucket_a or not bucket_b:
        return 0.5
    dt_a = datetime.fromisoformat(bucket_a.replace("Z", "+00:00"))
    dt_b = datetime.fromisoformat(bucket_b.replace("Z", "+00:00"))
    delta_min = abs((dt_a - dt_b).total_seconds()) / 60
    if delta_min == 0:                        return 1.0
    if delta_min <= BUCKET_MINUTES:           return 0.7
    if delta_min <= BUCKET_MINUTES * 2:       return 0.4
    return 0.0

def profile_score(user_a: dict, user_b: dict) -> float:
    score = 0.0
    if user_a.get("lang") == user_b.get("lang"):
        score += 0.1
    if user_a.get("verified") and user_b.get("verified"):
        score += 0.1
    return score

# ── v4 — Dynamic threshold ────────────────────────────────────────────

def compute_dynamic_threshold(
    base_threshold: float,
    flight_time_a: str,
    flight_time_b: str,
    now: datetime,
) -> float:
    """
    Time-decay threshold: more selective far from flight, more lenient near lock window.

    Curve:
    - ≥168h (7d) → 0.70
    - ≥48h       → 0.50
    - ≥24h       → 0.35
    - ≥6h        → base_threshold (typically 0.25)
    - <6h        → max(0.20, base_threshold - 0.05)
    """
    dt_a = datetime.fromisoformat(flight_time_a.replace("Z", "+00:00"))
    dt_b = datetime.fromisoformat(flight_time_b.replace("Z", "+00:00"))
    hours_to_flight = min(
        (dt_a - now).total_seconds() / 3600,
        (dt_b - now).total_seconds() / 3600,
    )
    hours_to_flight = max(0, hours_to_flight)

    if hours_to_flight >= 168:  return 0.70
    if hours_to_flight >= 48:   return 0.50
    if hours_to_flight >= 24:   return 0.35
    if hours_to_flight >= 6:    return base_threshold
    return max(0.20, base_threshold - 0.05)

# ── v4 — Detour corridor ──────────────────────────────────────────────

def estimate_detour_minutes(
    trip_a: dict,
    trip_b: dict,
    airport: AirportConfig,
) -> float:
    """
    Estimates driver detour in minutes to serve both destinations.
    MVP: haversine approximation, no routing API. Replace with Google Routes in v4.1.
    Uses airport.zones[0] as airport center approximation.
    """
    airport_lat = airport.zones[0].lat
    airport_lng = airport.zones[0].lng

    d_airport_to_a = haversine_km(airport_lat, airport_lng, trip_a["destLat"], trip_a["destLng"])
    d_airport_to_b = haversine_km(airport_lat, airport_lng, trip_b["destLat"], trip_b["destLng"])
    d_a_to_b = haversine_km(trip_a["destLat"], trip_a["destLng"], trip_b["destLat"], trip_b["destLng"])

    route_ab = d_airport_to_a + d_a_to_b
    route_ba = d_airport_to_b + d_a_to_b
    direct = max(d_airport_to_a, d_airport_to_b)

    detour_km = min(route_ab, route_ba) - direct
    URBAN_SPEED_KMH = 30
    return max(0.0, (detour_km / URBAN_SPEED_KMH) * 60)


def apply_detour_penalty(score: float, detour_min: float, max_detour_min: int) -> float:
    """
    Penalizes score for inefficient driver routes.

    - 0–5 min:  no penalty
    - 5–15 min: linear penalty up to -0.2
    - >15 min:  fixed -0.3 (V-route — near-impossible to match)
    """
    if detour_min <= 5:
        return score
    elif detour_min <= 15:
        penalty = 0.2 * ((detour_min - 5) / 10)
        return max(0.0, score - penalty)
    else:
        return max(0.0, score - 0.3)

# ── Compatibility ─────────────────────────────────────────────────────

def can_match_direction(trip_a: dict, trip_b: dict) -> bool:
    return trip_a.get("direction") == trip_b.get("direction")

def can_match_modes(trip_a: dict, trip_b: dict) -> bool:
    mode_a, mode_b = trip_a.get("mode"), trip_b.get("mode")
    if mode_a == mode_b:
        return True
    live = trip_a if mode_a == "live" else trip_b
    sched = trip_a if mode_a == "scheduled" else trip_b
    slot_start = datetime.fromisoformat(sched["arrivalSlot"].replace("Z", "+00:00"))
    slot_end = slot_start + timedelta(minutes=_SLOT_DURATION_MIN)
    live_time = datetime.fromisoformat(live["createdAt"].replace("Z", "+00:00"))
    return (slot_start - timedelta(minutes=_LIVE_TOLERANCE_MIN)) <= live_time <= slot_end

def compute_match_score(
    trip_a: dict,
    trip_b: dict,
    user_a: dict,
    user_b: dict,
    mode: str = "scheduled",
    airport: "AirportConfig | None" = None,
) -> float:
    if airport is not None:
        lat_a, lng_a = get_match_coords(trip_a, airport)
        lat_b, lng_b = get_match_coords(trip_b, airport)
    else:
        lat_a, lng_a = float(trip_a["destLat"]), float(trip_a["destLng"])
        lat_b, lng_b = float(trip_b["destLat"]), float(trip_b["destLng"])
    dist_km = haversine_km(lat_a, lng_a, lat_b, lng_b)
    d_score = distance_score(dist_km)
    t_score = time_score(trip_a.get("timeBucket", ""), trip_b.get("timeBucket", ""))
    p_score = profile_score(user_a, user_b)

    if mode == "scheduled":
        final = (0.6 * d_score) + (0.2 * t_score) + (0.2 * p_score)
    else:
        final = (0.5 * d_score) + (0.3 * t_score) + (0.2 * p_score)

    logger.info(
        "match_score_computed",
        dist_km=round(dist_km, 2),
        d_score=d_score,
        t_score=t_score,
        p_score=p_score,
        final=round(final, 3),
    )
    return final

# ── v4 — Time compatibility check ────────────────────────────────────

def check_time_compatibility(
    trip_a: dict,
    trip_b: dict,
    airport: AirportConfig,
    mode: str = "scheduled",
) -> bool:
    """
    Returns True if both trips' flightTimes are within the match window.
    Used by on_flight_delayed to decide if a delay breaks an existing match.
    """
    ft_a = trip_a.get("flightTime")
    ft_b = trip_b.get("flightTime")
    if not ft_a or not ft_b:
        return False
    dt_a = datetime.fromisoformat(ft_a.replace("Z", "+00:00"))
    dt_b = datetime.fromisoformat(ft_b.replace("Z", "+00:00"))
    delta_min = abs((dt_a - dt_b).total_seconds()) / 60
    window = airport.scheduled_match_window_min if mode == "scheduled" else airport.max_wait_minutes
    return delta_min <= window


# ── MVP helpers ───────────────────────────────────────────────────────

def get_match_coords(trip: dict, airport: AirportConfig) -> tuple[float, float]:
    """
    Returns the coordinate pair to use for distance-based matching.
    For TO_AIRPORT trips, uses the city departure origin (originLat/Lng).
    For all other directions, uses the trip destination (destLat/Lng).
    """
    if (
        airport.to_airport_direction
        and trip.get("direction") == airport.to_airport_direction
        and trip.get("originLat") is not None
    ):
        return float(trip["originLat"]), float(trip["originLng"])
    return float(trip["destLat"]), float(trip["destLng"])


def is_in_active_window(airport: AirportConfig, now_utc: datetime) -> bool:
    """Returns True if now_utc falls within any of airport.mvp_active_windows (local time)."""
    if not airport.mvp_active_windows:
        return True
    local_now = now_utc.astimezone(ZoneInfo(airport.timezone))
    h = local_now.hour
    return any(start <= h < end for start, end in airport.mvp_active_windows)


def next_active_window_label(airport: AirportConfig, now_utc: datetime) -> str:
    """Returns a human-readable label for the next active matching window."""
    if not airport.mvp_active_windows:
        return "adesso"
    local_now = now_utc.astimezone(ZoneInfo(airport.timezone))
    h = local_now.hour
    for start, _ in sorted(airport.mvp_active_windows):
        if start > h:
            return f"{start:02d}:00"
    first_start = min(s for s, _ in airport.mvp_active_windows)
    return f"domani alle {first_start:02d}:00"


def compute_pickup_point(trip_a: dict, trip_b: dict, airport: AirportConfig) -> dict:
    """
    Computes the geometric midpoint of the two trip origin coordinates and
    resolves the nearest Zone from the airport registry for labelling.

    The returned lat/lng are the real midpoint coordinates — the Zone is
    used only as a human-readable label (zoneCode / zoneLabel / landmarks).
    The Zone center is never used as the meeting coordinate.

    # TODO MvpRouteApiEnabled: in futuro, snap del midpoint a un punto pedonale
    # realmente raggiungibile via Places (evita midpoint che cadono in aree
    # non accessibili a piedi — parchi recintati, tangenziali, isolati chiusi).
    """
    lat_a, lng_a = get_match_coords(trip_a, airport)
    lat_b, lng_b = get_match_coords(trip_b, airport)
    mid_lat = (lat_a + lat_b) / 2
    mid_lng = (lng_a + lng_b) / 2

    nearest_zone = min(airport.zones, key=lambda z: haversine_km(mid_lat, mid_lng, z.lat, z.lng))
    return {
        "lat": mid_lat,
        "lng": mid_lng,
        "zoneCode": nearest_zone.code,
        "zoneLabel": nearest_zone.label,
        "landmarks": list(nearest_zone.landmarks),
    }


def compute_pickup_time(trip_a: dict, trip_b: dict, airport: AirportConfig) -> str | None:
    """
    Pick-up meeting time = earliest departure − airport.pickup_buffer_minutes.

    Uses the EARLIEST of the two flightTimes (min): the trip departing first
    dictates the meeting so nobody misses their flight. Output ISO 8601 UTC.
    Returns None if either flightTime is missing.

    OUTPUT ONLY — independent of compute_match_score / time_score / distance
    gates / dynamic threshold. The buffer never influences matching.
    """
    ft_a = trip_a.get("flightTime")
    ft_b = trip_b.get("flightTime")
    if not ft_a or not ft_b:
        return None
    dt_a = datetime.fromisoformat(ft_a.replace("Z", "+00:00"))
    dt_b = datetime.fromisoformat(ft_b.replace("Z", "+00:00"))
    earliest = min(dt_a, dt_b)
    pickup = earliest - timedelta(minutes=airport.pickup_buffer_minutes)
    return pickup.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ── Find best match (v3 greedy — replaced by build_compatibility_matrix in v4) ──

def find_best_match(query_trip: dict, query_user: dict) -> "MatchResult | None":
    airport = get_airport(query_trip["airportCode"])
    bucket = query_trip.get("timeBucket") or get_time_bucket(query_trip["flightTime"])
    buckets = get_adjacent_buckets_for_mode(bucket, query_trip.get("mode", "scheduled"), airport)

    best: "MatchResult | None" = None
    for b in buckets:
        candidates = dynamo.query_gsi(
            index_name="GSI1-TimeBucket",
            pk_name="gsi1pk",
            pk_value=f"{query_trip['airportCode']}#{b}",
        )
        for c in candidates:
            if c["pk"] == query_trip["pk"]:
                continue
            if c["userId"] == query_trip["userId"]:
                continue
            if c.get("status") not in ("scheduled", None):
                continue
            if not can_match_direction(query_trip, c):
                continue
            c_user = dynamo.get_item(f"USER#{c['userId']}", "PROFILE") or {}
            score = compute_match_score(query_trip, c, query_user, c_user, mode=query_trip.get("mode", "scheduled"))
            if score >= airport.match_threshold and (best is None or score > best.score):
                best = MatchResult(candidate=c, score=score)
    return best

# ── Entities ──────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    candidate: dict[str, Any]
    score: float

def build_match_item(
    trip_a: dict[str, Any],
    trip_b: dict[str, Any],
    score: float,
    pickup_point: dict[str, Any] | None = None,
    pickup_time: str | None = None,
) -> dict[str, Any]:
    match_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    item: dict[str, Any] = {
        "pk": f"MATCH#{match_id}",
        "sk": "META",
        "matchId": match_id,
        "airportCode": trip_a["airportCode"],
        "tripId1": trip_a["tripId"],
        "tripId2": trip_b["tripId"],
        "userId1": trip_a["userId"],
        "userId2": trip_b["userId"],
        "status": "pending",
        "score": str(round(score, 4)),
        "unlockedBy": [],
        "createdAt": now,
        "mode1": trip_a.get("mode"),
        "mode2": trip_b.get("mode"),
    }
    if pickup_point:
        item["pickupPoint"] = pickup_point
    if pickup_time:
        item["pickupTime"] = pickup_time
    return item
