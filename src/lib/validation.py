"""Flot — Pydantic validation models for API inputs.

All API inputs MUST be validated through Pydantic models.
ConfigDict(extra='forbid') rejects any unexpected fields.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, EmailStr, Field


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
    SEARCHING = "searching"
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


# ── User Models ──────────────────────────────────────────────────────


class UpdateProfileRequest(BaseModel):
    """Validated fields for PUT /users/me."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=100)
    lang: Language | None = None
    gender: Gender | None = None
    ageGroup: AgeGroup | None = None  # noqa: N815 — matches frontend convention


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
    direction: Direction
    destZone: str = Field(..., min_length=2, max_length=20)  # noqa: N815
    flightTime: str  # ISO 8601 datetime  # noqa: N815
    luggage: int = Field(default=1, ge=0, le=5)
    paxCount: int = Field(default=1, ge=1, le=2)  # noqa: N815
