"""Flot — Business-level CloudWatch metrics (funnel observability).

Usage:
    from lib.metrics import business_metrics

    business_metrics.record_trip_created("MXP")
    business_metrics.record_match_rate(airport_code, trips_created, trips_matched)
    business_metrics.record_match_latency_minutes(airport_code, minutes)
    business_metrics.record_deadlock_resolution(resolved=True)
    business_metrics.record_trip_expired_no_match("MXP")
    business_metrics.record_pool_fill(airport_code, bucket, count)

All metrics are in the "Flot/Business" namespace.
Technical Lambda metrics (cold starts etc.) remain in "Flot" via Powertools.
"""
from __future__ import annotations

import os

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools import Metrics
from aws_lambda_powertools.metrics import MetricUnit

logger = Logger(child=True)

_NAMESPACE = os.environ.get("BUSINESS_METRICS_NAMESPACE", "Flot/Business")

# Re-use Powertools Metrics for batch flushing — separate namespace from tech metrics.
_metrics = Metrics(namespace=_NAMESPACE)


class _BusinessMetrics:
    """Thin wrapper around CloudWatch PutMetricData.

    Uses boto3 directly (not Powertools) so metrics can be emitted outside
    of a Lambda handler invocation (e.g., from background helpers).
    """

    def __init__(self) -> None:
        self._client = None

    def _cw(self):
        if self._client is None:
            self._client = boto3.client("cloudwatch")
        return self._client

    def _put(self, metric_name: str, value: float, unit: str, dimensions: list[dict]) -> None:
        try:
            self._cw().put_metric_data(
                Namespace=_NAMESPACE,
                MetricData=[{
                    "MetricName": metric_name,
                    "Value": value,
                    "Unit": unit,
                    "Dimensions": dimensions,
                }],
            )
        except Exception as e:
            logger.warning("metric_emit_failed", metric=metric_name, error=str(e))

    # ── Trip funnel ──────────────────────────────────────────────────

    def record_trip_created(self, airport_code: str) -> None:
        self._put("TripCreated", 1, "Count", [{"Name": "Airport", "Value": airport_code}])

    def record_trip_matched(self, airport_code: str) -> None:
        """Emit when a trip transitions to 'matched' (lock window promotion)."""
        self._put("TripMatched", 1, "Count", [{"Name": "Airport", "Value": airport_code}])

    def record_trip_expired_no_match(self, airport_code: str) -> None:
        """Emit when a trip expires without ever being matched (cold-start indicator)."""
        self._put("TripExpiredNoMatch", 1, "Count", [{"Name": "Airport", "Value": airport_code}])

    # ── Match latency ────────────────────────────────────────────────

    def record_match_latency_minutes(self, airport_code: str, minutes: float) -> None:
        """Time from trip creation to match lock (scheduled→matched)."""
        self._put(
            "MatchLatencyMinutes",
            minutes,
            "None",  # CloudWatch unit "None" = dimensionless
            [{"Name": "Airport", "Value": airport_code}],
        )

    # ── Deadlock resolution ──────────────────────────────────────────

    def record_deadlock_resolution(self, resolved: bool, airport_code: str = "ALL") -> None:
        """Record whether a match deadlock was resolved (both unlocked) or timed out."""
        metric_name = "DeadlockResolved" if resolved else "DeadlockTimedOut"
        self._put(metric_name, 1, "Count", [{"Name": "Airport", "Value": airport_code}])

    # ── Pool fill ────────────────────────────────────────────────────

    def record_pool_fill(self, airport_code: str, time_bucket: str, trip_count: int) -> None:
        """Snapshot of active trips in a time bucket — identifies sparse buckets."""
        self._put(
            "PoolFillCount",
            float(trip_count),
            "Count",
            [
                {"Name": "Airport", "Value": airport_code},
                {"Name": "Bucket", "Value": time_bucket},
            ],
        )


business_metrics = _BusinessMetrics()
