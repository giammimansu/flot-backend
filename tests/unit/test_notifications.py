import pytest
from unittest.mock import patch, MagicMock
from lib.notifications import notify_match_found, send_push_notification, send_email_notification

def test_notify_match_found():
    with patch("lib.notifications.save_notification") as mock_save, \
         patch("lib.notifications.send_to_user") as mock_ws, \
         patch("lib.notifications.dynamo.get_item") as mock_get_item, \
         patch("lib.notifications.send_push_notification") as mock_push, \
         patch("lib.notifications.send_email_notification") as mock_email:
        
        mock_get_item.return_value = {"pushToken": "token123", "email": "test@test.com"}
        
        notify_match_found("u1", {"matchId": "m1"}, {"some": "data"})
        
        mock_save.assert_called_once()
        mock_ws.assert_called_once()
        mock_push.assert_called_once()
        mock_email.assert_called_once()

def test_send_push_fake_door():
    with patch("lib.notifications.FAKE_DOOR_MODE", True), \
         patch("lib.notifications._get_firebase_app") as mock_firebase:
        send_push_notification("token123", "Title", "Body", {"data": "x"})
        mock_firebase.assert_not_called()

def test_send_push_real():
    mock_app = MagicMock()
    with patch("lib.notifications.FAKE_DOOR_MODE", False), \
         patch("lib.notifications._get_firebase_app", return_value=mock_app), \
         patch("lib.notifications.messaging.send") as mock_send:
        send_push_notification("token123", "Title", "Body", {"data": "x"})
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert msg.token == "token123"
        assert msg.notification.title == "Title"
        assert msg.notification.body == "Body"
