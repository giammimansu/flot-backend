import os
from datetime import datetime, timedelta, timezone

from lib.airports import get_airport
from lib.dynamo import get_user, save_notification
from lib.notifications import send_push_notification, send_email, send_ws_notification
from lib.schedulers import create_unlock_reminder_schedule
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
    savings = airport.base_fare // 2 / 100

    # 1. WebSocket (se online)
    send_ws_notification(partner_id, "partner_unlocked", {
        "matchId": match_id,
        "partnerName": unlocked_by_name,
        "deadline": deadline,
    })

    # 2. Push notification — urgente
    if partner.get("pushToken"):
        send_push_notification(
            token=partner["pushToken"],
            title=f"{unlocked_by_name} ha sbloccato! 🔓",
            body=f"Sblocca anche tu per condividere il taxi e risparmiare ~€{savings:.0f}",
            data={"matchId": match_id, "action": "open_match"},
            priority="high",
        )

    # 3. Email con CTA diretto
    if partner.get("email"):
        send_email(
            to=partner["email"],
            template="partner_unlocked",
            data={
                "partnerName": unlocked_by_name,
                "savings": savings,
                "matchUrl": f"https://app.flot.app/match/{match_id}",
                "deadline": deadline,
            },
        )

    # 4. Salva notifica in-app
    save_notification(partner_id, {
        "type": "partner_unlocked",
        "title": f"{unlocked_by_name} ha sbloccato!",
        "body": "Sblocca anche tu per condividere il taxi",
        "matchId": match_id,
    })

    # 5. Crea scheduled reminders
    reminder_intervals = detail.get("reminderIntervals", [30, 60, 90])
    
    first_unlock_at = (
        datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        - timedelta(minutes=airport.unlock_timeout_minutes)
    )

    for offset_min in reminder_intervals:
        fire_at = first_unlock_at + timedelta(minutes=offset_min)
        if fire_at < datetime.now(timezone.utc):
            continue  # non creare reminder nel passato

        create_unlock_reminder_schedule(
            match_id=match_id,
            partner_id=partner_id,
            reminder_number=reminder_intervals.index(offset_min) + 1,
            total_reminders=len(reminder_intervals),
            fire_at=fire_at,
        )

    logger.info("partial_unlock_notified",
        matchId=match_id,
        partnerId=partner_id,
        remindersScheduled=len(reminder_intervals),
    )
