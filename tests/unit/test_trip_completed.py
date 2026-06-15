"""Flot — tests for the trip.completed handler (plan #1)."""

import sys
from unittest.mock import patch, MagicMock

# firebase_admin ships only in the deployed Lambda layer; stub it so the
# notifications module (imported transitively) loads locally.
sys.modules.setdefault("firebase_admin", MagicMock())
sys.modules.setdefault("firebase_admin.credentials", MagicMock())
sys.modules.setdefault("firebase_admin.messaging", MagicMock())

from handlers.events import on_trip_completed


def _match(status="unlocked"):
    return {
        "pk": "MATCH#m1",
        "sk": "META",
        "matchId": "m1",
        "status": status,
        "tripId1": "t1",
        "tripId2": "t2",
        "userId1": "u1",
        "userId2": "u2",
    }


def _trip(trip_id, status="matched"):
    return {"pk": f"TRIP#{trip_id}", "sk": "META", "tripId": trip_id, "status": status}


def _event():
    return {"detail": {"matchId": "m1"}}


def _ctx():
    ctx = MagicMock()
    ctx.function_name = "test"
    ctx.memory_limit_in_mb = 256
    ctx.invoked_function_arn = "arn:aws:lambda:eu-south-1:1:function:test"
    ctx.aws_request_id = "req-1"
    return ctx


def test_completes_unlocked_match():
    match = _match("unlocked")
    trips = {"t1": _trip("t1"), "t2": _trip("t2")}
    fake_table = MagicMock()
    fake_table.query.return_value = {"Items": []}

    with patch.object(on_trip_completed, "get_match", return_value=match), \
         patch.object(on_trip_completed, "get_trip", side_effect=lambda tid: trips[tid]), \
         patch.object(on_trip_completed, "table", fake_table), \
         patch.object(on_trip_completed, "put_event") as mock_evt, \
         patch.object(on_trip_completed, "deliver") as mock_notif:

        on_trip_completed.handler(_event(), _ctx())

    # Match update (conditional) + 2 trip completions = 3 update_item calls.
    assert fake_table.update_item.call_count == 3
    # review.requested emitted once per user.
    assert mock_evt.call_count == 2
    assert all(c[0][0] == "review.requested" for c in mock_evt.call_args_list)
    # One deliver() per user — it persists in-app internally, so no separate
    # save_notification call (which would double-persist the feed item).
    assert mock_notif.call_count == 2
    assert not hasattr(on_trip_completed, "save_notification")


def test_idempotent_on_terminal_status():
    match = _match("completed")
    fake_table = MagicMock()

    with patch.object(on_trip_completed, "get_match", return_value=match), \
         patch.object(on_trip_completed, "table", fake_table), \
         patch.object(on_trip_completed, "put_event") as mock_evt:

        on_trip_completed.handler(_event(), _ctx())

    fake_table.update_item.assert_not_called()
    mock_evt.assert_not_called()


def test_ignores_never_unlocked_match():
    match = _match("partially_unlocked")
    fake_table = MagicMock()

    with patch.object(on_trip_completed, "get_match", return_value=match), \
         patch.object(on_trip_completed, "table", fake_table), \
         patch.object(on_trip_completed, "put_event") as mock_evt:

        on_trip_completed.handler(_event(), _ctx())

    fake_table.update_item.assert_not_called()
    mock_evt.assert_not_called()


def test_sets_chat_ttl_on_messages():
    match = _match("unlocked")
    trips = {"t1": _trip("t1"), "t2": _trip("t2")}
    fake_table = MagicMock()
    fake_table.query.return_value = {
        "Items": [
            {"pk": "MATCH#m1", "sk": "CHAT#1"},
            {"pk": "MATCH#m1", "sk": "CHAT#2"},
        ]
    }

    with patch.object(on_trip_completed, "get_match", return_value=match), \
         patch.object(on_trip_completed, "get_trip", side_effect=lambda tid: trips[tid]), \
         patch.object(on_trip_completed, "table", fake_table), \
         patch.object(on_trip_completed, "put_event"), \
         patch.object(on_trip_completed, "deliver"):

        on_trip_completed.handler(_event(), _ctx())

    # 1 match + 2 trips + 2 chat messages = 5 update_item calls.
    assert fake_table.update_item.call_count == 5
    ttl_calls = [
        c for c in fake_table.update_item.call_args_list
        if "#ttl" in c.kwargs.get("ExpressionAttributeNames", {})
    ]
    assert len(ttl_calls) == 2
