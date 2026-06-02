"""Flot Admin — POST /admin/matches/{matchId}/void

Forces void of the first unlock's PaymentIntent and dissolves the match.
Used by ops when a match is stuck in partially_unlocked and cannot resolve.

Auth: IAM (not Cognito) — only AWS principals with execute-api:Invoke permission.
"""
from __future__ import annotations

import os

import stripe
from botocore.exceptions import ClientError

from aws_lambda_powertools import Logger, Tracer

from lib.dynamo import get_match, table, now_iso
from lib.eventbridge import put_event
from lib.http import AppError, app_handler, success
from lib.state_machine import MatchStateMachine

logger = Logger()
tracer = Tracer()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=False)  # IAM auth is enforced at API Gateway level
def handler(event: dict, context) -> dict:
    origin = event.get("_origin")
    path_params = event.get("pathParameters") or {}
    match_id = path_params.get("matchId")
    if not match_id:
        raise AppError(400, "Missing matchId")

    match = get_match(match_id)
    if not match:
        raise AppError(404, "Match not found")

    if MatchStateMachine.is_terminal(match.get("status", "")):
        return success({"matchId": match_id, "status": match["status"], "skipped": "already_terminal"}, origin)

    # Void first PI if present
    first_pi_id = match.get("firstUnlockPaymentIntentId")
    voided = False
    if first_pi_id and os.environ.get("STRIPE_SECRET_KEY"):
        try:
            stripe.PaymentIntent.cancel(first_pi_id)
            voided = True
            logger.info("admin_pi_voided", piId=first_pi_id, matchId=match_id)
        except stripe.error.InvalidRequestError as e:
            if "already been canceled" in str(e) or "cannot be canceled" in str(e):
                voided = True  # already done
                logger.info("admin_pi_already_voided", piId=first_pi_id)
            else:
                logger.error("admin_pi_void_failed", piId=first_pi_id, error=str(e))

    # Dissolve match via event (reuses existing handler + state machine)
    put_event("match.dissolved", {
        "matchId": match_id,
        "reason": "admin_void",
    })

    logger.info("admin_void_match", matchId=match_id, piVoided=voided)
    return success({"matchId": match_id, "piVoided": voided, "dissolveEmitted": True}, origin)
