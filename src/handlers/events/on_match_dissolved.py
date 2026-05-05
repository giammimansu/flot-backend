from lib.airports import get_airport
from lib.dynamo import get_match, get_trip, table, now_iso
from aws_lambda_powertools import Logger

logger = Logger()

def repool_trip_with_exclusion(trip: dict, previous_partner_user_id: str):
    """Rimette un trip nel pool per il re-match dal Matchmaker escludendo l'ex partner."""
    table.update_item(
        Key={"pk": trip["pk"], "sk": "META"},
        UpdateExpression=(
            "SET #status = :status, "
            "gsi5pk = :gsi5, "
            "matchId = :mid, "
            "previousMatchPartners = list_append("
            "  if_not_exists(previousMatchPartners, :empty_list), :new_partner"
            "), "
            "updatedAt = :ua"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "scheduled",
            ":gsi5": f"{trip['airportCode']}#scheduled",
            ":mid": None,
            ":empty_list": [],
            ":new_partner": [previous_partner_user_id],
            ":ua": now_iso(),
        },
    )
    logger.info("trip_repooled_with_exclusion", tripId=trip["pk"], excludedPartner=previous_partner_user_id)

@logger.inject_lambda_context
def handler(event, context):
    detail = event["detail"]
    match_id = detail["matchId"]
    reason = detail["reason"]
    match = get_match(match_id)

    if match["status"] in ("unlocked", "completed", "unlock_expired", "dissolved"):
        return  # già gestito

    # Aggiorna match
    table.update_item(
        Key={"pk": f"MATCH#{match_id}", "sk": "META"},
        UpdateExpression="SET #status = :s, dissolveReason = :r, updatedAt = :ua",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":s": "dissolved",
            ":r": reason,
            ":ua": now_iso(),
        },
    )

    # Re-pool entrambi i trip
    airport = get_airport(match["airportCode"])
    if airport.unlock_repool_enabled:
        for trip_id in [match["tripId1"], match["tripId2"]]:
            trip = get_trip(trip_id)
            if trip["status"] not in ("cancelled", "expired", "completed"):
                partner_id = match["userId2"] if trip["userId"] == match["userId1"] else match["userId1"]
                repool_trip_with_exclusion(trip, partner_id)

    logger.info("match_dissolved", matchId=match_id, reason=reason)
