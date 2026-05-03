"""Flot — $connect WebSocket handler.

Validates the JWT passed via querystring (?token=<id_token>) using Cognito
JWKS, extracts the user's sub, and stores a CONN# record in DynamoDB.
"""
from __future__ import annotations

import json
import os
import time
from urllib.request import urlopen

from aws_lambda_powertools import Logger, Tracer
from jose import jwt
from jose.utils import base64url_decode

from lib import websocket as ws

logger = Logger()
tracer = Tracer()

USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
APP_CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")
REGION = os.environ.get("AWS_REGION", "eu-west-1")

_jwks_cache: dict[str, dict] | None = None


def _get_jwks() -> dict[str, dict]:
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache
    url = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/jwks.json"
    with urlopen(url, timeout=3) as r:
        keys = json.load(r)["keys"]
    _jwks_cache = {k["kid"]: k for k in keys}
    return _jwks_cache


def _verify_token(token: str) -> dict:
    headers = jwt.get_unverified_headers(token)
    kid = headers["kid"]
    key = _get_jwks().get(kid)
    if not key:
        # Refresh JWKS once on miss
        global _jwks_cache
        _jwks_cache = None
        key = _get_jwks().get(kid)
    if not key:
        raise ValueError("Unknown signing key")

    # Verify signature
    message, encoded_sig = token.rsplit(".", 1)
    decoded_sig = base64url_decode(encoded_sig.encode())
    public_key = jwt.construct_rsa_key(key) if hasattr(jwt, "construct_rsa_key") else None
    # python-jose handles full verification via jwt.decode
    claims = jwt.decode(
        token,
        key,
        algorithms=[key["alg"]],
        audience=APP_CLIENT_ID,
        options={"verify_at_hash": False},
    )
    if claims.get("token_use") not in ("id", "access"):
        raise ValueError("Invalid token_use")
    if claims.get("exp", 0) < int(time.time()):
        raise ValueError("Token expired")
    return claims


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> dict:
    """$connect — authenticate then persist connection."""
    connection_id = event["requestContext"]["connectionId"]
    qs = event.get("queryStringParameters") or {}
    token = qs.get("token")
    if not token:
        logger.warning("WS connect rejected: no token")
        return {"statusCode": 401, "body": "Unauthorized"}

    try:
        claims = _verify_token(token)
    except Exception as e:
        logger.warning("WS connect rejected: token verify failed", extra={"error": str(e)})
        return {"statusCode": 401, "body": "Unauthorized"}

    user_id = claims["sub"]
    ws.store_connection(connection_id, user_id)
    logger.info("WS connected", extra={"userId": user_id, "connectionId": connection_id})
    return {"statusCode": 200, "body": "Connected"}
