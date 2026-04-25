"""Unit tests for photo upload and processing handlers."""
from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest
from moto import mock_aws
from PIL import Image

from tests.conftest import build_api_event, build_s3_event


class TestGetPhotoUploadUrl:
    """Tests for handlers/users/get_photo_upload_url.py."""

    @mock_aws
    def test_returns_presigned_url(self, s3_bucket, lambda_context):
        """PUT /users/me/photo should return uploadUrl and photoKey."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.get_photo_upload_url import handler

        event = build_api_event(
            method="PUT",
            path="/users/me/photo",
            user_id="photo-user-123",
        )
        result = handler(event, lambda_context)

        assert result["statusCode"] == 200

        body = json.loads(result["body"])
        assert "uploadUrl" in body
        assert body["photoKey"] == "photos/photo-user-123/original.webp"
        assert "flot-media-test" in body["uploadUrl"]

    @mock_aws
    def test_url_contains_webp_content_type(self, s3_bucket, lambda_context):
        """Presigned URL should specify image/webp content type."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.get_photo_upload_url import handler

        event = build_api_event(
            method="PUT",
            path="/users/me/photo",
        )
        result = handler(event, lambda_context)

        body = json.loads(result["body"])
        # The presigned URL includes content-type in signed headers
        assert "content-type" in body["uploadUrl"].lower()


class TestProcessPhoto:
    """Tests for handlers/users/process_photo.py."""

    def _create_test_image(self, width: int = 800, height: int = 600) -> bytes:
        """Create a test WebP image in memory."""
        img = Image.new("RGB", (width, height), color="red")
        buffer = io.BytesIO()
        img.save(buffer, format="WEBP")
        buffer.seek(0)
        return buffer.getvalue()

    @mock_aws
    def test_creates_three_variants(self, s3_bucket, dynamodb_table, lambda_context):
        """Process photo should create photo, thumb, and blurred variants."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.process_photo import handler

        user_id = "photo-user-456"

        # Seed user in DynamoDB
        dynamodb_table.put_item(Item={
            "pk": f"USER#{user_id}",
            "sk": "PROFILE",
            "userId": user_id,
            "email": "photo@test.com",
            "createdAt": "2026-04-24T10:00:00Z",
        })

        # Upload test image to S3
        test_image = self._create_test_image()
        s3_bucket.put_object(
            Bucket="flot-media-test",
            Key=f"photos/{user_id}/original.webp",
            Body=test_image,
            ContentType="image/webp",
        )

        # Trigger processing
        event = build_s3_event(key=f"photos/{user_id}/original.webp")
        handler(event, lambda_context)

        # Verify 3 variants were created in S3
        for variant in ["photo.webp", "thumb.webp", "blurred.webp"]:
            key = f"photos/{user_id}/{variant}"
            obj = s3_bucket.get_object(Bucket="flot-media-test", Key=key)
            assert obj["ContentType"] == "image/webp"
            assert obj["ContentLength"] > 0

    @mock_aws
    def test_updates_dynamodb_with_urls(self, s3_bucket, dynamodb_table, lambda_context):
        """Process photo should update user profile with CDN URLs."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.process_photo import handler

        user_id = "photo-user-789"

        # Seed user
        dynamodb_table.put_item(Item={
            "pk": f"USER#{user_id}",
            "sk": "PROFILE",
            "userId": user_id,
            "email": "photo@test.com",
            "photoUrl": "",
            "blurredPhotoUrl": "",
            "thumbUrl": "",
            "createdAt": "2026-04-24T10:00:00Z",
        })

        # Upload and process
        test_image = self._create_test_image()
        s3_bucket.put_object(
            Bucket="flot-media-test",
            Key=f"photos/{user_id}/original.webp",
            Body=test_image,
        )

        event = build_s3_event(key=f"photos/{user_id}/original.webp")
        handler(event, lambda_context)

        # Verify DynamoDB was updated
        item = dynamodb_table.get_item(
            Key={"pk": f"USER#{user_id}", "sk": "PROFILE"}
        ).get("Item")

        assert item["photoUrl"] != ""
        assert item["blurredPhotoUrl"] != ""
        assert item["thumbUrl"] != ""
        assert "d1234.cloudfront.net" in item["photoUrl"]

    @mock_aws
    def test_skips_non_original_files(self, s3_bucket, lambda_context):
        """Should skip files that don't match original.webp pattern."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.process_photo import handler

        # Trigger with non-original file (should not process)
        event = build_s3_event(key="photos/user-123/photo.webp")
        handler(event, lambda_context)  # Should not raise

    @mock_aws
    def test_resized_photo_is_400px_wide(self, s3_bucket, dynamodb_table, lambda_context):
        """Resized photo should be 400px wide."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.process_photo import handler

        user_id = "photo-resize-test"

        dynamodb_table.put_item(Item={
            "pk": f"USER#{user_id}",
            "sk": "PROFILE",
            "userId": user_id,
            "email": "r@t.com",
            "createdAt": "2026-04-24T10:00:00Z",
        })

        test_image = self._create_test_image(width=1600, height=1200)
        s3_bucket.put_object(
            Bucket="flot-media-test",
            Key=f"photos/{user_id}/original.webp",
            Body=test_image,
        )

        event = build_s3_event(key=f"photos/{user_id}/original.webp")
        handler(event, lambda_context)

        # Download resized photo and check dimensions
        obj = s3_bucket.get_object(
            Bucket="flot-media-test",
            Key=f"photos/{user_id}/photo.webp",
        )
        img = Image.open(io.BytesIO(obj["Body"].read()))
        assert img.width == 400
