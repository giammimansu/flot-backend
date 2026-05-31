"""
Script one-shot: inserisce trip sintetico per fake-test-passenger-001
e forza optimize_pool su DynamoDB reale (Flot-dev, eu-south-1).

Uso:
    cd flot-backend
    TABLE_NAME=Flot-dev AWS_DEFAULT_REGION=eu-south-1 python scripts/force_match_fake_passenger.py

Cleanup automatico al termine (rimuove il trip fake).
"""
import os
import sys
import uuid
import boto3
from decimal import Decimal
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────
TABLE_NAME = os.environ.get("TABLE_NAME", "Flot-dev")
REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-south-1")

REAL_USER_ID = "46fea2c0-9051-7056-8bd7-b7bad07bf362"
REAL_TRIP_ID = "38957676-8512-4501-b558-1d135929f562"
REAL_FLIGHT_TIME = "2026-05-15T07:45:00.000Z"
REAL_DEST_LAT = 45.4765755
REAL_DEST_LNG = 9.2169887

FAKE_USER_ID = "fake-test-passenger-001"
FAKE_TRIP_ID = f"fake-trip-{uuid.uuid4().hex[:8]}"

# dest ~250m dal real user → distance_score = 1.0
FAKE_DEST_LAT = 45.4783
FAKE_DEST_LNG = 9.2190

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ["TABLE_NAME"] = TABLE_NAME
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["POWERTOOLS_SERVICE_NAME"] = "force-match-script"
os.environ["LOG_LEVEL"] = "INFO"

# ── Fake trip item ────────────────────────────────────────────────────
def _make_fake_trip() -> dict:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    flight_time = REAL_FLIGHT_TIME
    time_bucket = flight_time[:16] + ":00Z"
    return {
        "pk": f"TRIP#{FAKE_TRIP_ID}",
        "sk": "META",
        "tripId": FAKE_TRIP_ID,
        "userId": FAKE_USER_ID,
        "airportCode": "MXP",
        "status": "scheduled",
        "flightTime": flight_time,
        "direction": "TO_MILAN",
        "destLat": Decimal(str(FAKE_DEST_LAT)),
        "destLng": Decimal(str(FAKE_DEST_LNG)),
        "timeBucket": time_bucket,
        "gsi5pk": "MXP#scheduled",
        "gsi5sk": flight_time,
        "createdAt": now,
    }


def _to_ddb(item: dict) -> dict:
    """Minimal Python→DDB serializer per questo script."""
    from boto3.dynamodb.types import TypeSerializer
    s = TypeSerializer()
    return {k: s.serialize(v) for k, v in item.items() if v is not None}


def insert_fake_trip(ddb, trip: dict):
    ddb.put_item(TableName=TABLE_NAME, Item=_to_ddb(trip))
    print(f"[INSERT] Trip {trip['tripId']} inserito per {FAKE_USER_ID}")


def delete_fake_trip(ddb, trip_id: str):
    ddb.delete_item(
        TableName=TABLE_NAME,
        Key={"pk": {"S": f"TRIP#{trip_id}"}, "sk": {"S": "META"}},
    )
    print(f"[DELETE] Trip {trip_id} rimosso")


def run():
    ddb = boto3.client("dynamodb", region_name=REGION)

    trip = _make_fake_trip()
    insert_fake_trip(ddb, trip)

    import time
    print("[WAIT] 3s per GSI propagation...")
    time.sleep(3)

    try:
        from lib.airports import get_airport
        from handlers.matching.matchmaker import _query_active_pool, build_compatibility_matrix, optimize_pool
        from lib.matching import compute_dynamic_threshold

        airport = get_airport("MXP")
        now = datetime.now(timezone.utc)

        pool = _query_active_pool("MXP", now)
        print(f"\n[POOL] {len(pool)} trip trovati:")
        for t in pool:
            print(f"  {t['tripId']} | user={t['userId']} | status={t['status']} | flight={t.get('flightTime')} | lat={t.get('destLat')} lng={t.get('destLng')}")

        pairs = build_compatibility_matrix(pool, airport, now)
        print(f"\n[PAIRS] {len(pairs)} coppie compatibili:")
        for p in pairs:
            tid_a, tid_b, score, dist_km, detour = p
            thr = compute_dynamic_threshold(airport.match_threshold,
                next(t["flightTime"] for t in pool if t["tripId"] == tid_a),
                next(t["flightTime"] for t in pool if t["tripId"] == tid_b),
                now)
            print(f"  {tid_a[:8]}..<->{tid_b[:8]}.. score={score:.3f} dist={dist_km}km detour={detour}min thr={thr:.2f}")

        print(f"\n[RUN] optimize_pool su MXP con {TABLE_NAME}...")
        tentative = optimize_pool(airport, now)
        print(f"[RESULT] TentativeMatch creati: {tentative}")

        # Cerca match/TM creati per i due trip
        result = ddb.query(
            TableName=TABLE_NAME,
            IndexName="GSI5-TripStatus",
            KeyConditionExpression="gsi5pk = :pk",
            ExpressionAttributeValues={":pk": {"S": "MXP#tentative_match"}},
        )
        for item in result.get("Items", []):
            from boto3.dynamodb.types import TypeDeserializer
            d = TypeDeserializer()
            row = {k: d.deserialize(v) for k, v in item.items()}
            if FAKE_TRIP_ID in (row.get("tripId1"), row.get("tripId2")):
                print(f"\n[MATCH TROVATO] TentativeMatch: {row.get('matchId')}")
                print(f"  tripId1: {row.get('tripId1')}")
                print(f"  tripId2: {row.get('tripId2')}")
                print(f"  score:   {row.get('score')}")
                print(f"  lockAt:  {row.get('lockAt')}")

    finally:
        delete_fake_trip(ddb, FAKE_TRIP_ID)
        print("\n[DONE] Cleanup completato.")


if __name__ == "__main__":
    run()
