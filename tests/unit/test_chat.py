"""Flot — Unit tests for #6 chat (chat_message WS handler, get_chat REST, system_message)."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import build_api_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_match(match_id: str, status: str = "unlocked", *, table_resource) -> None:
    now = datetime.now(timezone.utc).isoformat()
    table_resource.put_item(Item={
        "pk": f"MATCH#{match_id}",
        "sk": "META",
        "matchId": match_id,
        "status": status,
        "userId1": "u1",
        "userId2": "u2",
        "airportCode": "MXP",
        "createdAt": now,
        "updatedAt": now,
    })


def _make_conn(conn_id: str, user_id: str, *, table_resource) -> None:
    table_resource.put_item(Item={
        "pk": f"CONN#{conn_id}",
        "sk": "META",
        "connectionId": conn_id,
        "userId": user_id,
        "gsi3pk": f"USER#{user_id}",
        "gsi3sk": conn_id,
    })


def _ws_event(conn_id: str, body: dict) -> dict:
    return {
        "requestContext": {"connectionId": conn_id},
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# WS chat_message handler
# ---------------------------------------------------------------------------

class TestChatMessageHandler:
    def test_valid_message_persisted_and_relayed(self, dynamodb_table, lambda_context):
        _make_match("m1", table_resource=dynamodb_table)
        _make_conn("conn1", "u1", table_resource=dynamodb_table)
        dynamodb_table.put_item(Item={"pk": "USER#u2", "sk": "PROFILE", "name": "User Two"})

        from handlers.websocket.chat_message import handler

        with patch("handlers.websocket.chat_message.ws") as mock_ws:
            mock_ws.send_to_connection.return_value = True
            mock_ws.send_to_user.return_value = 1

            response = handler(
                _ws_event("conn1", {"action": "chat_message", "matchId": "m1", "text": "Ciao!"}),
                lambda_context,
            )

        assert response["statusCode"] == 200
        mock_ws.send_to_user.assert_called_once()
        args = mock_ws.send_to_user.call_args
        assert args[0][0] == "u2"  # partner
        assert args[0][1]["data"]["text"] == "Ciao!"

    def test_unknown_connection_rejected(self, dynamodb_table, lambda_context):
        from handlers.websocket.chat_message import handler

        response = handler(_ws_event("conn_unknown", {"action": "chat_message", "matchId": "m1", "text": "Hi"}), lambda_context)
        assert response["statusCode"] == 401

    def test_non_member_rejected(self, dynamodb_table, lambda_context):
        _make_match("m2", table_resource=dynamodb_table)
        _make_conn("conn_outsider", "outsider", table_resource=dynamodb_table)

        from handlers.websocket.chat_message import handler

        with patch("handlers.websocket.chat_message.ws") as mock_ws:
            mock_ws.send_to_connection.return_value = True
            response = handler(_ws_event("conn_outsider", {"matchId": "m2", "text": "Hack"}), lambda_context)

        assert response["statusCode"] == 403

    def test_non_unlocked_match_rejected(self, dynamodb_table, lambda_context):
        _make_match("m3", status="pending", table_resource=dynamodb_table)
        _make_conn("conn3", "u1", table_resource=dynamodb_table)

        from handlers.websocket.chat_message import handler

        with patch("handlers.websocket.chat_message.ws") as mock_ws:
            mock_ws.send_to_connection.return_value = True
            response = handler(_ws_event("conn3", {"matchId": "m3", "text": "Hi"}), lambda_context)

        assert response["statusCode"] == 400

    def test_text_too_long_rejected(self, dynamodb_table, lambda_context):
        _make_match("m4", table_resource=dynamodb_table)
        _make_conn("conn4", "u1", table_resource=dynamodb_table)

        from handlers.websocket.chat_message import handler

        with patch("handlers.websocket.chat_message.ws") as mock_ws:
            mock_ws.send_to_connection.return_value = True
            response = handler(_ws_event("conn4", {"matchId": "m4", "text": "x" * 1001}), lambda_context)

        assert response["statusCode"] == 400

    def test_push_fallback_when_partner_offline(self, dynamodb_table, lambda_context):
        import sys
        if "firebase_admin" not in sys.modules:
            sys.modules["firebase_admin"] = MagicMock()
            sys.modules["firebase_admin.credentials"] = MagicMock()
            sys.modules["firebase_admin.messaging"] = MagicMock()

        _make_match("m5", table_resource=dynamodb_table)
        _make_conn("conn5", "u1", table_resource=dynamodb_table)
        dynamodb_table.put_item(Item={"pk": "USER#u1", "sk": "PROFILE", "name": "User One"})
        dynamodb_table.put_item(Item={
            "pk": "USER#u2", "sk": "PROFILE", "name": "User Two", "pushToken": "tok_u2",
        })

        import lib.notifications as notif_mod
        mock_push = MagicMock()
        original = getattr(notif_mod, "send_push_notification", None)
        notif_mod.send_push_notification = mock_push

        try:
            from handlers.websocket.chat_message import handler

            with patch("handlers.websocket.chat_message.ws") as mock_ws, \
                 patch("handlers.websocket.chat_message._should_push", return_value=True), \
                 patch("handlers.websocket.chat_message._record_push"):
                mock_ws.send_to_connection.return_value = True
                mock_ws.send_to_user.return_value = 0  # partner offline
                handler(_ws_event("conn5", {"matchId": "m5", "text": "Sei online?"}), lambda_context)
        finally:
            if original is not None:
                notif_mod.send_push_notification = original

        mock_push.assert_called_once()


# ---------------------------------------------------------------------------
# REST GET /matches/:matchId/chat
# ---------------------------------------------------------------------------

class TestGetChat:
    def _seed_messages(self, match_id: str, count: int, *, table_resource) -> None:
        from datetime import datetime, timezone
        for i in range(count):
            ts = datetime(2026, 6, 2, 10, i, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
            table_resource.put_item(Item={
                "pk": f"MATCH#{match_id}",
                "sk": f"CHAT#{ts}#msg{i:03d}",
                "matchId": match_id,
                "messageId": f"msg{i:03d}",
                "type": "user",
                "senderId": "u1",
                "text": f"Message {i}",
                "createdAt": ts,
            })

    def test_returns_messages(self, dynamodb_table, lambda_context):
        _make_match("mc1", table_resource=dynamodb_table)
        self._seed_messages("mc1", 3, table_resource=dynamodb_table)

        from handlers.matches.get_chat import handler

        event = build_api_event(method="GET", path="/matches/mc1/chat",
                                path_parameters={"matchId": "mc1"}, user_id="u1")
        response = handler(event, lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body["messages"]) == 3
        assert "nextToken" not in body

    def test_non_member_forbidden(self, dynamodb_table, lambda_context):
        _make_match("mc2", table_resource=dynamodb_table)

        from handlers.matches.get_chat import handler

        event = build_api_event(method="GET", path="/matches/mc2/chat",
                                path_parameters={"matchId": "mc2"}, user_id="outsider")
        response = handler(event, lambda_context)
        assert response["statusCode"] == 403

    def test_non_unlocked_match_rejected(self, dynamodb_table, lambda_context):
        _make_match("mc3", status="pending", table_resource=dynamodb_table)

        from handlers.matches.get_chat import handler

        event = build_api_event(method="GET", path="/matches/mc3/chat",
                                path_parameters={"matchId": "mc3"}, user_id="u1")
        response = handler(event, lambda_context)
        assert response["statusCode"] == 400

    def test_pagination_returns_next_token(self, dynamodb_table, lambda_context):
        _make_match("mc4", table_resource=dynamodb_table)
        self._seed_messages("mc4", 5, table_resource=dynamodb_table)

        from handlers.matches.get_chat import handler

        event = build_api_event(method="GET", path="/matches/mc4/chat",
                                path_parameters={"matchId": "mc4"},
                                query_string={"limit": "2"}, user_id="u1")
        response = handler(event, lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body["messages"]) == 2
        assert "nextToken" in body


# ---------------------------------------------------------------------------
# System message producer
# ---------------------------------------------------------------------------

class TestSystemMessage:
    def test_post_match_confirmed(self, dynamodb_table, lambda_context):
        _make_match("sm1", table_resource=dynamodb_table)

        from handlers.chat.system_message import post_match_confirmed

        with patch("handlers.chat.system_message.ws") as mock_ws:
            mock_ws.send_to_user.return_value = 0
            msg = post_match_confirmed("sm1")

        assert msg["type"] == "system"
        assert "Match confermato" in msg["text"]
        assert msg["matchId"] == "sm1"

    def test_post_partner_unlocked(self, dynamodb_table, lambda_context):
        _make_match("sm2", table_resource=dynamodb_table)

        from handlers.chat.system_message import post_partner_unlocked

        with patch("handlers.chat.system_message.ws") as mock_ws:
            mock_ws.send_to_user.return_value = 0
            msg = post_partner_unlocked("sm2", "Marco Rossi")

        assert "Marco" in msg["text"]

    def test_post_chat_expiring(self, dynamodb_table, lambda_context):
        _make_match("sm3", table_resource=dynamodb_table)

        from handlers.chat.system_message import post_chat_expiring

        with patch("handlers.chat.system_message.ws") as mock_ws:
            mock_ws.send_to_user.return_value = 0
            msg = post_chat_expiring("sm3", hours_left=2)

        assert "2" in msg["text"]
        assert msg["type"] == "system"

    def test_system_message_persisted_in_db(self, dynamodb_table, lambda_context):
        _make_match("sm4", table_resource=dynamodb_table)

        from handlers.chat.system_message import post_system_message
        from lib.dynamo import get_chat_messages

        with patch("handlers.chat.system_message.ws") as mock_ws:
            mock_ws.send_to_user.return_value = 0
            post_system_message("sm4", "Test system message")

        messages, _ = get_chat_messages("sm4")
        assert len(messages) == 1
        assert messages[0]["type"] == "system"
        assert messages[0]["text"] == "Test system message"
        assert "senderId" not in messages[0]
