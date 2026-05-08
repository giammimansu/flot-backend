"""Flot — PUT /users/me/push-token handler.

Registers a push token (FCM/APNS) for the user.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer
from pydantic import ValidationError
from lib import dynamo
from lib.http import AppError, app_handler, json_response
from lib.validation import PushTokenUpdate

logger = Logger()
tracer = Tracer()

@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    user_id = event["_user_id"]
    body = event["_body"]
    
    try:
        req = PushTokenUpdate.model_validate(body)
    except ValidationError as e:
        raise AppError(400, "Invalid payload", details={"errors": e.errors()}) from e
        
    dynamo.update_item(
        f"USER#{user_id}",
        "PROFILE",
        updates={"pushToken": req.token, "platform": req.platform},
    )
    
    return json_response(200, {"message": "Push token registered"})