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
# Scenario 1: Webhook-driven capture (both holds authorized → capture both)
# ---------------------------------------------------------------------------

def _seed_both_holds(table_resource, *, second: bool = True) -> None:
    """Match with first (and optionally second) authorized hold pending capture."""
    item = {
        "pk": "MATCH#m1", "sk": "META", "matchId": "m1",
        "status": "partially_unlocked",
        "userId1": "u1", "userId2": "u2", "tripId1": "t1", "tripId2": "t2",
        "airportCode": "MXP",
        "unlockedBy": ["u1", "u2"] if second else ["u1"],
        "firstUnlockPaymentIntentId": "pi_first",
        "unlockDeadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    if second:
        item["secondUnlockPaymentIntentId"] = "pi_second"
    table_resource.put_item(Item=item)


def _capturable_event(event_id: str = "evt_cap", pi: str = "pi_second", match_id: str = "m1") -> dict:
    return {
        "httpMethod": "POST",
        "headers": {},
        "body": json.dumps({
            "id": event_id,
            "type": "payment_intent.amount_capturable_updated",
            "data": {"object": {"id": pi, "metadata": {"matchId": match_id}}},
        }),
        "requestContext": {},
    }


class TestWebhookCapture:
    def test_both_authorized_captures_in_order_and_unlocks(self, dynamodb_table, lambda_context):
        _seed_both_holds(dynamodb_table)
        from handlers.webhooks import stripe_webhook

        with patch.object(stripe_webhook, "stripe") as ms, \
             patch.object(stripe_webhook, "put_event") as mev, \
             patch.object(stripe_webhook, "cancel_unlock_timeout_schedule") as mcancel, \
             patch.object(stripe_webhook, "business_metrics"), \
             patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": ""}):

            ms.error.StripeError = Exception
            authorized = MagicMock(); authorized.status = "requires_capture"
            ms.PaymentIntent.retrieve.return_value = authorized

            resp = stripe_webhook.handler(_capturable_event("evt_cap_ok"), lambda_context)

            assert resp["statusCode"] == 200
            # Capture order: second PI first, then first PI (safety ordering).
            captured = [c.args[0] for c in ms.PaymentIntent.capture.call_args_list]
            assert captured == ["pi_second", "pi_first"]
            mev.assert_called_once()
            mcancel.assert_called_once_with("m1")

        m = dynamodb_table.get_item(Key={"pk": "MATCH#m1", "sk": "META"})["Item"]
        assert m["status"] == "unlocked"
        assert "captureInProgress" not in m

    def test_second_capture_fails_no_charge_stays_partial(self, dynamodb_table, lambda_context):
        _seed_both_holds(dynamodb_table)
        from handlers.webhooks import stripe_webhook

        with patch.object(stripe_webhook, "stripe") as ms, \
             patch.object(stripe_webhook, "put_event"), \
             patch.object(stripe_webhook, "business_metrics"), \
             patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": ""}):

            ms.error.StripeError = Exception
            authorized = MagicMock(); authorized.status = "requires_capture"
            ms.PaymentIntent.retrieve.return_value = authorized
            ms.PaymentIntent.capture.side_effect = Exception("capture_failed")

            resp = stripe_webhook.handler(_capturable_event("evt_cap_fail"), lambda_context)

            assert resp["statusCode"] == 200
            # Only second PI attempted; first never captured.
            captured = [c.args[0] for c in ms.PaymentIntent.capture.call_args_list]
            assert captured == ["pi_second"]
            ms.PaymentIntent.cancel.assert_called_once_with("pi_second")

        m = dynamodb_table.get_item(Key={"pk": "MATCH#m1", "sk": "META"})["Item"]
        assert m["status"] == "partially_unlocked"   # no charge, hold reverted to pending
        assert "captureInProgress" not in m          # claim released for retry

    def test_waits_when_only_one_hold_authorized(self, dynamodb_table, lambda_context):
        _seed_both_holds(dynamodb_table, second=False)  # no second PI yet
        from handlers.webhooks import stripe_webhook

        with patch.object(stripe_webhook, "stripe") as ms, \
             patch.object(stripe_webhook, "put_event") as mev, \
             patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": ""}):

            ms.error.StripeError = Exception
            resp = stripe_webhook.handler(_capturable_event("evt_cap_wait", pi="pi_first"), lambda_context)

            assert resp["statusCode"] == 200
            ms.PaymentIntent.capture.assert_not_called()
            mev.assert_not_called()

        m = dynamodb_table.get_item(Key={"pk": "MATCH#m1", "sk": "META"})["Item"]
        assert m["status"] == "partially_unlocked"


# ---------------------------------------------------------------------------
# Scenario 1b: second unlock no longer captures synchronously (returns hold secret)
# ---------------------------------------------------------------------------

class TestSecondUnlockDefersCapture:
    def test_second_unlock_returns_client_secret_no_capture(self, dynamodb_table, lambda_context):
        _make_match("m1", "partially_unlocked", table_resource=dynamodb_table)
        dynamodb_table.put_item(Item={"pk": "USER#u2", "sk": "PROFILE", "name": "User Two"})
        _make_trip("t1", "u1", "matched", table_resource=dynamodb_table)
        _make_trip("t2", "u2", "matched", table_resource=dynamodb_table)

        from handlers.matches.unlock_match import handler

        event = build_api_event(method="POST", body={"matchId": "m1"}, user_id="u2")

        with patch("handlers.matches.unlock_match.stripe") as mock_stripe, \
             patch("handlers.matches.unlock_match.put_event"), \
             patch("handlers.matches.unlock_match.get_user", return_value={"name": "User Two"}), \
             patch.dict("os.environ", {"STRIPE_SECRET_KEY": "sk_test_fake", "BETA_MODE": ""}):

            mock_intent = MagicMock()
            mock_intent.id = "pi_second"
            mock_intent.client_secret = "pi_second_secret"
            mock_stripe.PaymentIntent.create.return_value = mock_intent
            mock_stripe.error.StripeError = Exception

            response = handler(event, lambda_context)
            body = json.loads(response["body"])

        assert response["statusCode"] == 200
        assert body["matchStatus"] == "partially_unlocked"   # capture deferred to webhook
        assert body["paymentIntentClientSecret"] == "pi_second_secret"
        mock_stripe.PaymentIntent.capture.assert_not_called()

        m = dynamodb_table.get_item(Key={"pk": "MATCH#m1", "sk": "META"})["Item"]
        assert m["status"] == "partially_unlocked"
        assert m["secondUnlockPaymentIntentId"] == "pi_second"
        assert set(m["unlockedBy"]) == {"u1", "u2"}


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
