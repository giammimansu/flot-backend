"""Flot — EventBridge handler for match.partially_unlocked.

Notifies the partner that the first user has unlocked, schedules reminders,
and posts a system message to the match chat.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lib.airports import get_airport
from lib.dynamo import get_user, save_notification as dynamo_save_notification, now_iso
from lib.notifications import save_notification, send_push_notification
from lib.websocket import send_to_user
from lib.schedulers import create_unlock_reminder_schedule
from handlers.chat.system_message import post_partner_unlocked
from aws_lambda_powertools import Logger

logger = Logger()


@logger.inject_lambda_context
def handler(event, context):
    detail = event["detail"]
    partner_id = detail["partnerUserId"]
    unlocked_by_name = detail["unlockedByName"]
    match_id = detail["matchId"]
    deadline = detail["unlockDeadline"]
    airport = get_airport(detail["airportCode"])

    partner = get_user(partner_id)
    if not partner:
        logger.warning("partner_not_found", partnerId=partner_id, matchId=match_id)
        return

    savings = airport.base_fare // 2 / 100

    # 1. WebSocket (se online)
    send_to_user(partner_id, {
        "type": "match.partner_unlocked",
        "data": {
            "matchId": match_id,
            "partnerName": unlocked_by_name,
            "deadline": deadline,
        },
    })

    # 2. Push notification — urgente
    if partner.get("pushToken"):
        send_push_notification(
            token=partner["pushToken"],
            title=f"{unlocked_by_name.split()[0] if unlocked_by_name else 'Il tuo partner'} ha sbloccato! 🔓",
            body=f"Sblocca anche tu per condividere il taxi e risparmiare ~€{savings:.0f}",
            payload={"matchId": match_id, "action": "open_match"},
        )

    # 3. Salva notifica in-app (via lib.notifications for consistent schema)
    save_notification(
        partner_id,
        f"{unlocked_by_name.split()[0] if unlocked_by_name else 'Il tuo partner'} ha sbloccato!",
        "Sblocca anche tu per condividere il taxi",
        {"type": "partner_unlocked", "matchId": match_id},
    )

    # 4. System message in chat
    try:
        post_partner_unlocked(match_id, unlocked_by_name)
    except Exception:
        logger.warning("system_message_failed", matchId=match_id)

    # 5. Crea scheduled reminders
    reminder_intervals = detail.get("reminderIntervals", [30, 60, 90])

    first_unlock_at = (
        datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        - timedelta(minutes=airport.unlock_timeout_minutes)
    )

    scheduled = 0
    for offset_min in reminder_intervals:
        fire_at = first_unlock_at + timedelta(minutes=offset_min)
        if fire_at < datetime.now(timezone.utc):
            continue

        create_unlock_reminder_schedule(
            match_id=match_id,
            partner_id=partner_id,
            reminder_number=reminder_intervals.index(offset_min) + 1,
            total_reminders=len(reminder_intervals),
            fire_at=fire_at,
        )
        scheduled += 1

    logger.info(
        "partial_unlock_notified",
        matchId=match_id,
        partnerId=partner_id,
        remindersScheduled=scheduled,
    )
