from __future__ import annotations

from datetime import date as date_type, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

Position = Literal["BATSMAN", "BOWLER", "ALL_ROUNDER", "WICKET_KEEPER", "NO_PREFERENCE"]
EventStatus = Literal["DRAFT", "OPEN", "FULL", "ONGOING", "COMPLETED", "CANCELLED"]


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
    status: EventStatus = "OPEN"

    @field_validator("name", "venue", "upi_id")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank")
        return value

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
    upi_id: str | None = Field(default=None, min_length=3, max_length=120)
    status: EventStatus | None = None


class RegistrationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=10, max_length=16)
    email: EmailStr | None = None
    preferred_position: Position = "NO_PREFERENCE"

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value: str) -> str:
        cleaned = value.replace(" ", "").replace("-", "")
        if cleaned.startswith("+91"):
            cleaned = cleaned[3:]
        if not (cleaned.isdigit() and len(cleaned) == 10):
            raise ValueError("Enter a valid 10-digit phone number")
        return cleaned


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
    upi_id: str
    status: str
    confirmed_count: int = 0
    pending_count: int = 0
    payment_submitted_count: int = 0
    waitlist_count: int = 0
    available_slots: int = 0
    collected_amount: int = 0


class PaymentResponse(BaseModel):
    id: int
    amount: int
    status: str
    submitted_at: datetime
    verified_at: datetime | None = None
    rejection_reason: str | None = None
    screenshot_url: str | None = None


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
