"""Flot — EventBridge handler for match.invalidated (v4).

Notifies both users that their confirmed match was cancelled due to a flight delay.
Only fires for definitive matches (status=matched). TentativeMatch dissolution is silent.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

from lib import dynamo
from lib.i18n import tr, user_lang
from lib.notifications import send_push_notification, save_notification

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> None:
    detail = event.get("detail", {})
    match_id = detail.get("matchId")
    user_id_1 = detail.get("userId1")
    user_id_2 = detail.get("userId2")
    reason = detail.get("reason", "unknown")
    delta_min = detail.get("deltaMinutes", 0)

    if not all([match_id, user_id_1, user_id_2]):
        logger.warning("match_invalidated_missing_fields", detail=detail)
        return

    delta_str = f"{round(delta_min, 0):.0f}"

    for user_id in (user_id_1, user_id_2):
        # Localize per recipient.
        user = dynamo.get_item(f"USER#{user_id}", "PROFILE") or {}
        lang = user_lang(user)
        title = tr("match_invalidated.title", lang)
        body = tr("match_invalidated.body", lang, delta_min=delta_str)

        save_notification(user_id, title, body, {
            "type": "MATCH_INVALIDATED",
            "matchId": match_id,
            "reason": reason,
            "deltaMinutes": delta_min,
        })
        if user.get("pushToken"):
            send_push_notification(user["pushToken"], title, body, {
                "type": "MATCH_INVALIDATED",
                "matchId": match_id,
            })

    logger.info("match_invalidated_notified", matchId=match_id, reason=reason)
