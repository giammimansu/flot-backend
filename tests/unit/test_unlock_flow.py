"""Flot — unlock_match handler tests (real Stripe path, capture deferred to webhook)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from tests.conftest import build_api_event


def _seed_match(table, status: str, unlocked_by: list[str]) -> None:
    item = {
        "pk": "MATCH#m1", "sk": "META", "matchId": "m1",
        "status": status,
        "userId1": "u1", "userId2": "u2", "tripId1": "t1", "tripId2": "t2",
        "airportCode": "MXP",
        "unlockedBy": unlocked_by,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    if unlocked_by:
        item["firstUnlockPaymentIntentId"] = "pi_first"
    table.put_item(Item=item)


def _patches():
    """Standard patch stack for the real-Stripe unlock path."""
    return (
        patch("handlers.matches.unlock_match.stripe"),
        patch("handlers.matches.unlock_match.put_event"),
        patch("handlers.matches.unlock_match.get_user", return_value={"name": "User"}),
        patch("handlers.matches.unlock_match.create_unlock_timeout_schedule"),
        patch.dict("os.environ", {"STRIPE_SECRET_KEY": "sk_test", "BETA_MODE": ""}),
    )


def _run(user_id: str, lambda_context):
    from handlers.matches.unlock_match import handler
    p_stripe, p_event, p_user, p_sched, p_env = _patches()
    with p_stripe as ms, p_event, p_user, p_sched, p_env:
        intent = MagicMock()
        intent.id = "pi_second"
        intent.client_secret = "pi_second_secret"
        ms.PaymentIntent.create.return_value = intent
        ms.error.StripeError = Exception
        event = build_api_event(method="POST", body={"matchId": "m1"}, user_id=user_id)
        resp = handler(event, lambda_context)
    return resp, ms


def test_first_unlock_sets_partially_unlocked(dynamodb_table, lambda_context):
    _seed_match(dynamodb_table, "pending", [])
    resp, _ = _run("u1", lambda_context)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["matchStatus"] == "partially_unlocked"
    m = dynamodb_table.get_item(Key={"pk": "MATCH#m1", "sk": "META"})["Item"]
    assert m["status"] == "partially_unlocked"
    assert m["unlockedBy"] == ["u1"]
    assert m["firstUnlockPaymentIntentId"] == "pi_second"
    assert m.get("unlockDeadline")


def test_second_unlock_records_hold_without_capture(dynamodb_table, lambda_context):
    _seed_match(dynamodb_table, "partially_unlocked", ["u1"])
    resp, ms = _run("u2", lambda_context)

    assert resp["statusCode"] == 200
    # Capture is webhook-driven now — second unlock must NOT capture.
    assert json.loads(resp["body"])["matchStatus"] == "partially_unlocked"
    ms.PaymentIntent.capture.assert_not_called()
    m = dynamodb_table.get_item(Key={"pk": "MATCH#m1", "sk": "META"})["Item"]
    assert m["status"] == "partially_unlocked"
    assert set(m["unlockedBy"]) == {"u1", "u2"}
    assert m["secondUnlockPaymentIntentId"] == "pi_second"


def test_duplicate_unlock_rejected(dynamodb_table, lambda_context):
    _seed_match(dynamodb_table, "partially_unlocked", ["u1"])
    resp, _ = _run("u1", lambda_context)
    assert resp["statusCode"] == 400
    assert "already unlocked" in json.loads(resp["body"])["error"]


def test_unlock_wrong_user_rejected(dynamodb_table, lambda_context):
    _seed_match(dynamodb_table, "pending", [])
    resp, _ = _run("u3", lambda_context)
    assert resp["statusCode"] == 403
