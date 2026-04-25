"""Flot — S3 trigger: photo processing Lambda.

Triggered when a photo is uploaded to S3 at photos/{userId}/original.webp.
Creates three variants:
  - photos/{userId}/photo.webp   (400px width)
  - photos/{userId}/thumb.webp   (100px thumbnail)
  - photos/{userId}/blurred.webp (Gaussian blur σ=15)

Updates the user's DynamoDB record with CloudFront URLs.

MemorySize: 1024 (image processing needs more CPU/RAM)
"""
from __future__ import annotations

import io
import os
import urllib.parse

import boto3
from aws_lambda_powertools import Logger, Tracer
from PIL import Image, ImageFilter

from lib.dynamo import update_item

logger = Logger()
tracer = Tracer()

# Module-level clients
s3_client = boto3.client("s3")

MEDIA_BUCKET = os.environ.get("MEDIA_BUCKET", "")
CDN_DOMAIN = os.environ.get("CDN_DOMAIN", "")

# Image processing constants
PHOTO_WIDTH = 400
THUMB_WIDTH = 100
BLUR_SIGMA = 15


def resize_image(img: Image.Image, target_width: int) -> Image.Image:
    """Resize image maintaining aspect ratio."""
    ratio = target_width / img.width
    target_height = int(img.height * ratio)
    return img.resize((target_width, target_height), Image.LANCZOS)


def save_to_s3(img: Image.Image, bucket: str, key: str) -> None:
    """Save PIL Image to S3 as WebP."""
    buffer = io.BytesIO()
    img.save(buffer, format="WEBP", quality=85)
    buffer.seek(0)
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=buffer.getvalue(),
        ContentType="image/webp",
        CacheControl="public, max-age=86400",  # 24h CDN cache
    )


@logger.inject_lambda_context(log_event=True)
@tracer.capture_lambda_handler
def handler(event: dict, context) -> None:
    """S3 trigger handler — processes uploaded photo into 3 variants."""
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        # Safety: only process originals (prevent infinite loop)
        if not key.endswith("/original.webp"):
            logger.info("Skipping non-original file", extra={"key": key})
            continue

        # Extract userId from key: photos/{userId}/original.webp
        parts = key.split("/")
        if len(parts) < 3:
            logger.error("Unexpected key format", extra={"key": key})
            continue

        user_id = parts[1]
        base_path = f"photos/{user_id}"

        logger.info("Processing photo", extra={"userId": user_id, "key": key})

        # Download original
        response = s3_client.get_object(Bucket=bucket, Key=key)
        original_bytes = response["Body"].read()
        img = Image.open(io.BytesIO(original_bytes))

        # Convert to RGB if necessary (handles RGBA, P mode, etc.)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        # 1. Resized photo (400px width)
        photo = resize_image(img, PHOTO_WIDTH)
        photo_key = f"{base_path}/photo.webp"
        save_to_s3(photo, MEDIA_BUCKET, photo_key)

        # 2. Thumbnail (100px width)
        thumb = resize_image(img, THUMB_WIDTH)
        thumb_key = f"{base_path}/thumb.webp"
        save_to_s3(thumb, MEDIA_BUCKET, thumb_key)

        # 3. Blurred version (Gaussian blur σ=15 — server-side only!)
        blurred = photo.copy()
        blurred = blurred.filter(ImageFilter.GaussianBlur(radius=BLUR_SIGMA))
        blurred_key = f"{base_path}/blurred.webp"
        save_to_s3(blurred, MEDIA_BUCKET, blurred_key)

        # Build CloudFront URLs
        cdn_base = f"https://{CDN_DOMAIN}" if CDN_DOMAIN else f"https://{MEDIA_BUCKET}.s3.amazonaws.com"
        photo_url = f"{cdn_base}/{photo_key}"
        thumb_url = f"{cdn_base}/{thumb_key}"
        blurred_url = f"{cdn_base}/{blurred_key}"

        # Update user profile in DynamoDB
        update_item(
            pk=f"USER#{user_id}",
            sk="PROFILE",
            updates={
                "photoUrl": photo_url,
                "thumbUrl": thumb_url,
                "blurredPhotoUrl": blurred_url,
            },
        )

        logger.info("Photo processing complete", extra={
            "userId": user_id,
            "photoUrl": photo_url,
            "thumbUrl": thumb_url,
            "blurredUrl": blurred_url,
        })
