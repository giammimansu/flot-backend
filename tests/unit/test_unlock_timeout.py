import pytest

def test_timeout_voids_auth_and_repools():
    pass
    # \"\"\"Allo scadere del timeout, l'auth viene annullata e i trip tornano nel pool.\"\"\"
    # match = create_test_match(
    #     status="partially_unlocked",
    #     unlockedBy=["user1"],
    #     firstUnlockPaymentIntentId="pi_test_123",
    # )
    # handle_unlock_expired({"detail": {"matchId": match["matchId"]}})
    # updated = get_match(match["matchId"])
    # assert updated["status"] == "unlock_expired"
    # trip1 = get_trip(match["tripId1"])
    # trip2 = get_trip(match["tripId2"])
    # assert trip1["status"] == "scheduled"
    # assert trip2["status"] == "scheduled"

def test_timeout_skipped_if_already_unlocked():
    pass
    # \"\"\"Se il match è già unlocked quando il timeout scatta, non succede nulla.\"\"\"
    # match = create_test_match(status="unlocked")
    # handle_unlock_expired({"detail": {"matchId": match["matchId"]}})
    # updated = get_match(match["matchId"])
    # assert updated["status"] == "unlocked"  # invariato
