from datetime import datetime, timezone

from lib.airports import get_airport
from lib.dynamo import get_match, get_user
from lib.i18n import tr, user_lang
from lib.notifications import send_push_notification, send_email_notification
from aws_lambda_powertools import Logger

logger = Logger()

@logger.inject_lambda_context
def handler(event, context):
    detail = event["detail"]
    match_id = detail["matchId"]
    partner_id = detail["partnerId"]
    reminder_number = detail["reminderNumber"]
    total_reminders = detail["totalReminders"]

    match = get_match(match_id)

    # Guard: match già sbloccato o scaduto
    if match["status"] != "partially_unlocked":
        return

    partner = get_user(partner_id)
    unlocked_user_id = match["unlockedBy"][0]
    unlocked_user = get_user(unlocked_user_id)
    unlocked_name = unlocked_user.get("name", "Your partner").split()[0]
    airport = get_airport(match["airportCode"])
    deadline = datetime.fromisoformat(match["unlockDeadline"].replace("Z", "+00:00"))
    minutes_left = max(0, int((deadline - datetime.now(timezone.utc)).total_seconds() / 60))
    savings = f"{airport.base_fare // 2 / 100:.0f}"

    # Localize to the RECIPIENT (partner) language, not the actor's.
    lang = user_lang(partner)

    # Escalazione copy in base al reminder number
    if reminder_number == 1:
        variant = "first"
    elif reminder_number == total_reminders:
        variant = "last"
    else:
        variant = "mid"

    title = tr(f"unlock_reminder.{variant}.title", lang,
               partner_name=unlocked_name, minutes_left=minutes_left, savings=savings)
    body = tr(f"unlock_reminder.{variant}.body", lang,
              partner_name=unlocked_name, minutes_left=minutes_left, savings=savings)

    # Push
    if partner.get("pushToken"):
        send_push_notification(
            partner["pushToken"],
            title,
            body,
            {"matchId": match_id, "action": "open_match", "type": "unlock_reminder"},
        )

    # Email solo per reminder escalati (non spammare)
    if reminder_number >= total_reminders - 1 and partner.get("email"):
        match_url = f"https://app.flot.app/match/{match_id}"
        send_email_notification(
            partner["email"],
            tr("unlock_reminder.email.subject", lang),
            tr("unlock_reminder.email.body", lang,
               partner_name=unlocked_name, minutes_left=minutes_left, match_url=match_url),
        )

    logger.info("unlock_reminder_sent",
        matchId=match_id,
        partnerId=partner_id,
        reminderNumber=reminder_number,
        minutesLeft=minutes_left,
    )
