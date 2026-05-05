import pytest

def test_repooled_trip_excluded_from_same_partner():
    pass
    # \"\"\"Un trip re-pooled non deve essere ri-matchato con lo stesso partner.\"\"\"
    # trip = create_test_trip(
    #     status="scheduled",
    #     previousMatchPartners=["user_bad_partner"],
    # )
    # candidates = [
    #     {"userId": "user_bad_partner", "destLat": 45.47, "destLng": 9.19},
    #     {"userId": "user_good_partner", "destLat": 45.47, "destLng": 9.19},
    # ]
    # filtered = filter_candidates(trip, candidates)
    # assert len(filtered) == 1
    # assert filtered[0]["userId"] == "user_good_partner"
