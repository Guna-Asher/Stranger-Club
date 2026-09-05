from __future__ import annotations

import contextvars
import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

REQUEST_ID_HEADER = "X-Request-ID"


def current_request_id() -> str:
    return _request_id_var.get()


class RequestIdLogFilter(logging.Filter):
    """Attaches the current request's correlation ID to every log record
    emitted while handling it, without threading it through every function
    call manually."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Every request gets a correlation ID: taken from X-Request-ID if the
    request came through a trusted proxy that sets it, otherwise generated
    here. Available to every log line for the duration of the request (via
    RequestIdLogFilter) and echoed back in the response header so a client
    or operator can correlate a specific request across HTTP -> DB ->
    storage -> realtime log lines."""

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming if incoming else uuid.uuid4().hex
        token = _request_id_var.set(request_id)
        try:
            response: Response = await call_next(request)
        finally:
            _request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
