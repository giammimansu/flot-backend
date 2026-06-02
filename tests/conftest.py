"""Shared pytest fixtures for Flot backend tests.

Uses moto to mock AWS services (DynamoDB, S3, Cognito).
All tests run against local mocks — no real AWS calls.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

# Set TABLE_NAME before any test-file-level imports can load lib.dynamo,
# otherwise the module-level _table = Table(os.environ.get("TABLE_NAME","Flot"))
# would point to the wrong table name when the full suite runs.
os.environ.setdefault("TABLE_NAME", "Flot-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "flot-test")
os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "FlotTest")


# ── Environment Variables (set before any imports that read them) ─────

@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    """Set required environment variables for all tests."""
    monkeypatch.setenv("TABLE_NAME", "Flot-test")
    monkeypatch.setenv("MEDIA_BUCKET", "flot-media-test")
    monkeypatch.setenv("CDN_DOMAIN", "d1234.cloudfront.net")
    monkeypatch.setenv("STAGE", "dev")
    monkeypatch.setenv("FAKE_DOOR_MODE", "true")
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "flot-test")
    monkeypatch.setenv("POWERTOOLS_METRICS_NAMESPACE", "FlotTest")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


# ── DynamoDB Mock ────────────────────────────────────────────────────

@pytest.fixture
def dynamodb_table():
    """Create a mocked DynamoDB table with all 4 GSIs."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="eu-south-1")

        table = dynamodb.create_table(
            TableName="Flot-test",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
                {"AttributeName": "gsi2pk", "AttributeType": "S"},
                {"AttributeName": "gsi2sk", "AttributeType": "S"},
                {"AttributeName": "gsi3pk", "AttributeType": "S"},
                {"AttributeName": "gsi3sk", "AttributeType": "S"},
                {"AttributeName": "gsi4pk", "AttributeType": "S"},
                {"AttributeName": "gsi5pk", "AttributeType": "S"},
                {"AttributeName": "gsi5sk", "AttributeType": "S"},
                {"AttributeName": "gsi6pk", "AttributeType": "S"},
                {"AttributeName": "lockAt", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1-TimeBucket",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI2-UserTrips",
                    "KeySchema": [
                        {"AttributeName": "gsi2pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi2sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI3-UserConn",
                    "KeySchema": [
                        {"AttributeName": "gsi3pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi3sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
                {
                    "IndexName": "GSI4-StripeIntent",
                    "KeySchema": [
                        {"AttributeName": "gsi4pk", "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI5-TripStatus",
                    "KeySchema": [
                        {"AttributeName": "gsi5pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi5sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI6-TentativeMatch",
                    "KeySchema": [
                        {"AttributeName": "gsi6pk", "KeyType": "HASH"},
                        {"AttributeName": "lockAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        table.meta.client.get_waiter("table_exists").wait(TableName="Flot-test")
        yield table


# ── S3 Mock ──────────────────────────────────────────────────────────

@pytest.fixture
def s3_bucket():
    """Create a mocked S3 bucket."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-south-1")
        s3.create_bucket(
            Bucket="flot-media-test",
            CreateBucketConfiguration={"LocationConstraint": "eu-south-1"},
        )
        yield s3


# ── Lambda Context Mock ──────────────────────────────────────────────

@pytest.fixture
def lambda_context():
    """Create a mock Lambda context object."""
    context = MagicMock()
    context.function_name = "test-function"
    context.memory_limit_in_mb = 256
    context.invoked_function_arn = "arn:aws:lambda:eu-south-1:123456789012:function:test"
    context.aws_request_id = "test-request-id-1234"
    return context


# ── API Gateway Event Builders ───────────────────────────────────────

def build_api_event(
    method: str = "GET",
    path: str = "/",
    body: dict | None = None,
    path_parameters: dict | None = None,
    query_string: dict | None = None,
    user_id: str = "test-user-id-123",
    headers: dict | None = None,
) -> dict:
    """Build a mock API Gateway proxy event."""
    event = {
        "httpMethod": method,
        "path": path,
        "pathParameters": path_parameters,
        "queryStringParameters": query_string,
        "headers": {
            "Content-Type": "application/json",
            "origin": "http://localhost:3000",
            **(headers or {}),
        },
        "body": json.dumps(body) if body else None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": user_id,
                    "email": f"{user_id}@test.com",
                },
            },
        },
        "isBase64Encoded": False,
    }
    return event


def build_cognito_event(
    user_id: str = "test-user-id-123",
    email: str = "test@example.com",
    name: str = "Test User",
    picture: str = "https://example.com/photo.jpg",
    trigger_source: str = "PostConfirmation_ConfirmSignUp",
) -> dict:
    """Build a mock Cognito PostConfirmation trigger event."""
    return {
        "version": "1",
        "triggerSource": trigger_source,
        "region": "eu-south-1",
        "userPoolId": "eu-south-1_test",
        "userName": user_id,
        "callerContext": {
            "awsSdkVersion": "aws-sdk-python-3.0",
            "clientId": "test-client-id",
        },
        "request": {
            "userAttributes": {
                "sub": user_id,
                "email": email,
                "name": name,
                "picture": picture,
                "email_verified": "true",
            },
        },
        "response": {},
    }


def build_s3_event(
    bucket: str = "flot-media-test",
    key: str = "photos/test-user-id-123/original.webp",
) -> dict:
    """Build a mock S3 trigger event."""
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key, "size": 102400},
                },
            },
        ],
    }
