"""Flot — Unit tests for business metrics (#8)."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch, call

import pytest

if "firebase_admin" not in sys.modules:
    sys.modules["firebase_admin"] = MagicMock()
    sys.modules["firebase_admin.credentials"] = MagicMock()
    sys.modules["firebase_admin.messaging"] = MagicMock()
    sys.modules["firebase_admin.exceptions"] = MagicMock()


class TestBusinessMetrics:
    def test_record_trip_created_emits_metric(self):
        from lib.metrics import _BusinessMetrics
        m = _BusinessMetrics()
        with patch.object(m, "_put") as mock_put:
            m.record_trip_created("MXP")
        mock_put.assert_called_once_with(
            "TripCreated", 1, "Count", [{"Name": "Airport", "Value": "MXP"}]
        )

    def test_record_trip_matched_emits_metric(self):
        from lib.metrics import _BusinessMetrics
        m = _BusinessMetrics()
        with patch.object(m, "_put") as mock_put:
            m.record_trip_matched("MXP")
        mock_put.assert_called_once_with(
            "TripMatched", 1, "Count", [{"Name": "Airport", "Value": "MXP"}]
        )

    def test_record_trip_expired_no_match(self):
        from lib.metrics import _BusinessMetrics
        m = _BusinessMetrics()
        with patch.object(m, "_put") as mock_put:
            m.record_trip_expired_no_match("FCO")
        mock_put.assert_called_once_with(
            "TripExpiredNoMatch", 1, "Count", [{"Name": "Airport", "Value": "FCO"}]
        )

    def test_record_match_latency(self):
        from lib.metrics import _BusinessMetrics
        m = _BusinessMetrics()
        with patch.object(m, "_put") as mock_put:
            m.record_match_latency_minutes("MXP", 47.5)
        mock_put.assert_called_once_with(
            "MatchLatencyMinutes", 47.5, "None", [{"Name": "Airport", "Value": "MXP"}]
        )

    def test_record_deadlock_resolved(self):
        from lib.metrics import _BusinessMetrics
        m = _BusinessMetrics()
        with patch.object(m, "_put") as mock_put:
            m.record_deadlock_resolution(resolved=True, airport_code="MXP")
        mock_put.assert_called_once_with(
            "DeadlockResolved", 1, "Count", [{"Name": "Airport", "Value": "MXP"}]
        )

    def test_record_deadlock_timed_out(self):
        from lib.metrics import _BusinessMetrics
        m = _BusinessMetrics()
        with patch.object(m, "_put") as mock_put:
            m.record_deadlock_resolution(resolved=False, airport_code="MXP")
        mock_put.assert_called_once_with(
            "DeadlockTimedOut", 1, "Count", [{"Name": "Airport", "Value": "MXP"}]
        )

    def test_record_pool_fill(self):
        from lib.metrics import _BusinessMetrics
        m = _BusinessMetrics()
        with patch.object(m, "_put") as mock_put:
            m.record_pool_fill("MXP", "2026-06-02T10:00:00Z", 8)
        mock_put.assert_called_once_with(
            "PoolFillCount",
            8.0,
            "Count",
            [
                {"Name": "Airport", "Value": "MXP"},
                {"Name": "Bucket", "Value": "2026-06-02T10:00:00Z"},
            ],
        )

    def test_put_failure_does_not_raise(self):
        """Metric emit failure must never crash the caller."""
        from lib.metrics import _BusinessMetrics
        m = _BusinessMetrics()
        with patch.object(m, "_cw") as mock_cw:
            mock_cw.return_value.put_metric_data.side_effect = Exception("CW down")
            # Must not raise
            m.record_trip_created("MXP")


class TestOnTripExpiredHandler:
    def test_emits_metric_and_notifies_user(self, dynamodb_table, lambda_context):
        dynamodb_table.put_item(Item={
            "pk": "TRIP#t_expired",
            "sk": "META",
            "tripId": "t_expired",
            "userId": "u_exp",
            "airportCode": "MXP",
            "status": "expired",
            "createdAt": "2026-06-02T09:00:00Z",
        })

        from handlers.events.on_trip_expired import handler

        with patch("handlers.events.on_trip_expired.business_metrics") as mock_bm, \
             patch("handlers.events.on_trip_expired.deliver") as mock_deliver:

            handler(
                {"detail": {"tripId": "t_expired", "airportCode": "MXP"}},
                lambda_context,
            )

        mock_bm.record_trip_expired_no_match.assert_called_once_with("MXP")
        mock_deliver.assert_called_once()
        deliver_call = mock_deliver.call_args
        assert deliver_call[0][0] == "u_exp"

    def test_missing_trip_id_emits_metric_only(self, dynamodb_table, lambda_context):
        from handlers.events.on_trip_expired import handler

        with patch("handlers.events.on_trip_expired.business_metrics") as mock_bm, \
             patch("handlers.events.on_trip_expired.deliver") as mock_deliver:

            handler({"detail": {"airportCode": "MXP"}}, lambda_context)

        mock_bm.record_trip_expired_no_match.assert_called_once_with("MXP")
        mock_deliver.assert_not_called()
