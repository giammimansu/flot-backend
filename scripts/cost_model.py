#!/usr/bin/env python3
"""Flot — parametric AWS cost model (P2 #13).

Validates the "< $50/month at MVP volume" target with an explicit, auditable
model instead of a back-of-envelope guess. Every assumption is a named constant
you can override; the breakdown is printed per service.

This is a *model*, not a measurement. Real billing depends on payload sizes,
GSI projection reads, and request mix. Numbers are deliberately conservative
(round up). Run:  python scripts/cost_model.py

Pricing: eu-south-1 (Milan), on-demand, approx. early-2026 public rates (USD).
Update PRICES if AWS changes list prices.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────
# Load scenario (from the action plan #13)
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scenario:
    trips_per_day: int = 100
    days_per_month: int = 30
    # Matchmaker scheduled cadence
    matchmaker_every_min: int = 5
    flight_tracker_every_min: int = 15
    dissolve_checker_every_min: int = 60
    active_hours_per_day: int = 16
    # Per-trip request fan-out (REST API calls over a trip's lifetime)
    api_calls_per_trip: int = 20
    # WebSocket messages per matched pair (chat + typing + system)
    ws_messages_per_match: int = 40
    match_rate: float = 0.6  # fraction of trips that reach a match
    # Notifications per trip (push + email across the funnel)
    notifications_per_trip: int = 4
    email_fraction: float = 0.5  # fraction of notifications that fall through to SES

    @property
    def trips_per_month(self) -> int:
        return self.trips_per_day * self.days_per_month

    @property
    def matches_per_month(self) -> float:
        return self.trips_per_month * self.match_rate / 2  # 2 trips per match


# ─────────────────────────────────────────────────────────────────────
# Pricing constants (USD, on-demand, eu-south-1 approx.)
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Prices:
    # Lambda (arm64)
    lambda_request: float = 0.20e-6           # $0.20 per 1M requests
    lambda_gb_second: float = 0.0000133334    # arm64 $/GB-s
    # DynamoDB on-demand
    ddb_write_request: float = 1.25e-6        # $1.25 per 1M WRU
    ddb_read_request: float = 0.25e-6         # $0.25 per 1M RRU (eventually consistent counts 0.5)
    ddb_storage_gb_month: float = 0.25
    # EventBridge custom bus
    eb_event: float = 1.0e-6                  # $1.00 per 1M events
    # API Gateway REST
    apigw_rest_request: float = 3.5e-6        # $3.50 per 1M
    # API Gateway WebSocket
    apigw_ws_message: float = 1.0e-6          # $1.00 per 1M messages
    apigw_ws_minute: float = 0.25e-6          # connection-minutes
    # SNS mobile push
    sns_push: float = 0.50e-6                 # $0.50 per 1M (first 1M free/month)
    # SES
    ses_email: float = 0.10e-3                # $0.10 per 1,000
    # CloudWatch
    cw_metric_month: float = 0.30             # per custom metric/month
    cw_put_metric: float = 0.01e-3            # $0.01 per 1,000 PutMetricData (after free tier)
    # S3 + CloudFront — dominated by free tier at MVP; flat token allowance.
    s3_cloudfront_flat: float = 1.00


# ─────────────────────────────────────────────────────────────────────
# Model
# ─────────────────────────────────────────────────────────────────────


@dataclass
class LineItem:
    service: str
    detail: str
    monthly_usd: float


def _scheduled_invocations_per_month(every_min: int, hours_per_day: int, days: int) -> int:
    per_day = (hours_per_day * 60) // every_min
    return per_day * days


def model(s: Scenario, p: Prices) -> list[LineItem]:
    items: list[LineItem] = []

    # ── Lambda ────────────────────────────────────────────────────────
    # Scheduled jobs (always-on cadence) + per-trip API handlers + WS handlers.
    matchmaker_inv = _scheduled_invocations_per_month(s.matchmaker_every_min, 24, s.days_per_month)
    tracker_inv = _scheduled_invocations_per_month(s.flight_tracker_every_min, 24, s.days_per_month)
    dissolve_inv = _scheduled_invocations_per_month(s.dissolve_checker_every_min, 24, s.days_per_month)
    api_inv = s.trips_per_month * s.api_calls_per_trip
    ws_inv = int(s.matches_per_month * s.ws_messages_per_match)
    event_inv = s.trips_per_month * 6  # ~6 event-driven lambdas per trip lifecycle

    total_lambda_inv = matchmaker_inv + tracker_inv + dissolve_inv + api_inv + ws_inv + event_inv
    # Avg billed duration: scheduled jobs ~800ms@256MB, API ~150ms@256MB. Use 250ms avg.
    avg_ms = 250
    mem_gb = 256 / 1024
    gb_seconds = total_lambda_inv * (avg_ms / 1000) * mem_gb
    lambda_cost = total_lambda_inv * p.lambda_request + gb_seconds * p.lambda_gb_second
    items.append(LineItem("Lambda", f"{total_lambda_inv:,} inv, {gb_seconds:,.0f} GB-s", lambda_cost))

    # ── DynamoDB ──────────────────────────────────────────────────────
    # Writes: trip create + status transitions + match + payments + chat persist.
    writes_per_trip = 12
    writes_per_match = 8
    ddb_writes = s.trips_per_month * writes_per_trip + int(s.matches_per_month * writes_per_match)
    # Reads: matchmaker scans the pool every run (GSI5), API gets, public profiles.
    pool_scan_reads = matchmaker_inv * 40        # ~40 items read per scan (pool size)
    api_reads = s.trips_per_month * 30
    ddb_reads = pool_scan_reads + api_reads
    ddb_cost = ddb_writes * p.ddb_write_request + ddb_reads * p.ddb_read_request
    ddb_cost += 1.0 * p.ddb_storage_gb_month     # ~1GB at MVP (TTL keeps it small)
    items.append(LineItem("DynamoDB", f"{ddb_writes:,} WRU, {ddb_reads:,} RRU", ddb_cost))

    # ── EventBridge ───────────────────────────────────────────────────
    eb_cost = event_inv * p.eb_event
    items.append(LineItem("EventBridge", f"{event_inv:,} events", eb_cost))

    # ── API Gateway REST ──────────────────────────────────────────────
    rest_cost = api_inv * p.apigw_rest_request
    items.append(LineItem("API GW REST", f"{api_inv:,} requests", rest_cost))

    # ── API Gateway WebSocket ─────────────────────────────────────────
    # Connection-minutes: assume each matched user connected ~30 min/day.
    conn_minutes = int(s.trips_per_month * 30)
    ws_cost = ws_inv * p.apigw_ws_message + conn_minutes * p.apigw_ws_minute
    items.append(LineItem("API GW WS", f"{ws_inv:,} msgs, {conn_minutes:,} conn-min", ws_cost))

    # ── SNS push ──────────────────────────────────────────────────────
    pushes = int(s.trips_per_month * s.notifications_per_trip * (1 - s.email_fraction))
    sns_cost = max(0.0, (pushes - 1_000_000)) * p.sns_push  # first 1M free
    items.append(LineItem("SNS push", f"{pushes:,} pushes (1M free)", sns_cost))

    # ── SES email ─────────────────────────────────────────────────────
    emails = int(s.trips_per_month * s.notifications_per_trip * s.email_fraction)
    ses_cost = emails * p.ses_email
    items.append(LineItem("SES email", f"{emails:,} emails", ses_cost))

    # ── CloudWatch ────────────────────────────────────────────────────
    custom_metrics = 12  # Flot/Business namespace metrics
    metric_puts = matchmaker_inv * 4 + s.trips_per_month * 3
    cw_cost = custom_metrics * p.cw_metric_month + metric_puts * p.cw_put_metric
    items.append(LineItem("CloudWatch", f"{custom_metrics} metrics, {metric_puts:,} puts", cw_cost))

    # ── S3 + CloudFront ───────────────────────────────────────────────
    items.append(LineItem("S3+CloudFront", "profile photos (free-tier bound)", p.s3_cloudfront_flat))

    return items


def main() -> None:
    s = Scenario()
    p = Prices()
    items = model(s, p)
    total = sum(i.monthly_usd for i in items)

    print("=" * 68)
    print(f"Flot — Monthly Cost Model  ({s.trips_per_day} trips/day, "
          f"{s.trips_per_month:,} trips/month)")
    print("=" * 68)
    print(f"{'Service':<16}{'Detail':<38}{'USD/mo':>10}")
    print("-" * 68)
    for i in sorted(items, key=lambda x: x.monthly_usd, reverse=True):
        print(f"{i.service:<16}{i.detail[:37]:<38}{i.monthly_usd:>10.2f}")
    print("-" * 68)
    print(f"{'TOTAL':<54}{total:>10.2f}")
    print("=" * 68)
    target = 50.0
    verdict = "PASS" if total < target else "OVER BUDGET"
    print(f"Target < ${target:.0f}/month  ->  {verdict}  "
          f"(headroom ${target - total:.2f})")
    print()
    print("Top cost drivers (optimize first):")
    for i in sorted(items, key=lambda x: x.monthly_usd, reverse=True)[:3]:
        print(f"  - {i.service}: ${i.monthly_usd:.2f}/mo - {i.detail}")


if __name__ == "__main__":
    main()
