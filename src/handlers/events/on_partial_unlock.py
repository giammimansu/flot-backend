"""Flot — EventBridge handler for match.partially_unlocked.

Notifies the partner that the first user has unlocked, schedules reminders,
and posts a system message to the match chat.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lib.airports import get_airport
from lib.dynamo import get_user
from lib.i18n import tr, user_lang
from lib.notifications import deliver
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

    # Localize to the RECIPIENT (partner), not the actor who unlocked.
    lang = user_lang(partner)
    default_partner = "Il tuo partner" if lang == "it" else "Your partner"
    name = unlocked_by_name.split()[0] if unlocked_by_name else default_partner

    # WS → Push → Email chain with dedup (push skipped if WS delivered)
    deliver(
        partner_id,
        title=tr("partner_unlocked.title", lang, name=name),
        body=tr("partner_unlocked.body", lang, savings=f"{savings:.0f}"),
        payload={
            "type": "match.partner_unlocked",
            "matchId": match_id,
            "partnerName": unlocked_by_name,
            "deadline": deadline,
            "action": "open_match",
        },
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
