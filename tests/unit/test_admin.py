"""Flot — Unit tests for admin ops endpoints (#9)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import build_api_event

if "firebase_admin" not in sys.modules:
    sys.modules["firebase_admin"] = MagicMock()
    sys.modules["firebase_admin.credentials"] = MagicMock()
    sys.modules["firebase_admin.messaging"] = MagicMock()
    sys.modules["firebase_admin.exceptions"] = MagicMock()


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _make_match(match_id: str, status: str, *, table_resource, pi_id: str | None = None) -> None:
    item = {
        "pk": f"MATCH#{match_id}",
        "sk": "META",
        "matchId": match_id,
        "status": status,
        "userId1": "u1",
        "userId2": "u2",
        "tripId1": "t1",
        "tripId2": "t2",
        "airportCode": "MXP",
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    if pi_id:
        item["firstUnlockPaymentIntentId"] = pi_id
    table_resource.put_item(Item=item)


def _make_trip(trip_id: str, status: str, *, table_resource) -> None:
    table_resource.put_item(Item={
        "pk": f"TRIP#{trip_id}",
        "sk": "META",
        "tripId": trip_id,
        "userId": "u1",
        "airportCode": "MXP",
        "status": status,
        "flightNumber": "AZ1234",
        "flightDate": "2026-06-03",
        "flightTime": "2026-06-03T10:00:00Z",
        "createdAt": _now(),
        "updatedAt": _now(),
    })


def _admin_event(method: str, path_params: dict) -> dict:
    return {
        "httpMethod": method,
        "headers": {},
        "body": None,
        "pathParameters": path_params,
        "requestContext": {},
        "_origin": None,
        "_body": {},
    }


# ---------------------------------------------------------------------------
# void_match
# ---------------------------------------------------------------------------

class TestAdminVoidMatch:
    def test_void_partially_unlocked_match(self, dynamodb_table, lambda_context):
        _make_match("vm1", "partially_unlocked", table_resource=dynamodb_table, pi_id="pi_123")

        from handlers.admin.void_match import handler

        with patch("handlers.admin.void_match.stripe") as mock_stripe, \
             patch("handlers.admin.void_match.put_event") as mock_event, \
             patch.dict("os.environ", {"STRIPE_SECRET_KEY": "sk_test"}):

            mock_stripe.error.InvalidRequestError = Exception
            response = handler(_admin_event("POST", {"matchId": "vm1"}), lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["dissolveEmitted"] is True
        mock_event.assert_called_once()

    def test_terminal_match_skipped(self, dynamodb_table, lambda_context):
        _make_match("vm2", "completed", table_resource=dynamodb_table)

        from handlers.admin.void_match import handler

        with patch("handlers.admin.void_match.put_event") as mock_event:
            response = handler(_admin_event("POST", {"matchId": "vm2"}), lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body.get("skipped") == "already_terminal"
        mock_event.assert_not_called()

    def test_missing_match_id_returns_400(self, dynamodb_table, lambda_context):
        from handlers.admin.void_match import handler

        response = handler(_admin_event("POST", {}), lambda_context)
        assert response["statusCode"] == 400

    def test_match_not_found_returns_404(self, dynamodb_table, lambda_context):
        from handlers.admin.void_match import handler

        response = handler(_admin_event("POST", {"matchId": "no_such"}), lambda_context)
        assert response["statusCode"] == 404


# ---------------------------------------------------------------------------
# repool_trip
# ---------------------------------------------------------------------------

class TestAdminRepoolTrip:
    def test_repool_matched_trip(self, dynamodb_table, lambda_context):
        _make_trip("rt1", "matched", table_resource=dynamodb_table)

        from handlers.admin.repool_trip import handler

        response = handler(_admin_event("POST", {"tripId": "rt1"}), lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["newStatus"] == "scheduled"
        assert body["previousStatus"] == "matched"

        # Verify DB state
        item = dynamodb_table.get_item(Key={"pk": "TRIP#rt1", "sk": "META"}).get("Item")
        assert item["status"] == "scheduled"

    def test_cannot_repool_cancelled_trip(self, dynamodb_table, lambda_context):
        _make_trip("rt2", "cancelled", table_resource=dynamodb_table)

        from handlers.admin.repool_trip import handler

        response = handler(_admin_event("POST", {"tripId": "rt2"}), lambda_context)
        assert response["statusCode"] == 400

    def test_trip_not_found_returns_404(self, dynamodb_table, lambda_context):
        from handlers.admin.repool_trip import handler

        response = handler(_admin_event("POST", {"tripId": "ghost"}), lambda_context)
        assert response["statusCode"] == 404


# ---------------------------------------------------------------------------
# inspect_match
# ---------------------------------------------------------------------------

class TestAdminInspectMatch:
    def test_returns_full_match_and_trips(self, dynamodb_table, lambda_context):
        _make_match("im1", "unlocked", table_resource=dynamodb_table)
        _make_trip("t1", "matched", table_resource=dynamodb_table)
        _make_trip("t2", "matched", table_resource=dynamodb_table)

        from handlers.admin.inspect_match import handler

        response = handler(_admin_event("GET", {"matchId": "im1"}), lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["match"]["matchId"] == "im1"
        assert body["trip1"] is not None
        assert body["trip2"] is not None

    def test_match_not_found_returns_404(self, dynamodb_table, lambda_context):
        from handlers.admin.inspect_match import handler

        response = handler(_admin_event("GET", {"matchId": "ghost"}), lambda_context)
        assert response["statusCode"] == 404


# ---------------------------------------------------------------------------
# refresh_eta
# ---------------------------------------------------------------------------

class TestAdminRefreshEta:
    def test_refresh_updates_flight_time(self, dynamodb_table, lambda_context):
        _make_trip("ref1", "matched", table_resource=dynamodb_table)

        from handlers.admin.refresh_eta import handler
        from datetime import datetime, timezone

        new_eta = datetime(2026, 6, 3, 12, 30, tzinfo=timezone.utc)

        with patch("handlers.admin.refresh_eta.fetch_flight_eta", return_value=new_eta), \
             patch("handlers.admin.refresh_eta.get_time_bucket", return_value="2026-06-03T12:00:00Z"):

            response = handler(_admin_event("POST", {"tripId": "ref1"}), lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "live"
        assert "12:30" in body["eta"]

    def test_degraded_when_tracker_returns_none(self, dynamodb_table, lambda_context):
        _make_trip("ref2", "matched", table_resource=dynamodb_table)

        from handlers.admin.refresh_eta import handler

        with patch("handlers.admin.refresh_eta.fetch_flight_eta", return_value=None):
            response = handler(_admin_event("POST", {"tripId": "ref2"}), lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "degraded"

    def test_trip_without_flight_number_returns_400(self, dynamodb_table, lambda_context):
        dynamodb_table.put_item(Item={
            "pk": "TRIP#ref3",
            "sk": "META",
            "tripId": "ref3",
            "userId": "u1",
            "airportCode": "MXP",
            "status": "matched",
            "createdAt": _now(),
        })

        from handlers.admin.refresh_eta import handler

        response = handler(_admin_event("POST", {"tripId": "ref3"}), lambda_context)
        assert response["statusCode"] == 400
