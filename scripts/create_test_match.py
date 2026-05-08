"""
Test script: creates a fake user + compatible trip, then invokes matchmaker Lambda.

Usage:
  python scripts/create_test_match.py           # create & match
  python scripts/create_test_match.py --cleanup # remove fake data
"""
import argparse
import json
import os
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeSerializer

TABLE = "Flot-dev"
REGION = "eu-south-1"
MATCHMAKER_FN = "flot-matchmaker-dev"
IDS_FILE = "scripts/.test_match_ids.json"

# Target trip (already scheduled in DynamoDB)
REAL_TRIP_ID = "0b62d96c-7e5b-4b38-96e6-db877943c0d3"
REAL_FLIGHT_TIME = "2026-05-08T12:00:00Z"
REAL_AIRPORT = "MXP"
REAL_DIRECTION = "TO_MILAN"
REAL_TERMINAL = "T1"

serializer = TypeSerializer()


def to_ddb(item: dict) -> dict:
    return {k: serializer.serialize(v) for k, v in item.items() if v is not None}


def main(cleanup: bool = False):
    ddb = boto3.client("dynamodb", region_name=REGION)
    lmb = boto3.client("lambda", region_name=REGION)

    if cleanup:
        print("Cleaning up fake data...")
        try:
            with open(IDS_FILE) as f:
                ids = json.load(f)
        except FileNotFoundError:
            print("No .test_match_ids.json found — nothing to clean up.")
            return
        ddb.delete_item(TableName=TABLE, Key=to_ddb({"pk": f"USER#{ids['fakeUserId']}", "sk": "PROFILE"}))
        ddb.delete_item(TableName=TABLE, Key=to_ddb({"pk": f"TRIP#{ids['fakeTripId']}", "sk": "META"}))
        os.remove(IDS_FILE)
        print("Done.")
        return

    fake_user_id = f"fake-{uuid.uuid4()}"
    fake_trip_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    flight_dt = datetime.fromisoformat(REAL_FLIGHT_TIME.replace("Z", "+00:00"))
    expires_at = int((flight_dt + timedelta(hours=2)).timestamp())

    # 1. Create fake user profile
    print(f"Creating fake user {fake_user_id}...")
    ddb.put_item(TableName=TABLE, Item=to_ddb({
        "pk": f"USER#{fake_user_id}",
        "sk": "PROFILE",
        "userId": fake_user_id,
        "name": "Test Passenger",
        "email": "testpassenger@flot.app",
        "lang": "it",
        "verified": False,
        "createdAt": now_iso,
    }))
    print("  Done.")

    # 2. Create fake trip — matches real trip on all dimensions
    #    destZone=nord, dest near 45.4955 / 9.1940 (real trip dest) → ~0.3 km apart
    print(f"Creating fake trip {fake_trip_id}...")
    ddb.put_item(TableName=TABLE, Item=to_ddb({
        "pk": f"TRIP#{fake_trip_id}",
        "sk": "META",
        "tripId": fake_trip_id,
        "userId": fake_user_id,
        "airportCode": REAL_AIRPORT,
        "terminal": REAL_TERMINAL,
        "direction": REAL_DIRECTION,
        "destination": "Piazza della Repubblica, Milano MI, Italia",
        "destLat": Decimal("45.4941100"),   # nord zone, ~0.2 km from real trip dest
        "destLng": Decimal("9.2021400"),
        "destPlaceId": "fake-place-id-test",
        "destZone": "nord",
        "mode": "scheduled",
        "flightNumber": "AZ9999",
        "flightDate": "2026-05-08",
        "flightTime": REAL_FLIGHT_TIME,
        "flightEtaUpdatedAt": now_iso,
        "timeBucket": REAL_FLIGHT_TIME,
        "luggage": 1,
        "paxCount": 1,
        "status": "scheduled",
        "tentativeMatchId": None,
        "lang": "it",
        "verified": False,
        "createdAt": now_iso,
        "expiresAt": expires_at,
        "gsi1pk": f"{REAL_AIRPORT}#{REAL_FLIGHT_TIME}",
        "gsi1sk": now_iso,
        "gsi2pk": f"USER#{fake_user_id}",
        "gsi2sk": now_iso,
        "gsi5pk": f"{REAL_AIRPORT}#scheduled",
        "gsi5sk": REAL_FLIGHT_TIME,
    }))
    print("  Done.")

    # Save IDs for cleanup
    with open(IDS_FILE, "w") as f:
        json.dump({"fakeUserId": fake_user_id, "fakeTripId": fake_trip_id}, f)

    # 3. Invoke matchmaker
    print("Invoking matchmaker Lambda...")
    resp = lmb.invoke(
        FunctionName=MATCHMAKER_FN,
        InvocationType="RequestResponse",
        Payload=json.dumps({}),
    )
    payload = json.loads(resp["Payload"].read())
    print(f"  Lambda status: {resp['StatusCode']}")
    print(f"  Result: {json.dumps(payload, indent=2)}")

    if resp.get("FunctionError"):
        print(f"\n  LAMBDA ERROR: {resp['FunctionError']}")
        return

    # 4. Check resulting statuses
    print("\nVerifying DynamoDB state...")
    for label, trip_id in [("real", REAL_TRIP_ID), ("fake", fake_trip_id)]:
        item = ddb.get_item(
            TableName=TABLE,
            Key=to_ddb({"pk": f"TRIP#{trip_id}", "sk": "META"}),
        ).get("Item", {})
        status = item.get("status", {}).get("S", "NOT FOUND")
        print(f"  [{label}] TRIP#{trip_id} → status={status}")

    print(f"\nCleanup: python scripts/create_test_match.py --cleanup")
