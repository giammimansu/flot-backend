"""Flot — HTTP utilities, CORS, error handling, and handler decorator.

Every API Lambda handler should be wrapped with @app_handler.
This ensures consistent JSON responses, CORS headers, and error handling.
"""
from __future__ import annotations

import json
import os
from functools import wraps
from typing import Any, Callable

from aws_lambda_powertools import Logger, Tracer

logger = Logger(child=True)
tracer = Tracer()

# ── CORS ─────────────────────────────────────────────────────────────

ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "https://flot-app.com",
    "https://d8o5e1c1cqrgw.cloudfront.net"
]


def cors_headers(origin: str | None = None) -> dict[str, str]:
    """Build CORS headers. Reflects matching Origin or defaults to *."""
    allow_origin = origin if origin else "*"

    return {
        "Access-Control-Allow-Origin": allow_origin,
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Amz-Date, X-Api-Key, X-Amz-Security-Token",
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Max-Age": "86400",
    }


# ── Response builders ────────────────────────────────────────────────


def json_response(status_code: int, body: Any, origin: str | None = None) -> dict[str, Any]:
    """Build a standard API Gateway response with CORS."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            **cors_headers(origin),
        },
        "body": json.dumps(body, default=str),
    }


def success(body: Any, origin: str | None = None) -> dict[str, Any]:
    """200 OK response."""
    return json_response(200, body, origin)


def created(body: Any, origin: str | None = None) -> dict[str, Any]:
    """201 Created response."""
    return json_response(201, body, origin)


def no_content(origin: str | None = None) -> dict[str, Any]:
    """204 No Content response."""
    return {
        "statusCode": 204,
        "headers": cors_headers(origin),
        "body": "",
    }


# ── Error handling ───────────────────────────────────────────────────


class AppError(Exception):
    """Application-level error with HTTP status code."""

    def __init__(self, status_code: int, message: str, details: dict[str, Any] | None = None):
        self.status_code = status_code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def error_response(status_code: int, message: str, origin: str | None = None) -> dict[str, Any]:
    """Build an error response."""
    return json_response(status_code, {"error": message}, origin)


# ── Handler decorator ────────────────────────────────────────────────


def get_user_id(event: dict[str, Any]) -> str:
    """Extract userId from Cognito authorizer context."""
    try:
        claims = event["requestContext"]["authorizer"]["claims"]
        return claims["sub"]
    except (KeyError, TypeError) as e:
        raise AppError(401, "Unauthorized: missing user identity") from e


def get_origin(event: dict[str, Any]) -> str | None:
    """Extract Origin header from the request (case-insensitive)."""
    headers = event.get("headers") or {}
    # API Gateway lowercases headers
    return headers.get("origin") or headers.get("Origin")


def parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse JSON body from API Gateway event."""
    body = event.get("body")
    if not body:
        return {}
    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise AppError(400, "Invalid JSON body") from e
    return body


def app_handler(requires_auth: bool = True) -> Callable:
    """Decorator that wraps every API Lambda handler.

    Provides:
    - JSON body parsing
    - User ID extraction (if auth required)
    - Consistent error responses with CORS
    - Structured logging with request context
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        @tracer.capture_method
        def wrapper(event: dict[str, Any], context: Any) -> dict[str, Any]:
            origin = get_origin(event)
            request_id = getattr(context, "aws_request_id", "local")

            logger.append_keys(request_id=request_id)

            try:
                # Inject parsed helpers into the event for convenience
                event["_origin"] = origin
                event["_body"] = parse_body(event)

                if requires_auth:
                    event["_user_id"] = get_user_id(event)

                return func(event, context)

            except AppError as e:
                logger.warning("Application error", extra={
                    "status_code": e.status_code,
                    "error_message": e.message,
                    "error_details": e.details,
                })
                return error_response(e.status_code, e.message, origin)

            except json.JSONDecodeError:
                return error_response(400, "Invalid JSON", origin)

            except Exception:
                logger.exception("Unhandled error")
                return error_response(500, "Internal server error", origin)

        return wrapper

    return decorator
