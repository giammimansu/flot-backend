"""Flot — Notification utilities.

Multi-channel delivery chain:  WebSocket → Push → Email
Dedup: if WS delivered, Push is skipped. Email always sent unless
always_email=False (default) and WS or Push already delivered.

Dead token cleanup: Firebase `registration-token-not-registered` or
`invalid-registration-token` errors cause the pushToken to be removed
from the user's DynamoDB profile.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field

import boto3
import firebase_admin
from firebase_admin import credentials, messaging
from aws_lambda_powertools import Logger

from lib import dynamo
from lib.websocket import send_to_user

logger = Logger()

ses = boto3.client("ses")

FAKE_DOOR_MODE = os.environ.get("FAKE_DOOR_MODE", "true").lower() == "true"
FIREBASE_CREDENTIALS_SECRET_ARN = os.environ.get("FIREBASE_CREDENTIALS_SECRET_ARN")
SES_FROM_EMAIL = os.environ.get("SES_FROM_EMAIL")

# Firebase error codes that mean the token is permanently invalid.
_DEAD_TOKEN_CODES = {
    "registration-token-not-registered",
    "invalid-registration-token",
    "mismatched-credential",
}

# Firebase app singleton — initialized once per Lambda container.
_firebase_app: firebase_admin.App | None = None


def _get_firebase_app() -> firebase_admin.App:
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app
    if not FIREBASE_CREDENTIALS_SECRET_ARN:
        raise RuntimeError("FIREBASE_CREDENTIALS_SECRET_ARN not configured")
    sm = boto3.client("secretsmanager")
    secret = sm.get_secret_value(SecretId=FIREBASE_CREDENTIALS_SECRET_ARN)
    cred_dict = json.loads(secret["SecretString"])
    cred = credentials.Certificate(cred_dict)
    _firebase_app = firebase_admin.initialize_app(cred)
    return _firebase_app


# ── Persistence ──────────────────────────────────────────────────────


def save_notification(user_id: str, title: str, body: str, payload: dict) -> dict:
    """Save a notification to DynamoDB for the user (in-app notification feed)."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    notif_id = str(uuid.uuid4())
    item = {
        "pk": f"USER#{user_id}",
        "sk": f"NOTIF#{now_iso}#{notif_id}",
        "type": "notification",
        "title": title,
        "body": body,
        "payload": payload,
        "createdAt": now_iso,
        "read": False,
        "ttl": int(time.time()) + 30 * 24 * 60 * 60,
    }
    dynamo.put_item(item)
    return item


# ── Individual channel senders ────────────────────────────────────────


def send_push_notification(token: str, title: str, body: str, payload: dict) -> bool:
    """Send a push notification. Returns True on success, False on failure.

    Dead tokens (registration-token-not-registered etc.) are detected and
    the caller should remove the token — this function does NOT mutate DB.
    Raises _DeadTokenError for dead-token cases so callers can clean up.
    """
    if FAKE_DOOR_MODE:
        logger.info("FAKE_DOOR_MODE: Sent PUSH", extra={"token": token, "title": title})
        return True

    try:
        _get_firebase_app()
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in payload.items() if isinstance(v, (str, int, float, bool))},
            token=token,
        )
        messaging.send(message)
        logger.info("push_sent", token=token[:20], title=title)
        return True
    except firebase_admin.exceptions.FirebaseError as e:
        error_code = getattr(e, "code", "") or ""
        if any(dead in error_code for dead in _DEAD_TOKEN_CODES):
            logger.warning("push_dead_token", token=token[:20])
            raise _DeadTokenError(token) from e
        logger.error("push_failed", error=str(e))
        return False
    except Exception as e:
        logger.error("push_unexpected", error=str(e))
        return False


def send_email_notification(email: str, subject: str, message: str) -> bool:
    """Send an email via SES. Returns True on success, False on failure."""
    if FAKE_DOOR_MODE:
        logger.info("FAKE_DOOR_MODE: Sent EMAIL", extra={"email": email, "subject": subject})
        return True

    if not SES_FROM_EMAIL:
        logger.warning("ses_from_email_missing")
        return False

    try:
        ses.send_email(
            Source=SES_FROM_EMAIL,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": message, "Charset": "UTF-8"}},
            },
        )
        logger.info("email_sent", email=email, subject=subject)
        return True
    except Exception as e:
        logger.error("email_failed", error=str(e))
        return False


class _DeadTokenError(Exception):
    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(f"dead_token:{token[:20]}")


def _clear_dead_token(user_id: str, dead_token: str) -> None:
    """Remove a confirmed-dead push token from the user's profile."""
    try:
        user = dynamo.get_user(user_id)
        if user and user.get("pushToken") == dead_token:
            dynamo.update_item(f"USER#{user_id}", "PROFILE", {"pushToken": None})
            logger.info("dead_token_removed", userId=user_id)
    except Exception as e:
        logger.warning("dead_token_removal_failed", userId=user_id, error=str(e))


# ── High-level delivery chain ────────────────────────────────────────


@dataclass
class DeliveryResult:
    """Records which channels successfully delivered."""
    ws: bool = False
    push: bool = False
    email: bool = False

    @property
    def delivered(self) -> bool:
        return self.ws or self.push or self.email

    @property
    def channels(self) -> list[str]:
        return [ch for ch, ok in [("ws", self.ws), ("push", self.push), ("email", self.email)] if ok]


def deliver(
    user_id: str,
    title: str,
    body: str,
    payload: dict,
    *,
    always_email: bool = False,
    persist: bool = True,
) -> DeliveryResult:
    """Multi-channel delivery with explicit chain and cross-channel dedup.

    Chain:
      1. WS — if user online (send_to_user returns > 0)
      2. Push — if WS missed AND pushToken present
      3. Email — if always_email=True OR both WS+push missed AND email present

    Dead push tokens are automatically removed from the user profile.
    If persist=True, also saves to the in-app notification feed.
    """
    result = DeliveryResult()

    if persist:
        save_notification(user_id, title, body, payload)

    # 1. WebSocket
    ws_count = send_to_user(user_id, {"type": payload.get("type", "notification"), "data": {**payload, "title": title, "body": body}})
    result.ws = ws_count > 0

    user = dynamo.get_item(f"USER#{user_id}", "PROFILE") or {}

    # 2. Push — only if WS did not deliver
    if not result.ws and user.get("pushToken"):
        try:
            result.push = send_push_notification(user["pushToken"], title, body, payload)
        except _DeadTokenError as e:
            _clear_dead_token(user_id, e.token)
            result.push = False

    # 3. Email — if always_email OR neither WS nor push delivered
    if user.get("email") and (always_email or not result.delivered):
        result.email = send_email_notification(user["email"], title, body)

    logger.info(
        "notification_delivered",
        userId=user_id,
        channels=result.channels,
        delivered=result.delivered,
    )
    return result


# ── Convenience wrappers (preserve existing call-sites) ───────────────


def notify_match_found(user_id: str, match_data: dict, match_context_for_user: dict) -> DeliveryResult:
    """Notify a user that a match was found."""
    title = "Match trovato! 🎉"
    body = "Abbiamo trovato un partner per il tuo viaggio. Sblocca per chattare!"
    return deliver(user_id, title, body, {**match_context_for_user, "type": "match.found"})


def notify_user(user_id: str, payload: dict) -> DeliveryResult:
    """Generic notification from a payload dict containing type, title, body."""
    title = payload.get("title", "")
    body = payload.get("body", "")
    return deliver(user_id, title, body, payload, persist=True)
