# Stranger Club

Real strangers. Real cricket. A production-oriented platform for
discovering, organizing, and playing community cricket matches.

Stranger Club replaces the manual "Google Form → UPI QR → payment
screenshot → WhatsApp confirmation" workflow organizers use to run weekly
pickup cricket games, and extends it into the layer on top: turning
confirmed players into teams, scheduling matches between them, and keeping
a basic record of what happened. It is a backend-first system — FastAPI +
PostgreSQL as the source of truth, a React frontend, and a deliberately
small infrastructure footprint (one application process, one database, one
object store).

It is **not** a live scoring platform. Ball-by-ball input, scorecards, and
detailed statistics are explicitly out of scope — see [§4](#4-what-is-intentionally-not-included).

---

## 1. Status

- Core product workflows described below — registration, OTP
  authentication, external UPI payment + proof review, waitlisting, teams,
  fixtures, and results — are implemented and covered by the test suite in
  [§8](#8-testing).
- The application has been deployed to cloud infrastructure for controlled
  testing with a small group of real users, using a real PostgreSQL
  database and real S3-compatible object storage.
- Two things are intentionally deferred to a later deployment stage: a
  production SMS/OTP delivery provider, and a permanent public domain. Both
  are configuration-level integrations, not architectural gaps — the
  application's OTP and auth flows are provider-agnostic by design (see
  [`backend/app/otp/base.py`](backend/app/otp/base.py)).
- This repository is public for portfolio and technical-review purposes.

## 2. Product

**Player**

```
discover event → register → authenticate (OTP)
  → submit payment proof → confirmed / waitlisted
  → join team → play fixture → view result / history
```

**Organizer**

```
create event → configure payment → review proofs
  → manage registrations → create teams → assign players
  → create fixtures → record results / MVP / awards
```

Payment happens **externally**, over UPI, directly between the player and
the organizer — Stranger Club never touches money. It generates the UPI
request (deep link / QR), accepts an uploaded screenshot as evidence, and
gives the organizer a review screen to confirm or reject it. There is no
payment gateway and no automatic payment verification.

## 3. Core features

Grouped by domain. Everything listed here exists in this codebase today.

**Identity & Access**
- Player authentication via phone OTP, separate from organizer identity
- Organizer authentication (Argon2 password hashing) with `ORGANIZER` /
  `PLATFORM_ADMIN` roles
- Persistent player profiles, independent of any single event registration
- Server-side sessions for both identities, with hashed session tokens

**Events & Registration**
- Event lifecycle: `DRAFT → OPEN → FULL → ONGOING → COMPLETED` /
  `CANCELLED`
- Registration with capacity enforcement
- FIFO waitlist with promotion on cancellation
- Cancellation and re-registration handling

**Payments**
- Organizer-configured UPI payee details per event
- Per-registration payment snapshot (amount, payee details at time of
  registration)
- Payment-proof screenshot upload, with duplicate-screenshot detection
- Organizer review: confirm or reject, with waitlist promotion on capacity
  changes
- Append-only payment evidence — proofs are never deleted, only superseded

**Teams & Matches**
- Organizer-managed teams scoped to one event
- Manual roster assignment, with capacity enforcement
- Fixture scheduling between two teams of the same event
- Roster locking once a team has played
- Fixture lifecycle: `SCHEDULED → IN_PROGRESS → COMPLETED` / `CANCELLED`

**Results & Player History**
- Manually recorded match results: winner / draw / no-result
- Per-fixture participant records (who actually played, for which team)
- Optional Player of the Match / Best Batter / Best Bowler awards
- Player profile page: registrations, upcoming fixtures, recent results

**Platform**
- Realtime updates (registration/team/fixture changes) via PostgreSQL
  `LISTEN`/`NOTIFY`
- Audit log of every meaningful state change
- Distributed, database-backed rate limiting
- Per-request correlation IDs threaded through logs and error responses
- `/health` and `/ready` endpoints (readiness includes a migration-head
  check)
- S3-compatible object storage for payment-proof and QR images
- Docker-based deployment (single multi-stage image)

## 4. What is intentionally NOT included

- Media hub, photo/media uploads, or third-party scoring-app integration
- Live scoring, ball-by-ball input, an innings engine, or scorecards
- Automatic MVP/result computation, ratings, rankings, or leaderboards
- AI-assisted team balancing or tournament/bracket engines
- Social feed, chat, or a notifications platform
- Payment gateway or wallet functionality
- Multi-city or multi-sport architecture

## 5. Engineering highlights

The project is built around backend correctness — the database enforces
the invariants that matter, not just the application code.

**Database integrity**
- PostgreSQL is production-authoritative; SQLite is development/test-only
  and the app refuses to boot against it in `staging`/`production`
  (`backend/app/config.py`)
- Composite foreign keys make cross-event data corruption structurally
  impossible to insert — e.g. a `TeamMember` row can only reference a team
  and a registration that both resolve to the *same* event
  (`backend/app/models.py`)
- Partial unique indexes enforce business rules at the row level: one
  active registration per player per event, one pending payment proof per
  payment, one fixture sequence value per event when set
- A player's avatar design is enforced unique at the database level (a
  `UNIQUE` constraint on the design column), not by a low-collision-odds
  hash

**Concurrency**
- Capacity-sensitive writes (registration, payment review, team
  assignment, waitlist promotion) use `SELECT ... FOR UPDATE` on
  PostgreSQL and `BEGIN IMMEDIATE` on SQLite — a real row lock, not an
  optimistic retry
- Locks are taken on the resource whose capacity the decision actually
  depends on (the event, not the individual payment or registration row)
- Realtime (`LISTEN`/`NOTIFY`) is treated purely as a signal to refetch —
  the database, not a dropped or delayed notification, is the source of
  truth

**Security**
- Every ownership check (event, team, fixture, registration, payment)
  re-verifies on every request and returns `404` — never `403` — on a
  mismatch, so a non-owner can't confirm a resource even exists
- OTP codes and session tokens are stored as hashes, never in plaintext
- Rate limiting on login, OTP request/verify, and payment-related
  endpoints, backed by atomic PostgreSQL counters shared across instances
- Object storage credentials have no delete permission on payment-proof
  data; every read is authorized per-request through a short-lived
  presigned URL
- Least-privilege database roles: separate migration, application, and
  backup credentials, none with more privilege than its job needs
- Production configuration fails fast — a missing or invalid required
  environment variable stops startup with a specific error, never a silent
  insecure default

**Reliability**
- `/ready` fails closed if the database is unreachable or its
  `alembic_version` doesn't match the code's expected migration head
- Migrations run through a single explicit code path guarded by a
  PostgreSQL advisory lock, so concurrent deploys can't race each other
- Dockerfile `HEALTHCHECK` targets `/ready`, not `/health` — an
  unreachable or un-migrated database is reported unhealthy
- The container can run with a read-only root filesystem plus a `tmpfs`
  mount for `/tmp` once storage is S3 and the database is PostgreSQL

## 6. Architecture

```
Browser (React SPA)
        │
        ▼
FastAPI backend  ──►  PostgreSQL (production authority)
        │                    │
        │                    └─ LISTEN/NOTIFY realtime, rate-limit
        │                       counters, audit log, migrations
        ▼
S3-compatible object storage (payment-proof screenshots, QR images)
```

One FastAPI process serves both the API and the built frontend (a static
mount + catch-all route) — no separate frontend server. There is no
message queue, cache layer, or service mesh; every application instance is
stateless, since sessions, rate limits, and realtime fan-out all live in
PostgreSQL rather than process memory.

**A note on naming** (this trips up anyone reading the schema cold): the
ORM class `Match` is the **event** entity (a scheduled cricket meetup
players register for) — it was named before teams/fixtures existed. The
ORM class `Fixture` is what the product UI calls a "match": an actual
scheduled game between two teams within an event. See the docstrings on
both classes in [`backend/app/models.py`](backend/app/models.py).

Full design rationale: [`docs/architecture.md`](docs/architecture.md).

## 7. Tech stack

| Layer | Technology |
|---|---|
| Frontend | React 19, Vite 8, react-router-dom 7 |
| Backend | Python 3.12, FastAPI |
| Database | PostgreSQL 16 (production), SQLite (dev/test) |
| ORM | SQLAlchemy 2.0 |
| Migrations | Alembic |
| Object storage | Any S3-compatible provider (boto3 client) — Cloudflare R2, AWS S3, or MinIO (dev/CI) |
| Realtime | PostgreSQL `LISTEN`/`NOTIFY` (production), in-process fallback (dev) |
| Authentication | Argon2 (organizer passwords), phone OTP (players), server-side sessions |
| Containers | Docker, multi-stage build (`node:22-alpine` → `python:3.12-slim`) |
| Testing | pytest, httpx |
| CI/CD | GitHub Actions (SQLite tests, PostgreSQL tests, frontend build, Docker build+healthcheck) |

Exact resolved versions: [`requirements.lock.txt`](requirements.lock.txt)
(backend), [`package-lock.json`](package-lock.json) (frontend).

## 8. Data model

```
User ──► PlayerProfile

Match (Event)
  ├──► EventPaymentConfiguration
  ├──► Registration ──► Payment ──► PaymentProof
  ├──► Team ──► TeamMember
  └──► Fixture ──► MatchResult
                └──► MatchParticipant
```

- A `Registration` belongs to one `Match` (event) and one `User`; it owns
  at most one `Payment`, which owns one or more `PaymentProof` uploads
  (append-only).
- A `Team` belongs to one event; its `TeamMember` rows each reference one
  `Registration` — composite foreign keys guarantee both belong to the
  same event.
- A `Fixture` references two `Team`s from the same event. A
  `MatchParticipant` row records that a specific `Registration` actually
  played a specific `Fixture` for a specific `Team` — the chained
  composite foreign keys make it structurally impossible to record a
  participant against a team they were never assigned to.
- `MatchResult` is a single row per `Fixture`, with optional award fields
  (MVP / Best Batter / Best Bowler) that can only reference a real
  `MatchParticipant` of that same fixture.

Full schema and invariants: [`docs/database.md`](docs/database.md).

## 9. Testing

Latest verified run in this environment:

| Suite | Result |
|---|---|
| Backend (SQLite) | `168 passed, 14 skipped` |
| Backend (PostgreSQL + real object storage) | `176 passed, 6 skipped` |
| Frontend build | `npm run build` — passes |
| Frontend dependency audit | `npm audit` — 0 vulnerabilities |

```bash
pytest -q
```
runs the full suite against SQLite with no setup. Skipped tests are gated
on environment variables (`SC_TEST_DATABASE_URL`, `SC_TEST_S3_*`,
`SC_TEST_APP_ROLE_URL`/`SC_TEST_BACKUP_ROLE_URL`) that point at a real
PostgreSQL instance, real S3-compatible storage, and least-privilege
database roles respectively — unset, they skip rather than fail. The
PostgreSQL-gated portion exercises behavior SQLite can't: row-level
locking, `LISTEN`/`NOTIFY`, `JSONB` columns, and the composite-foreign-key
invariants above. Exact variables and setup:
[`docs/runbook.md`](docs/runbook.md#15-testing).

CI (`.github/workflows/ci.yml`) runs the SQLite suite, the PostgreSQL suite
against a real `postgres:16-alpine` service container (plus a from-scratch
migration bootstrap check), the frontend build, and a Docker build +
`/health` check, on every push/PR to `main`.

## 10. Deployment

The intended production shape:

```
Docker image ──► compute (e.g. EC2, or any container host)
                     │
                     ├──► managed PostgreSQL (e.g. RDS)
                     └──► S3-compatible object storage (e.g. S3, R2)
```

The same Dockerfile is used unmodified from local development through to
production — only the environment variables it's given differ. Production
requires `SC_ENV=production`, a real PostgreSQL `DATABASE_URL`,
`SC_STORAGE_BACKEND=s3` with real credentials, and `SC_TRUSTED_PROXY_IPS`
set to the actual reverse proxy in front of it — the application refuses
to start otherwise. Migrations run as an explicit release step
(`python -m backend.app.migrate`), never an application-startup side
effect once more than one instance is running.

Full environment variable reference, rollback procedure, and reverse-proxy
trust model: [`docs/deployment.md`](docs/deployment.md). Day-to-day
operational commands: [`docs/runbook.md`](docs/runbook.md).

No production endpoints, credentials, or infrastructure identifiers are
included in this repository.

## 11. Local development

The primary, recommended workflow is Docker Compose — one command starts
the app, PostgreSQL, and MinIO (a local S3-compatible store) together,
pre-migrated:

```bash
git clone https://github.com/Guna-Asher/Stranger-Club.git
cd Stranger-Club
docker compose up --build
```

Then open `http://localhost:8000`. The first organizer account is created
automatically: `organizer` / `local-development-only`. To stop:
`docker compose down`; to also wipe local data: `docker compose down -v`.

Native (no Docker) alternative, for hot-reload editing:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
SC_DATA_DIR=./data SC_ADMIN_PASSWORD='local-dev-password' \
  .venv/bin/uvicorn backend.app.main:app --reload --port 8000
```

```bash
npm install
npm run dev   # http://localhost:5173
```

Full setup detail, environment variables, and an end-to-end product
walkthrough: [`docs/runbook.md`](docs/runbook.md).

## 12. Project structure

```
backend/
  app/
    routers/            HTTP endpoints — thin, delegate to services.py
    models.py           SQLAlchemy models (schema source of truth)
    schemas.py          Pydantic request/response models
    services.py          Core business logic and state machines
    services_player.py    Player OTP auth logic
    config.py               Environment configuration, fail-fast validation
    database.py               Engine/session creation, migration execution
    migrate.py                  Explicit `python -m backend.app.migrate` entry point
    deps.py                      FastAPI dependencies (auth, ownership checks)
    storage.py / storage_s3.py    Object storage protocol + S3 implementation
    otp/                            OTP provider protocol + console (dev) implementation
    main.py                           FastAPI app assembly, health/readiness
alembic/versions/     One migration file per revision, applied in order
src/                  React frontend — admin/ (organizer UI), player/ (public UI),
                      components/, lib/
tests/                pytest suite — SQLite always; PostgreSQL/S3/role tests
                      are environment-variable-gated
scripts/              Operational scripts: role provisioning, bucket
                      protection, storage reconciliation, restore testing
docs/                 Architecture, database, storage, deployment, runbook,
                      backups, disaster recovery, observability
Dockerfile            Multi-stage build: frontend build → Python runtime
docker-entrypoint.sh  Starts uvicorn, wires reverse-proxy trust
```

## 13. Security

- All secrets (database credentials, storage keys, admin bootstrap
  password) are supplied through environment configuration — never
  committed to source, and `backend/app/config.py` fails startup if a
  required one is missing.
- Payment-proof and QR object storage is private: no public URLs, every
  read authorized per-request through a short-lived presigned URL.
- Session tokens and OTP codes are stored as hashes, never in plaintext.
- Authorization is enforced server-side on every request; ownership
  mismatches return `404`, not `403`.
- Critical invariants (capacity, one-active-registration,
  one-pending-proof, cross-event integrity) are enforced by database
  constraints, not application logic alone.

This repository does not currently define a formal responsible-disclosure
contact or security policy file. If you find a genuine vulnerability,
please raise it through a private channel on the repository (e.g. a
GitHub private security advisory) rather than a public issue.

## 14. Roadmap

- Integrate a real SMS/OTP delivery provider (the current
  `ConsoleOtpProvider` is a development-only stand-in behind a swappable
  `OtpProvider` interface — see [`backend/app/otp/base.py`](backend/app/otp/base.py))
- Configure a permanent public domain and HTTPS for the deployed instance
- Automate production database backups (a reviewed `pg_dump` GitHub
  Actions template exists at
  [`.github/workflows/backup.yml`](.github/workflows/backup.yml) but is
  not yet active — see [`docs/backups.md`](docs/backups.md))

## 15. License

No license file is currently present in this repository. All rights are
reserved by default until a license is added — do not assume permission to
reuse this code.
