from datetime import datetime, timezone

from lib.airports import get_airport
from lib.dynamo import get_match, get_user
from lib.notifications import send_push_notification, send_email
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

    # Escalazione copy in base al reminder number
    if reminder_number == 1:
        title = f"{unlocked_name} ti sta aspettando"
        body = f"Hai ancora {minutes_left} min per sbloccare e risparmiare ~€{airport.base_fare // 2 / 100:.0f}"
    elif reminder_number == total_reminders:
        title = "⏰ Ultima chance!"
        body = f"Il match con {unlocked_name} scade tra {minutes_left} min. Sblocca ora o perdi il match."
    else:
        title = f"Hai ancora {minutes_left} min"
        body = f"{unlocked_name} ha già sbloccato. Sblocca per condividere il taxi."

    # Push
    if partner.get("pushToken"):
        send_push_notification(
            token=partner["pushToken"],
            title=title,
            body=body,
            data={"matchId": match_id, "action": "open_match"},
            priority="high",
        )

    # Email solo per reminder escalati (non spammare)
    if reminder_number >= total_reminders - 1 and partner.get("email"):
        send_email(
            to=partner["email"],
            template="unlock_reminder_urgent",
            data={
                "partnerName": unlocked_name,
                "minutesLeft": minutes_left,
                "matchUrl": f"https://app.flot.app/match/{match_id}",
            },
        )

    logger.info("unlock_reminder_sent",
        matchId=match_id,
        partnerId=partner_id,
        reminderNumber=reminder_number,
        minutesLeft=minutes_left,
    )
