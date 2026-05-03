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
         patch("lib.notifications.sns.publish") as mock_sns:
        send_push_notification("token123", "Title", "Body", {"data": "x"})
        mock_sns.assert_not_called()

def test_send_push_real():
    with patch("lib.notifications.FAKE_DOOR_MODE", False), \
         patch("lib.notifications.SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:flot-push"), \
         patch("lib.notifications.sns.publish") as mock_sns:
        send_push_notification("token123", "Title", "Body", {"data": "x"})
        mock_sns.assert_called_once()
