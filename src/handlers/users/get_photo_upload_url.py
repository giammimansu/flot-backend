"""Flot — PUT /users/me/photo handler.

Generates a presigned S3 PUT URL for direct photo upload from the client.
The client uploads the image directly to S3, which triggers process_photo.py.
"""
from __future__ import annotations

import os

import boto3
from aws_lambda_powertools import Logger, Tracer

from lib.http import app_handler, success

logger = Logger()
tracer = Tracer()

# Module-level client (reused across invocations)
s3_client = boto3.client("s3")

MEDIA_BUCKET = os.environ.get("MEDIA_BUCKET", "")
PRESIGNED_URL_EXPIRY = 300  # 5 minutes


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@app_handler(requires_auth=True)
def handler(event: dict, context) -> dict:
    """PUT /users/me/photo — Generate presigned S3 upload URL."""
    user_id: str = event["_user_id"]
    origin: str | None = event["_origin"]

    photo_key = f"photos/{user_id}/original.webp"

    upload_url = s3_client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": MEDIA_BUCKET,
            "Key": photo_key,
            "ContentType": "image/webp",
            "Metadata": {"userId": user_id},
        },
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )

    logger.info("Presigned URL generated", extra={"userId": user_id, "key": photo_key})

    return success(
        {
            "uploadUrl": upload_url,
            "photoKey": photo_key,
        },
        origin,
    )
