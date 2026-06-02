"""Flot — Stripe integration tests.

Tests cover the four hardening scenarios from plan #2:
  1. Double capture — second PI fails → first must not be charged
  2. Void on expired PI — unlock_expired handler skips void errors gracefully
  3. Webhook dedup — same event_id processed twice → second is a no-op
  4. Out-of-order webhooks — duplicate amount_capturable_updated both return 200

These tests mock the Stripe SDK (not live calls) to keep CI fast.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from tests.conftest import build_api_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_match(match_id: str, status: str = "partially_unlocked", *, table_resource) -> dict:
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
        "unlockedBy": ["u1"],
        "firstUnlockPaymentIntentId": "pi_first_123",
        "unlockDeadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    table_resource.put_item(Item=item)
    return item


def _make_trip(trip_id: str, user_id: str, status: str, *, table_resource) -> None:
    table_resource.put_item(Item={
        "pk": f"TRIP#{trip_id}",
        "sk": "META",
        "tripId": trip_id,
        "userId": user_id,
        "status": status,
        "airportCode": "MXP",
        "matchId": "m1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Scenario 1: Double capture — second PI fails → first must not be charged
# ---------------------------------------------------------------------------

class TestDoubleCaptureFailure:
    def test_second_pi_capture_fails_no_charge_to_first(self, dynamodb_table, lambda_context):
        """If second PI capture fails, cancel it and never touch first PI."""
        _make_match("m1", "partially_unlocked", table_resource=dynamodb_table)
        dynamodb_table.put_item(Item={"pk": "USER#u2", "sk": "PROFILE", "name": "User Two"})
        _make_trip("t1", "u1", "matched", table_resource=dynamodb_table)
        _make_trip("t2", "u2", "matched", table_resource=dynamodb_table)

        from handlers.matches.unlock_match import handler

        event = build_api_event(
            method="POST",
            body={"matchId": "m1"},
            user_id="u2",
            headers={"origin": "https://app.flot.it"},
        )

        with patch("handlers.matches.unlock_match.stripe") as mock_stripe, \
             patch("handlers.matches.unlock_match.cancel_unlock_timeout_schedule"), \
             patch("handlers.matches.unlock_match.put_event"), \
             patch("handlers.matches.unlock_match.get_user", return_value={"name": "User Two"}), \
             patch.dict("os.environ", {"STRIPE_SECRET_KEY": "sk_test_fake", "BETA_MODE": ""}):

            mock_intent = MagicMock()
            mock_intent.id = "pi_second_456"
            mock_intent.client_secret = "pi_second_456_secret"
            mock_stripe.PaymentIntent.create.return_value = mock_intent
            mock_stripe.error.StripeError = Exception
            # Second PI capture raises immediately
            mock_stripe.PaymentIntent.capture.side_effect = Exception("capture_failed")

            response = handler(event, lambda_context)
            body = json.loads(response["body"])

            assert response["statusCode"] == 500
            assert "No charges" in body.get("error", "")

            # Only second PI was attempted — first was never captured
            capture_calls = mock_stripe.PaymentIntent.capture.call_args_list
            assert len(capture_calls) == 1
            assert capture_calls[0] == call("pi_second_456")

            # Cancel called on second PI
            mock_stripe.PaymentIntent.cancel.assert_called_once_with("pi_second_456")


# ---------------------------------------------------------------------------
# Scenario 2: Void on expired PI — must not crash
# ---------------------------------------------------------------------------

class TestVoidOnExpiredPI:
    def test_void_expired_pi_continues_gracefully(self, dynamodb_table, lambda_context):
        """Void failure on already-expired PI must be swallowed."""
        _make_match("m2", "partially_unlocked", table_resource=dynamodb_table)
        _make_trip("t1", "u1", "matched", table_resource=dynamodb_table)
        _make_trip("t2", "u2", "matched", table_resource=dynamodb_table)

        # Pre-stub firebase_admin to avoid ModuleNotFoundError
        if "firebase_admin" not in sys.modules:
            sys.modules["firebase_admin"] = MagicMock()
            sys.modules["firebase_admin.messaging"] = MagicMock()

        # lib.notifications imports firebase_admin which is not installed locally.
        # Stub it before importing the handler module.
        if "firebase_admin" not in sys.modules:
            sys.modules["firebase_admin"] = MagicMock()
            sys.modules["firebase_admin.credentials"] = MagicMock()
            sys.modules["firebase_admin.messaging"] = MagicMock()

        from handlers.events.on_unlock_expired import handler

        with patch("handlers.events.on_unlock_expired.stripe") as mock_stripe, \
             patch("handlers.events.on_unlock_expired.get_airport") as mock_airport, \
             patch("handlers.events.on_unlock_expired.notify_user"), \
             patch("handlers.events.on_unlock_expired.cancel_all_unlock_reminders"):

            mock_airport.return_value = MagicMock(unlock_repool_enabled=False)
            mock_stripe.error.StripeError = Exception
            # PI already expired/cancelled on Stripe side
            mock_stripe.PaymentIntent.cancel.side_effect = Exception("already_canceled")

            event = {"detail": {"matchId": "m2"}}
            # Must NOT raise despite void failure
            handler(event, lambda_context)

            mock_stripe.PaymentIntent.cancel.assert_called_once_with("pi_first_123")


# ---------------------------------------------------------------------------
# Scenario 3: Webhook dedup — same event_id twice
# ---------------------------------------------------------------------------

class TestWebhookDedup:
    def _stripe_event(self, event_id: str, event_type: str = "payment_intent.amount_capturable_updated") -> dict:
        return json.dumps({
            "id": event_id,
            "type": event_type,
            "data": {"object": {"id": "pi_abc", "amount": 1000, "metadata": {"matchId": "m1"}}},
        })

    def test_first_delivery_processed(self, dynamodb_table, lambda_context):
        from handlers.webhooks import stripe_webhook

        # Patch module-level table to use mocked DynamoDB
        with patch.object(stripe_webhook, "table", dynamodb_table), \
             patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": ""}):

            event = {
                "httpMethod": "POST",
                "headers": {},
                "body": self._stripe_event("evt_001"),
                "requestContext": {},
            }
            response = stripe_webhook.handler(event, lambda_context)

        body = json.loads(response["body"])
        assert response["statusCode"] == 200
        assert body.get("received") is True
        assert body.get("duplicate") is not True

    def test_second_delivery_skipped(self, dynamodb_table, lambda_context):
        from handlers.webhooks import stripe_webhook

        with patch.object(stripe_webhook, "table", dynamodb_table), \
             patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": ""}):

            event = {
                "httpMethod": "POST",
                "headers": {},
                "body": self._stripe_event("evt_002"),
                "requestContext": {},
            }
            stripe_webhook.handler(event, lambda_context)  # first
            response = stripe_webhook.handler(event, lambda_context)  # duplicate

        body = json.loads(response["body"])
        assert response["statusCode"] == 200
        assert body.get("duplicate") is True


# ---------------------------------------------------------------------------
# Scenario 4: Out-of-order / duplicate amount_capturable_updated
# ---------------------------------------------------------------------------

class TestOutOfOrderWebhooks:
    def test_duplicate_capturable_updated_both_200(self, dynamodb_table, lambda_context):
        """Two deliveries with different event_ids for same PI must both return 200."""
        from handlers.webhooks import stripe_webhook

        def _event(eid: str) -> dict:
            return {
                "httpMethod": "POST",
                "headers": {},
                "body": json.dumps({
                    "id": eid,
                    "type": "payment_intent.amount_capturable_updated",
                    "data": {"object": {"id": "pi_xyz", "amount": 800, "metadata": {"matchId": "m3"}}},
                }),
                "requestContext": {},
            }

        with patch.object(stripe_webhook, "table", dynamodb_table), \
             patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": ""}):

            r1 = stripe_webhook.handler(_event("evt_dup_003"), lambda_context)
            r2 = stripe_webhook.handler(_event("evt_dup_003b"), lambda_context)

        assert r1["statusCode"] == 200
        assert r2["statusCode"] == 200
        # Second event is a different event_id — both new, both processed
        assert json.loads(r1["body"])["received"] is True
        assert json.loads(r2["body"])["received"] is True
