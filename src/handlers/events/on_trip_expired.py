"""Flot — EventBridge handler for trip.expired

Sends notification to the user assuming their trip schedule couldn't match.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer

logger = Logger()
tracer = Tracer()

@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context) -> None:
    """Handle trip.expired events."""
    logger.info("Trip Expired -> Should notify the user!")
