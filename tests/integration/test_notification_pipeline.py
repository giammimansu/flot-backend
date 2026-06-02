"""Flot — Notification pipeline integration tests (#7).

Scenarios:
  1. Full pipeline: token registered → match → user offline → push → email fallback
  2. WS delivered → push and email skipped (dedup)
  3. Dead push token → token removed from profile, email fallback fires
  4. Token invalid → silent skip (no crash), email sent
  5. No push token → email fallback directly
  6. always_email=True → email sent even when WS delivered
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# Stub firebase_admin before any notifications import
if "firebase_admin" not in sys.modules:
    sys.modules["firebase_admin"] = MagicMock()
    sys.modules["firebase_admin.credentials"] = MagicMock()
    sys.modules["firebase_admin.messaging"] = MagicMock()
    sys.modules["firebase_admin.exceptions"] = MagicMock()


def _make_user(user_id: str, *, table_resource, push_token: str | None = None, email: str | None = None) -> None:
    item: dict = {
        "pk": f"USER#{user_id}",
        "sk": "PROFILE",
        "userId": user_id,
        "name": f"User {user_id}",
    }
    if push_token:
        item["pushToken"] = push_token
    if email:
        item["email"] = email
    table_resource.put_item(Item=item)


# ---------------------------------------------------------------------------
# Scenario 1: Offline user — WS miss → push → email fallback
# ---------------------------------------------------------------------------

class TestOfflineDelivery:
    def test_push_sent_when_ws_misses(self, dynamodb_table, lambda_context):
        _make_user("u_offline", table_resource=dynamodb_table, push_token="tok_abc", email="u@test.com")

        from lib.notifications import deliver

        with patch("lib.notifications.send_to_user", return_value=0) as mock_ws, \
             patch("lib.notifications.send_push_notification", return_value=True) as mock_push, \
             patch("lib.notifications.send_email_notification", return_value=True) as mock_email:

            result = deliver("u_offline", "Test", "Body", {"type": "test"})

        mock_ws.assert_called_once()
        mock_push.assert_called_once()
        # Email skipped because push delivered
        mock_email.assert_not_called()
        assert result.push is True
        assert result.ws is False

    def test_email_sent_when_push_also_fails(self, dynamodb_table, lambda_context):
        _make_user("u_nopush", table_resource=dynamodb_table, email="u2@test.com")

        from lib.notifications import deliver

        with patch("lib.notifications.send_to_user", return_value=0), \
             patch("lib.notifications.send_email_notification", return_value=True) as mock_email:

            result = deliver("u_nopush", "Test", "Body", {"type": "test"})

        mock_email.assert_called_once()
        assert result.email is True


# ---------------------------------------------------------------------------
# Scenario 2: WS delivered → push and email skipped
# ---------------------------------------------------------------------------

class TestWsDedup:
    def test_push_skipped_when_ws_delivers(self, dynamodb_table, lambda_context):
        _make_user("u_online", table_resource=dynamodb_table, push_token="tok_xyz", email="u3@test.com")

        from lib.notifications import deliver

        with patch("lib.notifications.send_to_user", return_value=1) as mock_ws, \
             patch("lib.notifications.send_push_notification") as mock_push, \
             patch("lib.notifications.send_email_notification") as mock_email:

            result = deliver("u_online", "Test", "Body", {"type": "test"})

        mock_ws.assert_called_once()
        mock_push.assert_not_called()
        mock_email.assert_not_called()
        assert result.ws is True
        assert result.push is False
        assert result.email is False


# ---------------------------------------------------------------------------
# Scenario 3: Dead push token → removed from profile + email fallback
# ---------------------------------------------------------------------------

class TestDeadTokenCleanup:
    def test_dead_token_removed_and_email_sent(self, dynamodb_table, lambda_context):
        _make_user("u_dead", table_resource=dynamodb_table, push_token="tok_dead", email="u4@test.com")

        from lib.notifications import deliver, _DeadTokenError

        with patch("lib.notifications.send_to_user", return_value=0), \
             patch("lib.notifications.send_push_notification", side_effect=_DeadTokenError("tok_dead")) as mock_push, \
             patch("lib.notifications.send_email_notification", return_value=True) as mock_email, \
             patch("lib.notifications._clear_dead_token") as mock_clear:

            result = deliver("u_dead", "Test", "Body", {"type": "test"})

        mock_push.assert_called_once()
        mock_clear.assert_called_once_with("u_dead", "tok_dead")
        mock_email.assert_called_once()
        assert result.push is False
        assert result.email is True

    def test_clear_dead_token_removes_from_db(self, dynamodb_table, lambda_context):
        _make_user("u_dead2", table_resource=dynamodb_table, push_token="tok_stale")

        from lib.notifications import _clear_dead_token
        from lib.dynamo import get_user

        _clear_dead_token("u_dead2", "tok_stale")

        user = get_user("u_dead2")
        assert user is not None
        assert user.get("pushToken") is None


# ---------------------------------------------------------------------------
# Scenario 4: No push token → email fallback directly
# ---------------------------------------------------------------------------

class TestNoPushToken:
    def test_email_fallback_without_push_token(self, dynamodb_table, lambda_context):
        _make_user("u_ntoken", table_resource=dynamodb_table, email="u5@test.com")

        from lib.notifications import deliver

        with patch("lib.notifications.send_to_user", return_value=0), \
             patch("lib.notifications.send_push_notification") as mock_push, \
             patch("lib.notifications.send_email_notification", return_value=True) as mock_email:

            result = deliver("u_ntoken", "Test", "Body", {"type": "test"})

        mock_push.assert_not_called()
        mock_email.assert_called_once()
        assert result.email is True


# ---------------------------------------------------------------------------
# Scenario 5: always_email=True → email sent even when WS delivered
# ---------------------------------------------------------------------------

class TestAlwaysEmail:
    def test_email_sent_despite_ws_delivery(self, dynamodb_table, lambda_context):
        _make_user("u_always", table_resource=dynamodb_table, email="u6@test.com")

        from lib.notifications import deliver

        with patch("lib.notifications.send_to_user", return_value=1), \
             patch("lib.notifications.send_email_notification", return_value=True) as mock_email:

            result = deliver("u_always", "Test", "Body", {"type": "test"}, always_email=True)

        mock_email.assert_called_once()
        assert result.ws is True
        assert result.email is True


# ---------------------------------------------------------------------------
# Scenario 6: persist=False → no DynamoDB write
# ---------------------------------------------------------------------------

class TestPersistFlag:
    def test_no_db_write_when_persist_false(self, dynamodb_table, lambda_context):
        _make_user("u_nodb", table_resource=dynamodb_table)

        from lib.notifications import deliver
        from lib.dynamo import get_item

        with patch("lib.notifications.send_to_user", return_value=1), \
             patch("lib.notifications.save_notification") as mock_save:

            deliver("u_nodb", "Test", "Body", {"type": "test"}, persist=False)

        mock_save.assert_not_called()
