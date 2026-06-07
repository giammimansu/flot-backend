"""Flot — System message producer for in-app chat.

System messages are automatically inserted into the match chat on key events.
They use type="system" and have no senderId.

Public API:
    post_system_message(match_id, text)
    post_match_confirmed(match_id)
    post_partner_unlocked(match_id, partner_name)
    post_match_unlocked(match_id)
    post_chat_expiring(match_id, hours_left)
"""
from __future__ import annotations

from aws_lambda_powertools import Logger

from lib.dynamo import save_chat_message
from lib import websocket as ws

logger = Logger(child=True)


def post_system_message(match_id: str, text: str) -> dict:
    """Persist a system message and broadcast it to both users via WS."""
    msg = save_chat_message(match_id=match_id, sender_id=None, text=text, msg_type="system")

    payload = {
        "type": "chat.system",
        "data": {
            "matchId": match_id,
            "messageId": msg["messageId"],
            "text": text,
            "createdAt": msg["createdAt"],
        },
    }

    from lib.dynamo import get_match
    match = get_match(match_id)
    if match:
        for uid in (match.get("userId1"), match.get("userId2")):
            if uid:
                ws.send_to_user(uid, payload)

    logger.info("system_message_posted", matchId=match_id, text=text[:60])
    return msg


def post_match_confirmed(match_id: str) -> dict:
    """'Match confermato! Sblocca per iniziare a chattare.'"""
    return post_system_message(
        match_id,
        "✅ Match confermato! Sblocca la connessione per iniziare a chattare con il tuo partner.",
    )


def post_partner_unlocked(match_id: str, partner_name: str) -> dict:
    """'[Nome] ha sbloccato. Sblocca anche tu per aprire la chat.'"""
    first_name = partner_name.split()[0] if partner_name else "Il tuo partner"
    return post_system_message(
        match_id,
        f"🔓 {first_name} ha sbloccato la connessione. Sblocca anche tu per aprire la chat!",
    )


def post_match_unlocked(match_id: str) -> dict:
    """'Connessione sbloccata da entrambi — la chat è aperta.'"""
    return post_system_message(
        match_id,
        "🎉 Connessione sbloccata da entrambi! La chat è aperta — scambiatevi i dettagli del viaggio.",
    )


def post_chat_expiring(match_id: str, hours_left: int = 2) -> dict:
    """'La chat scadrà tra N ore.'"""
    return post_system_message(
        match_id,
        f"⏳ La chat scadrà tra {hours_left} {'ora' if hours_left == 1 else 'ore'}. Scambiatevi i contatti se volete restare in touch.",
    )
