# Observability

## What exists

- **Structured request logging**: every log line goes through Python's
  standard `logging` with a shared format including a `request_id` field
  (`backend/app/main.py`'s `logging.basicConfig` + `RequestIdLogFilter`).
- **Correlation IDs**: `CorrelationIdMiddleware`
  (`backend/app/middleware.py`) assigns a `request_id` to every request —
  taken from an incoming `X-Request-ID` header if present (only meaningful
  once a trusted reverse proxy sets it — see `deployment.md`), otherwise
  generated. It is attached to every log line emitted while handling that
  request (via a `contextvars.ContextVar`, so no function needs to thread
  it through manually), echoed back as the `X-Request-ID` response header,
  and included in every JSON error body as `"request_id"` — a
  user/organizer can quote it in a support message, and an operator can
  grep logs for that exact value across HTTP, DB, storage, and realtime log
  lines from the same request.
- **`/internal/diagnostics`**: `PLATFORM_ADMIN`-only (enforced by
  `require_platform_admin`, 404-not-403 for anyone else — consistent with
  every other ownership check in this codebase). Returns DB pool
  size/checked-out count, the active realtime backend type and its
  topic/subscriber counts, and which storage backend is active. Never
  returns connection strings, credentials, session tokens, or user/payment
  data — reviewed line-by-line to confirm this (see the table below for
  exactly what each subsystem's log lines/diagnostics do and don't expose).

## Log lines by category

Every line below is a real, existing `logger.*()` call — nothing in this
table is aspirational.

| Category | Log line(s) | What's included | What's deliberately excluded |
|---|---|---|---|
| Authentication (organizer) | `login_failed`, `login_succeeded`, `logout` | remote IP, organizer id | password (attempted or real) |
| Authentication (player/OTP) | `otp_requested`, `player_verified`, `otp_locked`, `otp_verify_failed` | challenge id, user id, attempt count | phone number, OTP code (attempted or real) |
| CSRF | `csrf_invalid` | organizer/user id, request path | the CSRF token (valid or supplied) |
| Database / migrations | `migration_lock_acquired`/`released`, `migration_run_starting`/`complete`/`failed` | env, dialect | connection string/credentials |
| Readiness | `readiness_failed reason=alembic_head_mismatch` (expected vs. actual revision) or `reason=database_unavailable` | revision ids | — |
| Rate limiting | `rate_limit_backend_unavailable` | the limiter key (e.g. `login:<ip>`, `otp_request_phone:<phone>` — the phone here is the same value already visible in the request itself, not newly exposed) | — |
| Realtime | `realtime_listener_connected`, `realtime_listener_disconnected, reconnecting in Ns`, `realtime_notify_failed`, `realtime_listen_failed`/`realtime_unlisten_failed` | event public_id | — |
| Storage | `storage_put_failed`, `storage_get_failed`, `storage_exists_check_failed`, `storage_presign_failed` | storage key, sanitized error (`storage_s3._safe_error` strips anything resembling a query string, so a presigned URL never lands in a log line) | credentials, full provider error internals, screenshot bytes |
| Unhandled errors | `unexpected_error` (full traceback, server-side only) | full exception | — the client only ever receives the generic `INTERNAL_ERROR` message + `request_id` |
| Domain events | `event_created`, `registration_created`, `registration_cancelled`, `payment_proof_submitted`, `payment_reviewed`, `event_ownership_backfilled`, `seed_event_created` | ids, statuses | screenshot bytes, UTR values, PII beyond ids |
| Backup (once `.github/workflows/backup.yml` is enabled — see `backups.md`) | the workflow run itself going red on a missing/undersized dump or a failed upload/verify | dump size, object key | database contents, credentials (GitHub Actions itself redacts values sourced from `secrets.*` in its own logs) |

## What is never logged, anywhere in this codebase

- OTP codes (attempted or real)
- Session tokens (organizer or player) — only their owning id
- CSRF tokens (valid or supplied)
- Full UTR values (`services.py`'s audit-log writer omits them entirely;
  no log line anywhere includes one)
- Raw payment-proof file bytes
- Storage/database credentials
- Presigned URLs (which would embed a signature) — `storage_s3._safe_error`
  strips anything after a `?` from a logged error message for exactly this
  reason

## `/internal/diagnostics` reviewed field-by-field

```json
{
  "database": {"dialect": "postgresql", "pool_checked_out": 2, "pool_size": 5},
  "realtime": {"backend": "PostgresBroadcaster", "active_event_topics": 3, "total_subscribers": 7},
  "storage": {"proof_backend": "S3Storage", "qr_backend": "S3Storage"}
}
```

Every value here is a count or a class name — never a connection string,
never a credential, never a specific user/event/payment identifier. Access
requires an authenticated organizer session with `role == PLATFORM_ADMIN`;
anyone else (including a regular organizer) gets the same `404` any other
ownership-checked resource returns for a non-owner, so the endpoint's
existence isn't even confirmable to them.

## Explicitly out of scope for this hardening pass

No Prometheus/Grafana/metrics-scrape endpoint exists or was added. The
logging + correlation-ID + diagnostics-endpoint combination above is
sufficient for manual troubleshooting at this project's current scale;
adding a metrics pipeline is a real, separate infrastructure decision that
should be made when actual operational scale justifies it, not
speculatively bundled into this pass.
