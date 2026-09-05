from __future__ import annotations


class ConsoleOtpProvider:
    """Development/test OTP provider. Never sends a real SMS.

    Deliberately prints to stdout rather than going through the app's
    structured logger, so OTP codes never end up in normal application logs
    even if this provider were mistakenly left active somewhere. Also keeps
    the last code per phone in memory so tests can read it directly instead
    of scraping output.
    """

    def __init__(self) -> None:
        self._last_codes: dict[str, str] = {}

    def send(self, phone: str, code: str) -> None:
        self._last_codes[phone] = code
        print(f"[DEV OTP] phone={phone} code={code}")

    def last_code_for(self, phone: str) -> str | None:
        return self._last_codes.get(phone)
