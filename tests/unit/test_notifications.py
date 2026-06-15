"""Flot — notification delivery tests (multi-channel chain WS → Push → Email, #7)."""
from __future__ import annotations

import sys
from unittest.mock import patch, MagicMock

# Stub firebase_admin before importing lib.notifications (not installed in CI).
for _m in ("firebase_admin", "firebase_admin.credentials", "firebase_admin.messaging", "firebase_admin.exceptions"):
    sys.modules.setdefault(_m, MagicMock())

from lib.notifications import notify_match_found, send_push_notification  # noqa: E402


def test_notify_match_found_offline_uses_push_not_email():
    """WS offline (count 0) + push token present → push delivers, email skipped."""
    with patch("lib.notifications.save_notification") as mock_save, \
         patch("lib.notifications.send_to_user", return_value=0) as mock_ws, \
         patch("lib.notifications.dynamo.get_item") as mock_get_item, \
         patch("lib.notifications.send_push_notification", return_value=True) as mock_push, \
         patch("lib.notifications.send_email_notification") as mock_email:

        mock_get_item.return_value = {"pushToken": "token123", "email": "test@test.com"}
        notify_match_found("u1", {"matchId": "m1"}, {"some": "data"})

        mock_save.assert_called_once()
        mock_ws.assert_called_once()
        mock_push.assert_called_once()          # WS missed → push
        mock_email.assert_not_called()          # push delivered → no email fallback


def test_notify_match_found_ws_delivered_skips_push_and_email():
    """WS online (count > 0) → push + email both skipped (dedup)."""
    with patch("lib.notifications.save_notification"), \
         patch("lib.notifications.send_to_user", return_value=1), \
         patch("lib.notifications.dynamo.get_item", return_value={"pushToken": "t", "email": "e@e.com"}), \
         patch("lib.notifications.send_push_notification") as mock_push, \
         patch("lib.notifications.send_email_notification") as mock_email:

        notify_match_found("u1", {"matchId": "m1"}, {"some": "data"})
        mock_push.assert_not_called()
        mock_email.assert_not_called()


def test_send_push_fake_door():
    with patch("lib.notifications.FAKE_DOOR_MODE", True), \
         patch("lib.notifications._get_firebase_app") as mock_firebase:
        assert send_push_notification("token123", "Title", "Body", {"data": "x"}) is True
        mock_firebase.assert_not_called()


def test_send_push_real():
    with patch("lib.notifications.FAKE_DOOR_MODE", False), \
         patch("lib.notifications._get_firebase_app"), \
         patch("lib.notifications.messaging") as mock_messaging:
        send_push_notification("token123", "Title", "Body", {"data": "x"})
        mock_messaging.send.assert_called_once()
        assert mock_messaging.Message.call_args.kwargs["token"] == "token123"


# ── Localization (it/en) ──────────────────────────────────────────────

def _push_title_for_lang(lang_value):
    """Run notify_match_found with a mocked profile, return the pushed title."""
    profile = {"pushToken": "tok", "email": "e@e.com"}
    if lang_value is not None:
        profile["lang"] = lang_value
    with patch("lib.notifications.save_notification"), \
         patch("lib.notifications.send_to_user", return_value=0), \
         patch("lib.notifications.dynamo.get_item", return_value=profile), \
         patch("lib.notifications.send_push_notification", return_value=True) as mock_push, \
         patch("lib.notifications.send_email_notification"):
        notify_match_found("u1", {"matchId": "m1"}, {"some": "data"})
    return mock_push.call_args[0][1]  # title positional arg


def test_match_found_copy_italian():
    assert _push_title_for_lang("it") == "Match trovato! 🎉"


def test_match_found_copy_english():
    assert _push_title_for_lang("en") == "Match found! 🎉"


def test_match_found_copy_defaults_english_when_lang_absent():
    assert _push_title_for_lang(None) == "Match found! 🎉"
