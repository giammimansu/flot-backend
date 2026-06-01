import os
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key, Attr
from lib.airports import get_active_airports
from lib.dynamo import table, now_iso
from lib.eventbridge import put_event
from aws_lambda_powertools import Logger

logger = Logger()

# Hours to wait after flightTime before declaring an unlocked match "completed"
# (gives the shared flight time to depart/land before asking for a review).
COMPLETION_TOLERANCE_HOURS = int(os.environ.get("COMPLETION_TOLERANCE_HOURS", "2"))

@logger.inject_lambda_context
def handler(event, context):
    airports = get_active_airports()
    now = datetime.now(timezone.utc)
    now_iso_str = now.isoformat().replace("+00:00", "Z")
    completion_cutoff_iso = (
        now - timedelta(hours=COMPLETION_TOLERANCE_HOURS)
    ).isoformat().replace("+00:00", "Z")
    dissolved_count = 0
    expired_count = 0
    completed_count = 0

    # Match states that are already terminal — no dissolve/expire needed.
    TERMINAL = ("completed", "expired", "dissolved", "unlock_expired", "cancelled")

    for airport in airports:
        # Trova match in stato 'pending' per questo aeroporto
        # Non c'è un GSI specifico per status='pending', ma usiamo uno scan limitato se necessario 
        # oppure assumiamo che il GSI1 o la tabella permetta di trovarli.
        # Poiché il numero di match pending non è enorme, cerchiamo i trip in stato "matched" e ricaviamo i match.
        # Oppure cerchiamo per GSI5-TripStatus: airportCode#matched
        
        # Approccio più semplice: cerchiamo i trips in "matched" status
        response = table.query(
            IndexName="GSI5-TripStatus",
            KeyConditionExpression=Key("gsi5pk").eq(f"{airport.code}#matched")
        )
        trips = response.get("Items", [])
        
        checked_matches = set()
        for trip in trips:
            match_id = trip.get("matchId")
            if not match_id or match_id in checked_matches:
                continue
                
            checked_matches.add(match_id)
            
            # Fetch match
            match_resp = table.get_item(Key={"pk": f"MATCH#{match_id}", "sk": "META"})
            match = match_resp.get("Item")
            if not match or match["status"] in TERMINAL:
                continue

            # 1. Flight has departed. Branch on whether the connection was unlocked.
            flight_time = trip.get("flightTime", "")
            if flight_time and flight_time <= now_iso_str:
                if match["status"] == "unlocked":
                    # Happy path: connection established. Complete after tolerance so the
                    # flight has actually flown before we ask for a review.
                    if flight_time <= completion_cutoff_iso:
                        put_event("trip.completed", {
                            "matchId": match_id,
                            "reason": "flight_departed",
                        })
                        completed_count += 1
                        logger.info("scheduled_complete_emitted", matchId=match_id, flightTime=flight_time)
                    # else: still inside tolerance window — wait for next run.
                else:
                    # Never unlocked (pending/partially_unlocked) → expire.
                    put_event("match.expired", {
                        "matchId": match_id,
                        "reason": "flight_departed",
                    })
                    expired_count += 1
                    logger.info("scheduled_expire_emitted", matchId=match_id, flightTime=flight_time)
                continue

            # 2. Still pending and nobody responded within the window → dissolve & re-pool.
            if match["status"] != "pending":
                continue

            created_at = datetime.fromisoformat(match["createdAt"].replace("Z", "+00:00"))
            hours_since_creation = (now - created_at).total_seconds() / 3600

            if hours_since_creation >= airport.unlock_no_response_dissolve_hours:
                put_event("match.dissolved", {
                    "matchId": match_id,
                    "reason": "no_response"
                })
                dissolved_count += 1
                logger.info("scheduled_dissolve_emitted", matchId=match_id, hours=hours_since_creation)

    logger.info(
        "dissolve_checker_completed",
        dissolvedCount=dissolved_count,
        expiredCount=expired_count,
        completedCount=completed_count,
    )
