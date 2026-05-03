import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import atan2, cos, radians, sin, sqrt
from typing import Any

from aws_lambda_powertools import Logger

from . import dynamo
from .airports import get_airport, AirportConfig

logger = Logger(child=True)

BUCKET_MINUTES = 10  # granularità bucket per GSI1

def get_time_bucket(flight_time: str) -> str:
    """Arrotonda al bucket da 10 minuti più vicino (in UTC)."""
    dt = datetime.fromisoformat(flight_time.replace("Z", "+00:00"))
    minutes = (dt.minute // BUCKET_MINUTES) * BUCKET_MINUTES
    dt = dt.replace(minute=minutes, second=0, microsecond=0)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def get_adjacent_buckets(bucket: str, n: int = 2) -> list[str]:
    """Restituisce i bucket adiacenti (±n bucket)."""
    dt = datetime.fromisoformat(bucket.replace("Z", "+00:00"))
    buckets = []
    for i in range(-n, n + 1):
        shifted = dt + timedelta(minutes=BUCKET_MINUTES * i)
        buckets.append(shifted.isoformat().replace("+00:00", "Z"))
    return buckets

def get_adjacent_buckets_for_mode(bucket: str, mode: str, airport: AirportConfig) -> list[str]:
    """
    Scheduled: ±6 bucket (±60 min con BUCKET_MINUTES=10)
    Live:      ±2 bucket (±20 min)
    """
    if mode == "scheduled":
        n = airport.scheduled_match_window_min // BUCKET_MINUTES
    else:
        n = airport.max_wait_minutes // BUCKET_MINUTES
    return get_adjacent_buckets(bucket, n=n)

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distanza in km tra due punti GPS."""
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

def distance_score(dist_km: float) -> float:
    """Score di distanza tra destinazioni."""
    if dist_km <= 2:   return 1.0
    if dist_km <= 5:   return 0.8
    if dist_km <= 10:  return 0.5
    if dist_km <= 20:  return 0.2
    return 0.0

def time_score(bucket_a: str, bucket_b: str) -> float:
    """Score di prossimità temporale tra due bucket."""
    dt_a = datetime.fromisoformat(bucket_a.replace("Z", "+00:00"))
    dt_b = datetime.fromisoformat(bucket_b.replace("Z", "+00:00"))
    delta_min = abs((dt_a - dt_b).total_seconds()) / 60
    if delta_min == 0:                        return 1.0
    if delta_min <= BUCKET_MINUTES:           return 0.7
    if delta_min <= BUCKET_MINUTES * 2:       return 0.4
    return 0.0

def profile_score(user_a: dict, user_b: dict) -> float:
    """Bonus profilo: lingua condivisa + verifica identità."""
    score = 0.0
    if user_a.get("lang") == user_b.get("lang"):
        score += 0.1
    if user_a.get("verified") and user_b.get("verified"):
        score += 0.1
    return score

def compute_match_score(trip_a: dict, trip_b: dict, user_a: dict, user_b: dict, mode: str = "scheduled") -> float:
    """Score finale pesato diversamente per modalità."""
    dist_km = haversine_km(trip_a["destLat"], trip_a["destLng"], trip_b["destLat"], trip_b["destLng"])
    d_score = distance_score(dist_km)
    t_score = time_score(trip_a["timeBucket"], trip_b["timeBucket"])
    p_score = profile_score(user_a, user_b)

    if mode == "scheduled":
        final = (0.6 * d_score) + (0.2 * t_score) + (0.2 * p_score)
    else:
        final = (0.5 * d_score) + (0.3 * t_score) + (0.2 * p_score)
        
    logger.info("match_score_computed", dist_km=round(dist_km, 2), d_score=d_score, t_score=t_score, p_score=p_score, final=round(final, 3))
    return final

def can_match_direction(trip_a: dict, trip_b: dict) -> bool:
    """I due trip devono avere la la stessa direzione."""
    return trip_a.get("direction") == trip_b.get("direction")

@dataclass
class MatchResult:
    candidate: dict[str, Any]
    score: float

def build_match_item(trip_a: dict[str, Any], trip_b: dict[str, Any], score: float) -> dict[str, Any]:
    match_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
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
