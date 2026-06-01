"""Unit tests for user handlers (get_profile, update_profile, post_confirmation)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from moto import mock_aws

from tests.conftest import build_api_event, build_cognito_event


class TestPostConfirmation:
    """Tests for handlers/auth/post_confirmation.py."""

    @mock_aws
    def test_creates_user_in_dynamodb(self, dynamodb_table, lambda_context):
        """PostConfirmation should create USER# item in DynamoDB."""
        # Need to reimport after moto is active to use mocked table
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.auth.post_confirmation import handler

        event = build_cognito_event(
            user_id="user-abc-123",
            email="mario@example.com",
            name="Mario Rossi",
            picture="https://lh3.google.com/photo.jpg",
        )

        result = handler(event, lambda_context)

        # Handler must return the event (Cognito requirement)
        assert result == event

        # Verify user was created in DynamoDB
        item = dynamodb_table.get_item(
            Key={"pk": "USER#user-abc-123", "sk": "PROFILE"}
        ).get("Item")

        assert item is not None
        assert item["userId"] == "user-abc-123"
        assert item["email"] == "mario@example.com"
        assert item["name"] == "Mario Rossi"
        assert item["isPro"] is False
        assert item["verified"] is False
        assert item["lang"] == "it"
        assert "createdAt" in item

    @mock_aws
    def test_skips_non_confirm_triggers(self, dynamodb_table, lambda_context):
        """Should skip triggers other than PostConfirmation_ConfirmSignUp."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.auth.post_confirmation import handler

        event = build_cognito_event(trigger_source="PreSignUp_ExternalProvider")

        result = handler(event, lambda_context)

        # Should return event without creating a user
        assert result == event

    @mock_aws
    def test_creates_user_with_onboarding_false(self, dynamodb_table, lambda_context):
        """PostConfirmation deve creare utente con onboarding=False."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.auth.post_confirmation import handler

        event = build_cognito_event(
            user_id="user-xyz-789",
            email="test-onboarding@example.com",
            name="Test Onboarding User",
        )

        handler(event, lambda_context)

        item = dynamodb_table.get_item(
            Key={"pk": "USER#user-xyz-789", "sk": "PROFILE"}
        ).get("Item")

        assert item is not None
        assert item["onboarding"] is False


class TestGetProfile:
    """Tests for handlers/users/get_profile.py."""

    @mock_aws
    def test_returns_user_profile(self, dynamodb_table, lambda_context):
        """GET /users/me should return 200 with user data."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.get_profile import handler

        # Seed a user
        dynamodb_table.put_item(Item={
            "pk": "USER#test-user-id-123",
            "sk": "PROFILE",
            "userId": "test-user-id-123",
            "email": "test@example.com",
            "name": "Test User",
            "isPro": False,
            "verified": False,
            "lang": "it",
            "createdAt": "2026-04-24T10:00:00Z",
        })

        event = build_api_event(method="GET", path="/users/me")
        result = handler(event, lambda_context)

        assert result["statusCode"] == 200

        body = json.loads(result["body"])
        assert body["userId"] == "test-user-id-123"
        assert body["email"] == "test@example.com"
        assert body["name"] == "Test User"
        assert body["isPro"] is False

    @mock_aws
    def test_returns_404_if_not_found(self, dynamodb_table, lambda_context):
        """GET /users/me should return 404 if user doesn't exist."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.get_profile import handler

        event = build_api_event(method="GET", path="/users/me", user_id="nonexistent")
        result = handler(event, lambda_context)

        assert result["statusCode"] == 404

        body = json.loads(result["body"])
        assert "error" in body

    @mock_aws
    def test_cors_headers_present(self, dynamodb_table, lambda_context):
        """Response should include CORS headers."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.get_profile import handler

        dynamodb_table.put_item(Item={
            "pk": "USER#test-user-id-123",
            "sk": "PROFILE",
            "userId": "test-user-id-123",
            "email": "t@t.com",
            "createdAt": "2026-04-24T10:00:00Z",
        })

        event = build_api_event()
        result = handler(event, lambda_context)

        assert "Access-Control-Allow-Origin" in result["headers"]

    @mock_aws
    def test_onboarding_in_get_profile_response(self, dynamodb_table, lambda_context):
        """GET /users/me deve includere il campo onboarding."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.get_profile import handler

        # Seed a user with onboarding=False
        dynamodb_table.put_item(Item={
            "pk": "USER#test-user-id-123",
            "sk": "PROFILE",
            "userId": "test-user-id-123",
            "email": "test@example.com",
            "name": "Test User",
            "isPro": False,
            "verified": False,
            "lang": "it",
            "onboarding": False,
            "createdAt": "2026-04-24T10:00:00Z",
        })

        event = build_api_event(method="GET", path="/users/me")
        result = handler(event, lambda_context)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["onboarding"] is False


class TestUpdateProfile:
    """Tests for handlers/users/update_profile.py."""

    @mock_aws
    def test_updates_name(self, dynamodb_table, lambda_context):
        """PUT /users/me should update allowed fields."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.update_profile import handler

        # Seed user
        dynamodb_table.put_item(Item={
            "pk": "USER#test-user-id-123",
            "sk": "PROFILE",
            "userId": "test-user-id-123",
            "email": "test@example.com",
            "name": "Old Name",
            "isPro": False,
            "createdAt": "2026-04-24T10:00:00Z",
        })

        event = build_api_event(
            method="PUT",
            path="/users/me",
            body={"name": "New Name"},
        )
        result = handler(event, lambda_context)

        assert result["statusCode"] == 200

        body = json.loads(result["body"])
        assert body["name"] == "New Name"

    @mock_aws
    def test_rejects_unknown_fields(self, dynamodb_table, lambda_context):
        """PUT /users/me should reject unexpected fields (extra='forbid')."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.update_profile import handler

        dynamodb_table.put_item(Item={
            "pk": "USER#test-user-id-123",
            "sk": "PROFILE",
            "userId": "test-user-id-123",
            "email": "test@example.com",
            "createdAt": "2026-04-24T10:00:00Z",
        })

        event = build_api_event(
            method="PUT",
            path="/users/me",
            body={"isPro": True, "hackerField": "evil"},  # Not allowed
        )
        result = handler(event, lambda_context)

        assert result["statusCode"] == 400

    @mock_aws
    def test_rejects_empty_body(self, dynamodb_table, lambda_context):
        """PUT /users/me with no fields should return 400."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.update_profile import handler

        dynamodb_table.put_item(Item={
            "pk": "USER#test-user-id-123",
            "sk": "PROFILE",
            "userId": "test-user-id-123",
            "email": "test@example.com",
            "createdAt": "2026-04-24T10:00:00Z",
        })

        event = build_api_event(method="PUT", path="/users/me", body={})
        result = handler(event, lambda_context)

        assert result["statusCode"] == 400

    @mock_aws
    def test_onboarding_set_to_true(self, dynamodb_table, lambda_context):
        """PUT /users/me con { onboarding: true } deve aggiornare il campo."""
        import importlib
        import lib.dynamo as dynamo_module
        importlib.reload(dynamo_module)

        from handlers.users.update_profile import handler

        # Seed user con onboarding=False
        dynamodb_table.put_item(Item={
            "pk": "USER#test-user-id-123",
            "sk": "PROFILE",
            "userId": "test-user-id-123",
            "email": "test@example.com",
            "name": "Test User",
            "isPro": False,
            "onboarding": False,
            "createdAt": "2026-04-24T10:00:00Z",
        })

        # Chiama handler con body={"onboarding": True}
        event = build_api_event(
            method="PUT",
            path="/users/me",
            body={"onboarding": True},
        )
        result = handler(event, lambda_context)

        # Assert: risposta 200, body["onboarding"] == True
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["onboarding"] is True

        # Assert: DynamoDB item["onboarding"] == True
        item = dynamodb_table.get_item(
            Key={"pk": "USER#test-user-id-123", "sk": "PROFILE"}
        ).get("Item")
        assert item["onboarding"] is True
