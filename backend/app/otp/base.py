from __future__ import annotations

from typing import Protocol


class OtpProvider(Protocol):
    """Delivers a one-time code to a phone number. The production provider
    (a real SMS vendor) can be swapped in later without touching the
    authentication/domain layer that calls this interface."""

    def send(self, phone: str, code: str) -> None: ...
