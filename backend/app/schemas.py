from __future__ import annotations

import re
from datetime import date as date_type, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

Position = Literal["BATSMAN", "BOWLER", "ALL_ROUNDER", "WICKET_KEEPER", "NO_PREFERENCE"]
EventStatus = Literal["DRAFT", "OPEN", "FULL", "ONGOING", "COMPLETED", "CANCELLED"]

UPI_ID_PATTERN = re.compile(r"^[a-zA-Z0-9.\-]{2,256}@[a-zA-Z][a-zA-Z]{1,64}$")


def normalize_upi_id(value: str) -> str:
    value = value.strip()
    if not UPI_ID_PATTERN.match(value):
        raise ValueError("Enter a valid UPI ID, e.g. name@bank")
    return value


def normalize_phone(value: str) -> str:
    cleaned = value.replace(" ", "").replace("-", "")
    if cleaned.startswith("+91"):
        cleaned = cleaned[3:]
    if not (cleaned.isdigit() and len(cleaned) == 10):
        raise ValueError("Enter a valid 10-digit phone number")
    return cleaned


class MatchCreate(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    date: date_type
    start_time: time
    end_time: time
    venue: str = Field(min_length=3, max_length=200)
    capacity: int = Field(ge=2, le=200)
    fee: int = Field(ge=1, le=100_000)
    registration_deadline: datetime
    upi_id: str = Field(min_length=3, max_length=120)
    payee_name: str = Field(default="Stranger Club", min_length=1, max_length=80)
    status: EventStatus = "OPEN"

    @field_validator("name", "venue")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank")
        return value

    @field_validator("upi_id")
    @classmethod
    def valid_upi(cls, value: str) -> str:
        return normalize_upi_id(value)

    @field_validator("payee_name")
    @classmethod
    def trim_payee_name(cls, value: str) -> str:
        value = value.strip()
        return value or "Stranger Club"

    @field_validator("end_time")
    @classmethod
    def end_must_be_after_start(cls, value: time, info) -> time:
        if "start_time" in info.data and value <= info.data["start_time"]:
            raise ValueError("End time must be after start time")
        return value


class EventUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=3, max_length=120)
    date: date_type | None = None
    start_time: time | None = None
    end_time: time | None = None
    venue: str | None = Field(default=None, min_length=3, max_length=200)
    capacity: int | None = Field(default=None, ge=2, le=200)
    fee: int | None = Field(default=None, ge=1, le=100_000)
    registration_deadline: datetime | None = None
    status: EventStatus | None = None


class PaymentConfigurationUpdate(BaseModel):
    """Editing an event's live payment configuration. Never touches any
    existing Payment record — those keep the snapshot they were created
    with. qr_source may only be set to GENERATED here (reverting a custom
    QR); setting UPLOADED happens only via the QR upload endpoint, which is
    the only path that actually has image bytes to store."""
    payee_upi_id: str | None = Field(default=None, min_length=3, max_length=120)
    payee_name: str | None = Field(default=None, min_length=1, max_length=80)
    qr_source: Literal["GENERATED"] | None = None

    @field_validator("payee_upi_id")
    @classmethod
    def valid_upi(cls, value: str) -> str:
        return normalize_upi_id(value)

    @field_validator("payee_name")
    @classmethod
    def trim_payee_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank")
        return value


class PaymentConfigurationResponse(BaseModel):
    payee_upi_id: str
    payee_name: str
    qr_source: str
    has_custom_qr: bool
    updated_at: datetime


class RegistrationCreate(BaseModel):
    # Phone is not submitted here: it comes from the OTP-verified player
    # session, never from client-supplied request data.
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr | None = None
    preferred_position: Position = "NO_PREFERENCE"

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("email", mode="before")
    @classmethod
    def blank_email_is_none(cls, value):
        # The optional email field is intentionally omittable; an empty string
        # (what a blank form field submits) means the same thing as omitting
        # it entirely, not an invalid email address.
        if isinstance(value, str) and not value.strip():
            return None
        return value


class OtpRequest(BaseModel):
    phone: str = Field(min_length=10, max_length=16)

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value: str) -> str:
        return normalize_phone(value)


class OtpVerify(BaseModel):
    phone: str = Field(min_length=10, max_length=16)
    code: str = Field(min_length=6, max_length=6)

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value: str) -> str:
        return normalize_phone(value)

    @field_validator("code")
    @classmethod
    def digits_only(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("Enter the 6-digit code")
        return value


class PlayerAuthResponse(BaseModel):
    phone: str
    csrf_token: str


class PlayerProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    cricket_role: Position | None = None
    skill_rating: int | None = Field(default=None, ge=1, le=10)
    bio: str | None = Field(default=None, max_length=500)

    @field_validator("display_name", "bio", mode="before")
    @classmethod
    def blank_is_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value


class PlayerProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    display_name: str | None = None
    cricket_role: str = "NO_PREFERENCE"
    skill_rating: int | None = None
    bio: str | None = None


class RejectRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class OrganizerResponse(BaseModel):
    username: str
    role: str


class AuthResponse(BaseModel):
    organizer: OrganizerResponse
    csrf_token: str


class EventSummary(BaseModel):
    event_id: str
    capacity: int
    confirmed: int
    pending: int
    payment_submitted: int
    waitlisted: int
    available: int
    collected_amount: int


class MatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    public_id: str
    name: str
    date: date_type
    start_time: time
    end_time: time
    venue: str
    capacity: int
    fee: int
    registration_deadline: datetime
    payment_configuration: PaymentConfigurationResponse
    status: str
    confirmed_count: int = 0
    pending_count: int = 0
    payment_submitted_count: int = 0
    waitlist_count: int = 0
    available_slots: int = 0
    collected_amount: int = 0


class DuplicateProofRef(BaseModel):
    proof_id: int
    registration_id: int
    player_name: str


class PaymentResponse(BaseModel):
    id: int
    status: str
    amount_due: int
    payee_upi_id_snapshot: str
    payee_name_snapshot: str
    qr_source_snapshot: str
    upi_uri: str
    qr_image_url: str | None = None
    submitted_at: datetime | None = None
    verified_at: datetime | None = None
    rejection_reason: str | None = None
    utr_reference: str | None = None
    # Organizer-review-only fields. Always None/omitted for the player-facing
    # view — see services.payment_to_response(viewer=...). Never leak these
    # (or the underlying screenshot) to a player who isn't the reviewer.
    screenshot_url: str | None = None
    pending_proof_id: int | None = None
    proof_count: int | None = None
    duplicate_of: list[DuplicateProofRef] | None = None


class RegistrationResponse(BaseModel):
    id: int
    public_id: str
    match_id: int
    name: str
    phone: str
    email: EmailStr | None = None
    status: str
    preferred_position: str = "NO_PREFERENCE"
    assigned_position: str | None = None
    created_at: datetime
    updated_at: datetime
    payment: PaymentResponse | None = None


class AdminMatchDetail(MatchResponse):
    registrations: list[RegistrationResponse]
