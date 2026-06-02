"""Unit tests for Trip and Match state machines."""
import pytest
from lib.state_machine import TripStateMachine, MatchStateMachine, InvalidTransitionError


# ---------------------------------------------------------------------------
# TripStateMachine
# ---------------------------------------------------------------------------

class TestTripStateMachine:
    def test_legal_scheduled_to_tentative(self):
        TripStateMachine.transition("scheduled", "tentative_match")

    def test_legal_tentative_to_matched(self):
        TripStateMachine.transition("tentative_match", "matched")

    def test_legal_matched_to_completed(self):
        TripStateMachine.transition("matched", "completed")

    def test_legal_matched_to_expired(self):
        TripStateMachine.transition("matched", "expired")

    def test_legal_scheduled_to_cancelled(self):
        TripStateMachine.transition("scheduled", "cancelled")

    def test_legal_searching_to_cancelled(self):
        TripStateMachine.transition("searching", "cancelled")

    def test_legal_scheduled_searching_interchangeable(self):
        TripStateMachine.transition("scheduled", "searching")
        TripStateMachine.transition("searching", "scheduled")

    def test_illegal_completed_to_scheduled(self):
        with pytest.raises(InvalidTransitionError) as exc_info:
            TripStateMachine.transition("completed", "scheduled")
        assert "completed" in str(exc_info.value)
        assert "scheduled" in str(exc_info.value)

    def test_illegal_cancelled_to_scheduled(self):
        with pytest.raises(InvalidTransitionError):
            TripStateMachine.transition("cancelled", "scheduled")

    def test_illegal_expired_to_matched(self):
        with pytest.raises(InvalidTransitionError):
            TripStateMachine.transition("expired", "matched")

    def test_unknown_from_status(self):
        with pytest.raises(InvalidTransitionError):
            TripStateMachine.transition("bogus_status", "scheduled")

    def test_is_terminal_true(self):
        for s in ("completed", "expired", "cancelled"):
            assert TripStateMachine.is_terminal(s)

    def test_is_terminal_false(self):
        for s in ("scheduled", "searching", "tentative_match", "matched"):
            assert not TripStateMachine.is_terminal(s)


# ---------------------------------------------------------------------------
# MatchStateMachine
# ---------------------------------------------------------------------------

class TestMatchStateMachine:
    def test_legal_pending_to_partially_unlocked(self):
        MatchStateMachine.transition("pending", "partially_unlocked")

    def test_legal_partially_unlocked_to_unlocked(self):
        MatchStateMachine.transition("partially_unlocked", "unlocked")

    def test_legal_unlocked_to_completed(self):
        MatchStateMachine.transition("unlocked", "completed")

    def test_legal_pending_to_dissolved(self):
        MatchStateMachine.transition("pending", "dissolved")

    def test_legal_pending_to_expired(self):
        MatchStateMachine.transition("pending", "expired")

    def test_legal_partially_unlocked_to_unlock_expired(self):
        MatchStateMachine.transition("partially_unlocked", "unlock_expired")

    def test_illegal_completed_to_pending(self):
        with pytest.raises(InvalidTransitionError):
            MatchStateMachine.transition("completed", "pending")

    def test_illegal_dissolved_to_pending(self):
        with pytest.raises(InvalidTransitionError):
            MatchStateMachine.transition("dissolved", "pending")

    def test_illegal_unlock_expired_to_unlocked(self):
        with pytest.raises(InvalidTransitionError):
            MatchStateMachine.transition("unlock_expired", "unlocked")

    def test_illegal_pending_to_completed(self):
        with pytest.raises(InvalidTransitionError):
            MatchStateMachine.transition("pending", "completed")

    def test_unknown_from_status(self):
        with pytest.raises(InvalidTransitionError):
            MatchStateMachine.transition("ghost_status", "pending")

    def test_is_terminal_true(self):
        for s in ("completed", "expired", "dissolved", "unlock_expired", "cancelled"):
            assert MatchStateMachine.is_terminal(s)

    def test_is_terminal_false(self):
        for s in ("pending", "partially_unlocked", "unlocked"):
            assert not MatchStateMachine.is_terminal(s)

    def test_error_message_contains_entity(self):
        with pytest.raises(InvalidTransitionError) as exc_info:
            MatchStateMachine.transition("completed", "pending")
        assert "Match" in str(exc_info.value)
