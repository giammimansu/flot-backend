"""Flot — Notification utilities.

Multi-channel notifications: WebSocket -> Firebase (Push) -> SES (Email).
"""
from __future__ import annotations

import json
import os
import time
import uuid

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

# Firebase app singleton — initialized once per Lambda container
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


def save_notification(user_id: str, title: str, body: str, payload: dict) -> dict:
    """Save a notification to DynamoDB for the user."""
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
        "ttl": int(time.time()) + 30 * 24 * 60 * 60,  # 30 days retention
    }
    dynamo.put_item(item)
    return item


def notify_match_found(user_id: str, match_data: dict, match_context_for_user: dict):
    """
    Notifica match trovato via tutti i canali disponibili.
    Ordine di priorità: WebSocket (se online) → Push → Email.
    """
    title = "Match Found!"
    body = "We found a match for your scheduled trip. Check it out!"
    
    # 1. Save to DynamoDB so it's visible in the UI
    save_notification(user_id, title, body, match_context_for_user)
    
    # 2. Prova WebSocket (utente ha la app aperta)
    ws_sent = send_to_user(user_id, {"type": "match.found", "data": match_context_for_user})
    
    user = dynamo.get_item(f"USER#{user_id}", "PROFILE") or {}
    
    # 3. Push notification via SNS (sempre)
    if user.get("pushToken"):
        send_push_notification(user["pushToken"], title, body, match_context_for_user)
    
    # 4. Email via SES come fallback (sempre inviato)
    if user.get("email"):
        send_email_notification(user["email"], title, body)


def send_push_notification(token: str, title: str, body: str, payload: dict):
    if FAKE_DOOR_MODE:
        logger.info("FAKE_DOOR_MODE: Sent PUSH", extra={"token": token, "title": title})
        return

    try:
        _get_firebase_app()
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={
                "action": "open_match",
                "matchId": str(payload.get("matchId", "")),
            },
            token=token,
        )
        messaging.send(message)
        logger.info("Sent PUSH via Firebase", extra={"token": token, "title": title})
    except Exception:
        logger.error("Failed to send PUSH", exc_info=True)


def send_email_notification(email: str, subject: str, message: str):
    if FAKE_DOOR_MODE:
        logger.info("FAKE_DOOR_MODE: Sent EMAIL", extra={"email": email, "subject": subject})
        return
        
    if not SES_FROM_EMAIL:
        logger.warning("No SES_FROM_EMAIL configured for email notifications.")
        return
        
    try:
        ses.send_email(
            Source=SES_FROM_EMAIL,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": message, "Charset": "UTF-8"}},
            },
        )
        logger.info("Sent EMAIL", extra={"email": email, "subject": subject})
    except Exception as e:
        logger.error("Failed to send EMAIL", exc_info=True)
