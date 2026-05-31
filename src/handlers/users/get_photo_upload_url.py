"""Flot — PUT /users/me/photo handler.

Generates a presigned S3 POST for direct photo upload from the client.
The client uploads via multipart POST directly to S3, which triggers process_photo.py.

Uses presigned POST (not PUT) to work around S3 CORS preflight issues in eu-south-1.
"""
from __future__ import annotations

import os

import boto3
from botocore.config import Config
from aws_lambda_powertools import Logger, Tracer

from lib.http import app_handler, success

logger = Logger()
tracer = Tracer()

# eu-south-1 opt-in region: needs regional endpoint + virtual-hosted style for CORS to work
s3_client = boto3.client(
    "s3",
    region_name="eu-south-1",
    endpoint_url="https://s3.eu-south-1.amazonaws.com",
    config=Config(s3={"addressing_style": "virtual"}),
)

MEDIA_BUCKET = os.environ.get("MEDIA_BUCKET", "")
PRESIGNED_URL_EXPIRY = 300  # 5 minutes


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    """PUT /users/me/photo — Generate presigned S3 POST fields for direct upload."""
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]

    photo_key = f"photos/{user_id}/original.webp"

    presigned = s3_client.generate_presigned_post(
        Bucket=MEDIA_BUCKET,
        Key=photo_key,
        Fields={"Content-Type": "image/webp"},
        Conditions=[["starts-with", "$Content-Type", "image/"]],
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )

    logger.info("Presigned POST generated", extra={"userId": user_id, "key": photo_key})

    return success(
        {
            "uploadUrl": presigned["url"],
            "uploadFields": presigned["fields"],
            "photoKey": photo_key,
        },
        origin,
    )
