# Architecture

## Production topology

```
Reverse proxy / Load balancer (TLS termination, trusted-proxy headers)
        |
1..N FastAPI/uvicorn instances (stateless)
        |
PostgreSQL (managed, single primary)
        |
Object storage (S3-compatible: Cloudflare R2 by default, or AWS S3 / any
S3-compatible provider — see storage.md)
```

Stranger Club is a modular monolith: one FastAPI application, one database,
one object store. There is no message queue, no cache layer, no service
mesh, and no plan to add one until real load actually demands it. Every
application instance is interchangeable and stateless — sessions, rate
limits, and realtime fan-out are all backed by PostgreSQL, not
process-local memory, so adding or removing instances is a pure capacity
decision with no application-code change.

The frontend is a static SPA served by the same FastAPI process
(`StaticFiles` mount + catch-all route) — no separate frontend server.

## Component responsibilities

| Component | Responsibility | Module |
|---|---|---|
| Config | Parse and validate all environment configuration once at startup; fail fast on missing/invalid production config | `backend/app/config.py` |
| Database | Dialect-aware engine/pool creation, migration execution | `backend/app/database.py` |
| Storage | `put`/`get`/`exists`/`presigned_url` — local filesystem (dev), S3-compatible (prod), in-memory fake (tests) | `backend/app/storage.py`, `storage_s3.py`, `storage_fake.py` |
| Realtime | SSE fan-out per event; in-process (dev/SQLite) or PostgreSQL LISTEN/NOTIFY (prod) | `backend/app/realtime.py` |
| Rate limiting | Atomic, PostgreSQL-backed fixed-window counters, shared across instances | `backend/app/rate_limit.py` |
| Correlation IDs | Per-request ID threaded through logs and error responses — see `observability.md` | `backend/app/middleware.py` |
| Services | All business logic and state machines (registration, payment, audit) | `backend/app/services.py`, `services_player.py` |
| Routers | HTTP surface, thin — authorization + calling into services | `backend/app/routers/` |

## Why no Redis, no queue, no Kubernetes

- **Rate limiting and realtime both run on PostgreSQL**, which the
  application already requires as its database of record. Introducing
  Redis would duplicate infrastructure to solve problems Postgres already
  solves at this project's scale (see `database.md` for the connection
  budget this assumes).
- **No background job queue exists** because nothing in the current product
  needs asynchronous processing outside the request/response cycle.
- **No Kubernetes / service mesh** because the deployment shape (1..N
  identical stateless instances behind a load balancer) does not need
  service discovery, sidecars, or orchestration beyond what any managed
  container platform (Render, Fly, Railway, ECS, etc.) already provides.

If a future requirement genuinely needs one of these, it should be added
because the workload proved it's needed — not pre-emptively.

## What changed in Phase 3

Phase 3 replaced every component that only worked correctly for exactly one
process:

| Concern | Before | After |
|---|---|---|
| Database | SQLite | PostgreSQL (SQLite remains for local dev/tests only) |
| Object storage | Local filesystem only | S3-compatible in production; local filesystem/in-memory fake for dev/tests |
| Realtime | In-process `dict` of queues | PostgreSQL LISTEN/NOTIFY, in-process fallback for SQLite dev |
| Rate limiting | In-process `dict` buckets | PostgreSQL atomic upsert counters |
| Migrations | `create_all()` + app-startup side effect | Explicit `alembic upgrade head` release step, advisory-lock guarded |
| Concurrency control | SQLite `BEGIN IMMEDIATE` only | Dialect-aware: `BEGIN IMMEDIATE` (SQLite) or row-level `SELECT ... FOR UPDATE` (PostgreSQL) |
