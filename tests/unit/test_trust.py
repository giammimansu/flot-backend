"""Flot — Unit tests for reputation / anti-no-show (P2 #10)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

if "firebase_admin" not in sys.modules:
    sys.modules["firebase_admin"] = MagicMock()
    sys.modules["firebase_admin.credentials"] = MagicMock()
    sys.modules["firebase_admin.messaging"] = MagicMock()
    sys.modules["firebase_admin.exceptions"] = MagicMock()


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _put_user(table, user_id: str, **attrs) -> None:
    table.put_item(Item={
        "pk": f"USER#{user_id}",
        "sk": "PROFILE",
        "userId": user_id,
        "email": f"{user_id}@test.com",
        "createdAt": _now(),
        **attrs,
    })


# ── Pure helpers ──────────────────────────────────────────────────────

class TestTrustHelpers:
    def test_default_trust_when_absent(self):
        from lib.trust import get_trust_score
        assert get_trust_score({}) == 1.0

    def test_reads_existing_trust(self):
        from lib.trust import get_trust_score
        assert get_trust_score({"trustScore": Decimal("0.6")}) == 0.6

    def test_meets_threshold_default_user(self):
        from lib.trust import meets_threshold
        from lib.airports import get_airport
        assert meets_threshold({}, get_airport("MXP")) is True

    def test_below_threshold_excluded(self):
        from lib.trust import meets_threshold
        from lib.airports import get_airport
        assert meets_threshold({"trustScore": Decimal("0.2")}, get_airport("MXP")) is False

    def test_banned_excluded_even_if_score_ok(self):
        from lib.trust import meets_threshold
        from lib.airports import get_airport
        assert meets_threshold({"trustScore": Decimal("1.0"), "banned": True}, get_airport("MXP")) is False


# ── record_violation ──────────────────────────────────────────────────

class TestRecordViolation:
    def test_decrements_score(self, dynamodb_table):
        from lib.trust import record_violation, get_trust_score
        from lib.airports import get_airport

        _put_user(dynamodb_table, "u1")
        updated = record_violation("u1", "unlock_no_response", get_airport("MXP"))

        assert get_trust_score(updated) == 0.8  # 1.0 - 0.2
        assert int(updated["trustViolations"]) == 1
        assert updated.get("banned") in (None, False)

    def test_bans_after_threshold(self, dynamodb_table):
        from lib.trust import record_violation
        from lib.airports import get_airport

        airport = get_airport("MXP")  # ban after 3 violations
        _put_user(dynamodb_table, "u2")

        record_violation("u2", "unlock_no_response", airport)
        record_violation("u2", "unlock_no_response", airport)
        final = record_violation("u2", "unlock_no_response", airport)

        assert final["banned"] is True
        assert int(final["trustViolations"]) == 3

    def test_score_floored_at_zero(self, dynamodb_table):
        from lib.trust import record_violation, get_trust_score
        from lib.airports import get_airport

        airport = get_airport("MXP")
        _put_user(dynamodb_table, "u3", trustScore=Decimal("0.1"))
        updated = record_violation("u3", "unlock_no_response", airport)
        assert get_trust_score(updated) == 0.0

    def test_missing_user_no_crash(self, dynamodb_table):
        from lib.trust import record_violation
        from lib.airports import get_airport
        assert record_violation("ghost", "unlock_no_response", get_airport("MXP")) == {}
