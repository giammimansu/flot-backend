"""Flot — State machine for Trip and Match lifecycle.

Validates status transitions at the application layer. DynamoDB ConditionExpression
guards in each handler remain as the persistence-layer safety net.

Usage:
    from lib.state_machine import TripStateMachine, MatchStateMachine

    TripStateMachine.transition("scheduled", "tentative_match")   # ok
    TripStateMachine.transition("completed", "scheduled")         # raises InvalidTransitionError
"""
from __future__ import annotations


class InvalidTransitionError(Exception):
    """Raised when a status transition is not permitted."""

    def __init__(self, entity: str, from_status: str, to_status: str) -> None:
        self.entity = entity
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"{entity}: illegal transition {from_status!r} → {to_status!r}"
        )


# ---------------------------------------------------------------------------
# Trip lifecycle
# ---------------------------------------------------------------------------
# scheduled → tentative_match → matched → partially_unlocked_wait → completed
#                                       ↘ expired
#          ↘ cancelled (from scheduled/tentative_match/searching)
#          ↘ searching (alias used during matchmaker scan — treated as scheduled)
#
# Note: "searching" and "scheduled" are both pre-match states; transitions
# between them are allowed (matchmaker may re-label).
# ---------------------------------------------------------------------------

_TRIP_EDGES: dict[str, set[str]] = {
    "scheduled":              {"tentative_match", "matched", "searching", "cancelled", "expired"},
    "searching":              {"tentative_match", "matched", "scheduled", "cancelled", "expired"},
    "tentative_match":        {"matched", "scheduled", "searching", "cancelled", "expired"},
    "matched":                {"partially_unlocked_wait", "completed", "expired", "cancelled"},
    "partially_unlocked_wait": {"completed", "expired", "cancelled"},
    # Terminal states — no outbound transitions.
    "completed":              set(),
    "expired":                set(),
    "cancelled":              set(),
}


class TripStateMachine:
    TERMINAL = frozenset({"completed", "expired", "cancelled"})

    @classmethod
    def transition(cls, from_status: str, to_status: str) -> None:
        """Validate a Trip status transition. Raises InvalidTransitionError if illegal."""
        allowed = _TRIP_EDGES.get(from_status)
        if allowed is None:
            raise InvalidTransitionError("Trip", from_status, to_status)
        if to_status not in allowed:
            raise InvalidTransitionError("Trip", from_status, to_status)

    @classmethod
    def is_terminal(cls, status: str) -> bool:
        return status in cls.TERMINAL


# ---------------------------------------------------------------------------
# Match lifecycle
# ---------------------------------------------------------------------------
# pending → partially_unlocked → unlocked → completed
#         ↘ unlock_expired (timeout, nobody unlocked second)
#         ↘ dissolved (no-response, user declined, matchmaker re-pool)
#         ↘ expired (flight departed, never unlocked)
# ---------------------------------------------------------------------------

_MATCH_EDGES: dict[str, set[str]] = {
    "pending":            {"partially_unlocked", "dissolved", "expired", "cancelled"},
    "partially_unlocked": {"unlocked", "unlock_expired", "dissolved", "expired"},
    "unlocked":           {"completed", "expired"},
    # Terminal states.
    "completed":          set(),
    "expired":            set(),
    "dissolved":          set(),
    "unlock_expired":     set(),
    "cancelled":          set(),
}


class MatchStateMachine:
    TERMINAL = frozenset({"completed", "expired", "dissolved", "unlock_expired", "cancelled"})

    @classmethod
    def transition(cls, from_status: str, to_status: str) -> None:
        """Validate a Match status transition. Raises InvalidTransitionError if illegal."""
        allowed = _MATCH_EDGES.get(from_status)
        if allowed is None:
            raise InvalidTransitionError("Match", from_status, to_status)
        if to_status not in allowed:
            raise InvalidTransitionError("Match", from_status, to_status)

    @classmethod
    def is_terminal(cls, status: str) -> bool:
        return status in cls.TERMINAL
