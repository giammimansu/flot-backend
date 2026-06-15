"""Flot — Pydantic validation models for API inputs.

All API inputs MUST be validated through Pydantic models.
ConfigDict(extra='forbid') rejects any unexpected fields.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


# ── Enums ────────────────────────────────────────────────────────────


class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


class AgeGroup(str, Enum):
    AGE_18_25 = "18-25"
    AGE_26_35 = "26-35"
    AGE_36_45 = "36-45"
    AGE_46_55 = "46-55"
    AGE_56_PLUS = "56+"


class Language(str, Enum):
    IT = "it"
    EN = "en"
    FR = "fr"
    DE = "de"
    ES = "es"


class Direction(str, Enum):
    TO_CITY = "TO_CITY"
    FROM_CITY = "FROM_CITY"


class TripStatus(str, Enum):
    SCHEDULED = "scheduled"
    SEARCHING = "searching"
    TENTATIVE_MATCH = "tentative_match"   # v4 — shadow pool
    TRACKING_PENDING = "tracking_pending"  # v4 — ETA not yet resolved
    MATCHED = "matched"
    UNLOCKED = "unlocked"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class MatchStatus(str, Enum):
    PENDING = "pending"
    UNLOCKED = "unlocked"
    ACTIVE = "active"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class TripMode(str, Enum):
    LIVE = "live"
    SCHEDULED = "scheduled"


# ── User Models ──────────────────────────────────────────────────────


class UpdateProfileRequest(BaseModel):
    """Validated fields for PUT /users/me."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=100)
    lang: Language | None = None
    gender: Gender | None = None
    ageGroup: AgeGroup | None = None  # noqa: N815 — matches frontend convention
    bio: str | None = Field(None, max_length=300)
    city: str | None = Field(None, max_length=100)
    onboarding: bool | None = None


class UserProfileResponse(BaseModel):
    """User profile as returned by GET /users/me."""

    userId: str  # noqa: N815
    email: str
    name: str | None = None
    photoUrl: str | None = None  # noqa: N815
    blurredPhotoUrl: str | None = None  # noqa: N815
    thumbUrl: str | None = None  # noqa: N815
    isPro: bool = False  # noqa: N815
    verified: bool = False
    lang: Language | None = None
    gender: Gender | None = None
    ageGroup: AgeGroup | None = None  # noqa: N815
    bio: str | None = None
    city: str | None = None
    onboarding: bool = False
    createdAt: str | None = None  # noqa: N815


class PhotoUploadResponse(BaseModel):
    """Response from PUT /users/me/photo."""

    uploadUrl: str  # noqa: N815
    photoKey: str  # noqa: N815


# ── Trip Models (Sprint 2, defined here for schema completeness) ─────


class CreateTripRequest(BaseModel):
    """Validated fields for POST /trips."""

    model_config = ConfigDict(extra="forbid")

    airportCode: str = Field(..., min_length=3, max_length=4)  # noqa: N815
    terminal: str = Field(..., min_length=2, max_length=4)
    direction: str = Field(..., min_length=2, max_length=32)  # validated against airport.direction_labels
    destination: str
    destLat: float = Field(..., ge=-90, le=90)  # noqa: N815
    destLng: float = Field(..., ge=-180, le=180)  # noqa: N815
    destPlaceId: str  # noqa: N815
    destZone: str | None = None  # noqa: N815
    # MVP TO_AIRPORT: departure address in city (required when direction == to_airport_direction).
    # Validated as required by the create_trip handler, not at Pydantic level,
    # so that the field remains optional for all other directions.
    originLat: float | None = Field(None, ge=-90, le=90)   # noqa: N815
    originLng: float | None = Field(None, ge=-180, le=180)  # noqa: N815
    originPlaceId: str | None = None                        # noqa: N815
    originLabel: str | None = None                          # noqa: N815 — human-readable address label
    luggage: int = Field(default=0, ge=0, le=6)
    paxCount: int = Field(default=1, ge=1, le=4)  # noqa: N815
    mode: TripMode
    # v4 — flightNumber+flightDate required; flightTime auto-resolved by tracker
    flightNumber: str = Field(..., min_length=2, max_length=10)  # noqa: N815
    flightDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")  # noqa: N815
    flightTime: str | None = None  # noqa: N815 — resolved by tracker, not from user

class TripCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None

class PushTokenUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(..., min_length=1)
    platform: str = Field(..., pattern="^(fcm|apns)$")


class ReviewDimensions(BaseModel):
    """Optional per-dimension star ratings (1-5). Any subset may be provided."""

    model_config = ConfigDict(extra="forbid")

    punctuality: int | None = Field(None, ge=1, le=5)
    sociability: int | None = Field(None, ge=1, le=5)
    reliability: int | None = Field(None, ge=1, le=5)
    cleanliness: int | None = Field(None, ge=1, le=5)


class CreateReviewRequest(BaseModel):
    """Payload for POST /matches/{matchId}/review (P2 #11)."""

    model_config = ConfigDict(extra="forbid")

    rating: int = Field(..., ge=1, le=5)  # overall, unchanged
    comment: str | None = Field(None, max_length=500)
    dimensions: ReviewDimensions | None = None  # optional multi-dimensional ratings


class ChatMessageCreate(BaseModel):
    """Payload for WS action=chat_message. Extra WS routing fields (action) are ignored."""

    model_config = ConfigDict(extra="ignore")

    matchId: str = Field(..., min_length=1)  # noqa: N815
    text: str = Field(..., min_length=1, max_length=1000)
