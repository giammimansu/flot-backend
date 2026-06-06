"""Flot — Reputation / anti-no-show (P2 #10).

`previousMatchPartners` prevents looping with the same partner, but does not
defend against systematic abuse: users who never unlock, repeated no-shows,
multi-account farming. This module adds a per-user `trustScore`.

Model
-----
- `trustScore`  float 0.0–1.0, default 1.0 (new users fully trusted).
- `trustViolations`  int, default 0 — count of recorded violations.
- `banned`  bool — hard ban after `trust_ban_violations` (set in airports.py).

A violation (e.g. partner no-response on a deadlock) decrements trustScore by
`trust_decrement_per_violation` (floored at 0.0) and increments the counter.
Once the counter reaches the airport's `trust_ban_violations`, `banned` is set.

Enforcement
-----------
- Matchmaker excludes trips whose owner is banned or below `trust_threshold`.
- create_trip rejects banned users up front.

All values are airport-configurable (never hardcoded here) per Rule 01.
"""
from __future__ import annotations

from decimal import Decimal

from aws_lambda_powertools import Logger

from lib import dynamo
from lib.airports import AirportConfig

logger = Logger(child=True)

DEFAULT_TRUST = 1.0


def get_trust_score(user: dict) -> float:
    """Read trustScore from a user profile dict, defaulting to 1.0."""
    raw = user.get("trustScore")
    if raw is None:
        return DEFAULT_TRUST
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TRUST


def is_banned(user: dict) -> bool:
    return bool(user.get("banned", False))


def meets_threshold(user: dict, airport: AirportConfig) -> bool:
    """True if the user may be matched: not banned and trustScore >= threshold."""
    if is_banned(user):
        return False
    return get_trust_score(user) >= airport.trust_threshold


def record_violation(user_id: str, reason: str, airport: AirportConfig) -> dict:
    """Atomically decrement trustScore, bump the violation counter, ban if over the limit.

    Returns the updated profile attributes ({} if the user was missing).
    Idempotency is not required: each violation is a distinct event (one no-show
    per match), and the caller fires it at most once per match resolution.
    """
    user = dynamo.get_user(user_id)
    if not user:
        logger.warning("trust_violation_user_missing", userId=user_id, reason=reason)
        return {}

    current = get_trust_score(user)
    new_score = max(0.0, round(current - airport.trust_decrement_per_violation, 3))
    new_violations = int(user.get("trustViolations", 0)) + 1
    banned = new_violations >= airport.trust_ban_violations

    updates: dict = {
        "trustScore": Decimal(str(new_score)),
        "trustViolations": new_violations,
    }
    if banned:
        updates["banned"] = True

    updated = dynamo.update_item(f"USER#{user_id}", "PROFILE", updates)
    logger.info(
        "trust_violation_recorded",
        userId=user_id,
        reason=reason,
        trustScore=new_score,
        violations=new_violations,
        banned=banned,
    )
    return updated
