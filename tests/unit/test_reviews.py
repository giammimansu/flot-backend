"""Flot — Unit tests for rating system (P2 #11)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from tests.conftest import build_api_event

if "firebase_admin" not in sys.modules:
    sys.modules["firebase_admin"] = MagicMock()
    sys.modules["firebase_admin.credentials"] = MagicMock()
    sys.modules["firebase_admin.messaging"] = MagicMock()
    sys.modules["firebase_admin.exceptions"] = MagicMock()


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _make_match(table, match_id, status="completed", *, completed_at=None, u1="u1", u2="u2"):
    item = {
        "pk": f"MATCH#{match_id}",
        "sk": "META",
        "matchId": match_id,
        "status": status,
        "userId1": u1,
        "userId2": u2,
        "tripId1": "t1",
        "tripId2": "t2",
        "airportCode": "MXP",
        "createdAt": _now(),
    }
    if completed_at is not None:
        item["completedAt"] = completed_at
    elif status == "completed":
        item["completedAt"] = _now()
    table.put_item(Item=item)


def _put_user(table, user_id, **attrs):
    table.put_item(Item={
        "pk": f"USER#{user_id}", "sk": "PROFILE", "userId": user_id,
        "email": f"{user_id}@test.com", "createdAt": _now(), **attrs,
    })


# ── create_review ─────────────────────────────────────────────────────

class TestCreateReview:
    def test_creates_review_and_updates_aggregate(self, dynamodb_table, lambda_context):
        _make_match(dynamodb_table, "m1")
        _put_user(dynamodb_table, "u2")

        from handlers.matches.create_review import handler
        event = build_api_event("POST", "/matches/m1/review",
                                body={"rating": 5}, path_parameters={"matchId": "m1"},
                                user_id="u1")
        resp = handler(event, lambda_context)
        assert resp["statusCode"] == 201
        body = json.loads(resp["body"])
        assert body["reviewedUserId"] == "u2"

        review = dynamodb_table.get_item(Key={"pk": "USER#u2", "sk": "REVIEW#m1"}).get("Item")
        assert int(review["rating"]) == 5
        assert review["reviewerId"] == "u1"

        profile = dynamodb_table.get_item(Key={"pk": "USER#u2", "sk": "PROFILE"}).get("Item")
        assert int(profile["ratingCount"]) == 1
        assert int(profile["ratingSum"]) == 5

    def test_idempotent_double_review_rejected(self, dynamodb_table, lambda_context):
        _make_match(dynamodb_table, "m2")
        _put_user(dynamodb_table, "u2")
        from handlers.matches.create_review import handler

        def _post():
            return handler(build_api_event("POST", "/matches/m2/review",
                           body={"rating": 4}, path_parameters={"matchId": "m2"},
                           user_id="u1"), lambda_context)

        assert _post()["statusCode"] == 201
        assert _post()["statusCode"] == 409

    def test_non_member_forbidden(self, dynamodb_table, lambda_context):
        _make_match(dynamodb_table, "m3")
        from handlers.matches.create_review import handler
        resp = handler(build_api_event("POST", "/matches/m3/review",
                       body={"rating": 3}, path_parameters={"matchId": "m3"},
                       user_id="stranger"), lambda_context)
        assert resp["statusCode"] == 403

    def test_not_completed_conflict(self, dynamodb_table, lambda_context):
        _make_match(dynamodb_table, "m4", status="unlocked")
        from handlers.matches.create_review import handler
        resp = handler(build_api_event("POST", "/matches/m4/review",
                       body={"rating": 3}, path_parameters={"matchId": "m4"},
                       user_id="u1"), lambda_context)
        assert resp["statusCode"] == 409

    def test_window_expired(self, dynamodb_table, lambda_context):
        old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat().replace("+00:00", "Z")
        _make_match(dynamodb_table, "m5", completed_at=old)
        from handlers.matches.create_review import handler
        resp = handler(build_api_event("POST", "/matches/m5/review",
                       body={"rating": 3}, path_parameters={"matchId": "m5"},
                       user_id="u1"), lambda_context)
        assert resp["statusCode"] == 410

    def test_invalid_rating_rejected(self, dynamodb_table, lambda_context):
        _make_match(dynamodb_table, "m6")
        from handlers.matches.create_review import handler
        resp = handler(build_api_event("POST", "/matches/m6/review",
                       body={"rating": 9}, path_parameters={"matchId": "m6"},
                       user_id="u1"), lambda_context)
        assert resp["statusCode"] == 400


# ── get_user_rating ───────────────────────────────────────────────────

class TestGetUserRating:
    def test_average_computed(self, dynamodb_table, lambda_context):
        _put_user(dynamodb_table, "u9", ratingSum=Decimal("9"), ratingCount=2)
        from handlers.users.get_user_rating import handler
        resp = handler(build_api_event("GET", "/users/u9/rating",
                       path_parameters={"userId": "u9"}, user_id="caller"), lambda_context)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["average"] == 4.5
        assert body["count"] == 2

    def test_no_ratings_returns_null_average(self, dynamodb_table, lambda_context):
        _put_user(dynamodb_table, "u10")
        from handlers.users.get_user_rating import handler
        resp = handler(build_api_event("GET", "/users/u10/rating",
                       path_parameters={"userId": "u10"}, user_id="caller"), lambda_context)
        body = json.loads(resp["body"])
        assert body["average"] is None
        assert body["count"] == 0

    def test_user_not_found(self, dynamodb_table, lambda_context):
        from handlers.users.get_user_rating import handler
        resp = handler(build_api_event("GET", "/users/ghost/rating",
                       path_parameters={"userId": "ghost"}, user_id="caller"), lambda_context)
        assert resp["statusCode"] == 404
