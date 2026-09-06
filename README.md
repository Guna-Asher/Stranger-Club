# Stranger Club

Stranger Club is a mobile-first platform for organizing stranger-cricket
events — from registration and external payment through confirmation,
teams, matches, and post-match results.

It is **not** a cricket scoring platform. Detailed ball-by-ball scoring,
scorecards, and statistics can stay in an external tool such as CricHeroes;
Stranger Club owns the operational workflow around getting people
registered, paid, confirmed, organized into teams, and the basic record of
what happened afterward.

---

## 1. What Stranger Club is

A single system replacing the manual "Google Form → UPI QR → payment
screenshot → WhatsApp confirmation" workflow organizers were using to run
weekly pickup cricket games, plus the layer on top of that: turning
confirmed players into teams, scheduling matches between them, and
recording the result.

## 2. Core workflow

**Player**

```
register → pay externally via UPI → upload payment proof
  → organizer verifies → slot confirmed / waitlisted
  → team assigned → match played → post-match result recorded
```

**Organizer**

```
create event → manage registrations → verify payments
  → manage teams → schedule matches → complete matches
  → record result / MVP / basic participation
```

Payment happens **externally**, over UPI, between the player and the
organizer — Stranger Club never touches money. It generates the UPI
request (deep link / QR), accepts an uploaded screenshot as evidence, and
gives the organizer a review screen to confirm or reject that evidence.
There is no automatic payment verification and no payment gateway.

## 3. Current capabilities

- Player registration (no player account required to view an event; OTP
  phone verification to register/track status), with a FIFO waitlist once
  an event is full
- Organizer-configured UPI payment details, server-generated payment
  request per registration, payment-proof screenshot upload with
  duplicate-screenshot detection
- Organizer review: confirm / reject payment proofs, promote from waitlist
- Organizer-managed **Teams** scoped to one event, with manual player
  assignment
- Organizer-scheduled **Matches** between two teams of the same event
  (`SCHEDULED → IN_PROGRESS → COMPLETED`, or `CANCELLED`)
- Manually recorded **post-match results**: winner / draw / no-result,
  optional Player of the Match / Best Batter / Best Bowler, and a simple
  participation record — never computed automatically
- A common landing page (open/joinable events only — never draft, completed,
  or cancelled) and a persistent player **Profile** page (registrations,
  upcoming fixtures, recent results), separate from per-event Registration
- Player **avatars**: a fixed, code-defined catalog of deterministic
  pixel/identicon designs (`PlayerProfile.avatar_design_id`). A design's
  *current* owner is enforced by that column's own database UNIQUE
  constraint — never just a low-collision-probability hash — so one design
  can never be two players' current avatar at once; releasing one (by
  switching to another) frees it immediately for reassignment. Nothing about
  a design's appearance is stored — it's rendered deterministically from the
  ID alone, client-side (see `src/lib/avatar.js`)
- Real-time updates (players in the same event see registration/team/match
  changes live) via PostgreSQL `LISTEN`/`NOTIFY`
- Organizer authentication (Argon2 password hashing, server-side sessions,
  CSRF protection, `ORGANIZER`/`PLATFORM_ADMIN` roles) and player
  authentication (OTP-over-phone, separate server-side sessions)
- Audit log of every meaningful state change
- PostgreSQL in production, SQLite for local development/tests; S3-compatible
  object storage for payment-proof/QR images

## 4. What is intentionally NOT included

The product stops deliberately at the boundary above. None of the
following exist in this codebase today:

- Media Hub, photo/media uploads, Instagram or CricHeroes API integration
- Live scoring, ball-by-ball input, an innings engine, or scorecards
- Automatic MVP/result computation, player ratings, Elo, rankings, or
  leaderboards
- AI-assisted team balancing or tournament/bracket engines
- Social feed, chat, or a notifications platform
- Subscriptions, a payment gateway, or wallet functionality
- City/multi-city architecture or multiple sports

## 5. Architecture

```
Browser (mobile-first SPA)
        │
        ▼
FastAPI backend  ──►  PostgreSQL (production) / SQLite (dev, tests)
        │                    │
        │                    └─ LISTEN/NOTIFY realtime, distributed
        │                       rate limiting, audit log, migrations
        ▼
S3-compatible object storage (payment-proof screenshots, QR images)
```

- **Authentication**: two separate identities — `Organizer` (password +
  Argon2, server-side session, CSRF token, `ORGANIZER`/`PLATFORM_ADMIN`
  role) and `User`/player (phone OTP, its own server-side session). Neither
  shares a session mechanism with the other.
- **Authorization**: every organizer-scoped resource (event, team, match,
  registration, payment) independently re-verifies ownership on every
  request; a non-owner gets `404`, never `403`, so existence itself isn't
  leaked. `PLATFORM_ADMIN` can act on any organizer's data.
- **Payment-proof storage**: private object storage, opaque generated keys,
  no public URLs — every read is authorized per-request and served through
  a short-lived presigned URL (S3) or streamed directly (local dev only).
  The runtime credential has no delete permission — evidence is append-only.
- **Realtime**: PostgreSQL `LISTEN`/`NOTIFY` in production (an in-process
  fallback backs local SQLite dev) — the database is always the source of
  truth; a dropped notification is safe because clients refetch.
- **Rate limiting**: atomic PostgreSQL-backed counters, shared correctly
  across multiple application instances (no in-process state).
- **Audit logging**: one `AuditLog` table records actor, action, target, and
  before/after state for every meaningful mutation.
- **Migrations**: Alembic is the sole source of schema truth from its
  baseline forward, applied through a single explicit code path, guarded by
  a PostgreSQL advisory lock so concurrent deploys never race.
- **Health/readiness**: `/health` (process alive) and `/ready` (database
  reachable *and* at the expected migration revision) — see
  [`docs/runbook.md`](docs/runbook.md).

There is no message queue, cache layer, or service mesh — one FastAPI
process, one database, one object store, until real load actually demands
otherwise. See [`docs/architecture.md`](docs/architecture.md) for the full
design rationale.

## 6. Technology stack

| Layer | Technology | Version (as pinned in this repo) |
|---|---|---|
| Backend | Python | 3.12 (`Dockerfile`, CI) |
| Backend framework | FastAPI | `>=0.115,<1.0` (`requirements.txt`); `0.141.1` resolved (`requirements.lock.txt`) |
| ORM / migrations | SQLAlchemy 2.0 / Alembic | `alembic==1.19.2` |
| Database driver | psycopg 3 | `>=3.1,<4.0` |
| Object storage client | boto3 | `>=1.34,<2.0` |
| Frontend | React | `19.2.8` |
| Frontend build tool | Vite | `8.2.1` |
| Frontend router | react-router-dom | `7.18.3` |
| Frontend runtime | Node.js | 22 (`Dockerfile`, CI) |
| Database (production) | PostgreSQL | 16 (`.github/workflows/ci.yml`, disposable dev containers) |
| Database (dev/test) | SQLite | via Python's built-in `sqlite3` |
| Object storage (production) | Any S3-compatible provider (Cloudflare R2 recommended, AWS S3 works identically) | — |
| Object storage (dev/CI) | MinIO | — |
| Containerization | Docker (multi-stage: `node:22-alpine` → `python:3.12-slim`) | — |

Exact resolved backend dependency versions: `requirements.lock.txt`. Exact
frontend dependency versions: `package-lock.json`.

## 7. Repository structure

```
backend/
  app/
    routers/          HTTP endpoints — thin, delegate to services.py
    models.py          SQLAlchemy models (schema source of truth)
    schemas.py          Pydantic request/response models
    services.py          Business logic and state machines
    services_player.py    Player OTP auth logic
    config.py             Environment configuration, fail-fast validation
    database.py            Engine/session creation, migration execution
    migrate.py               Explicit `python -m backend.app.migrate` entry point
    deps.py                   FastAPI dependencies (auth, ownership checks)
    storage.py / storage_s3.py  Object storage protocol + S3 implementation
    main.py                       FastAPI app assembly, health/readiness
alembic/
  versions/            One file per migration, applied in order
src/                   React frontend (admin/ organizer UI, player/ public UI)
scripts/               Operational scripts (backup restore test, storage
                       reconciliation, bucket protection, SQLite→Postgres
                       migration, DB role provisioning SQL)
tests/                 pytest suite (SQLite always; PostgreSQL/MinIO/role
                       tests are environment-variable-gated)
docs/                  Detailed reference docs — see docs/runbook.md first
Dockerfile             Multi-stage build: frontend build → Python runtime
docker-entrypoint.sh   Starts uvicorn, wires reverse-proxy trust
alembic.ini            Alembic configuration (script location, logging)
requirements.txt / requirements.lock.txt   Python dependencies (ranges / pinned)
package.json           Frontend dependencies and npm scripts
.github/workflows/     CI (tests, build, Docker) and a backup workflow template
```

## 8. Prerequisites

| Tool | Required for | Verify with |
|---|---|---|
| Git | Everything | `git --version` |
| Docker (with Compose v2, i.e. Docker Desktop or an equivalent) | **The primary local workflow** — starts the app, PostgreSQL, and MinIO together | `docker --version`, `docker compose version` |
| Python 3.12 | Only for native (non-Docker) backend development | `python3 --version` |
| Node.js 22 | Only for native (non-Docker) frontend development | `node --version` |
| npm | Only for native frontend development | `npm --version` |
| `psql` (PostgreSQL client) | Optional — inspecting a Postgres database directly | `psql --version` |

Full detail, exact commands, and what's optional vs. required in each
environment: **[`docs/runbook.md`](docs/runbook.md)**.

## 9. Quick start

**The primary, recommended way to run Stranger Club locally is Docker
Compose — one command starts the app, PostgreSQL, and MinIO (a local
S3-compatible store) together, pre-migrated and ready.**

```bash
git clone https://github.com/Guna-Asher/Stranger-Club.git
cd Stranger-Club
docker compose up --build
```

Then open:

```
http://localhost:8000
```

That's the whole setup — no Python, Node, or manual database/storage setup
needed. It builds the same production-style image used for deployment
(§14), starts PostgreSQL and MinIO, creates the MinIO bucket, runs every
Alembic migration, and only then starts the application — using
local-development-only credentials baked into `docker-compose.yml` (see
[`docs/runbook.md`](docs/runbook.md#2-local-development-docker-compose-primary-workflow)
for the full explanation, how to stop/reset it, and how to override the
defaults via a `.env` file if you want to).

The first organizer account is created automatically:
`organizer` / `local-development-only` (see
[`docs/runbook.md`](docs/runbook.md#2-local-development-docker-compose-primary-workflow)
to change it).

To stop everything: `docker compose down`. To wipe local data and start
fresh: `docker compose down -v` — **destroys the local Postgres/MinIO data
volumes**, never a production command.

### Advanced: native development (no Docker)

For editing backend/frontend code with hot reload outside a container —
SQLite and local filesystem storage, zero external services:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
SC_DATA_DIR=./data SC_ADMIN_PASSWORD='local-dev-password' \
  .venv/bin/uvicorn backend.app.main:app --reload --port 8000
```

In a second terminal:

```bash
npm install
npm run dev
```

| What | URL |
|---|---|
| Player app (native dev server) | `http://localhost:5173/` |
| Organizer login (native dev server) | `http://localhost:5173/admin/login` |
| Backend directly | `http://localhost:8000/` |

Full detail on both workflows, plus the complete end-to-end product
walkthrough (register a player, pay, verify, build teams, schedule and
complete a match, record a result): **[`docs/runbook.md`](docs/runbook.md)**.

## 10. Environment configuration

All configuration is environment variables, validated at startup by
`backend/app/config.py` — an invalid or missing required variable fails
fast with a specific error message rather than silently falling back to an
insecure default. The full variable-by-variable table (required vs.
optional, development vs. production, safe local defaults) is in
**[`docs/runbook.md`](docs/runbook.md#6-environment-variables)**.

## 11. Database

PostgreSQL is production-authoritative; SQLite is development/test-only and
must never back a staging or production deployment (`config.py` refuses to
start otherwise). Alembic is the sole schema authority from its baseline
forward. See [`docs/database.md`](docs/database.md) for the full design
(connection pooling, least-privilege roles, row-locking strategy, schema
invariants) and [`docs/runbook.md`](docs/runbook.md) for exact setup and
migration commands.

## 12. Object storage

Payment-proof screenshots and organizer-uploaded QR images are stored
through a provider-agnostic `Storage` protocol: `LocalFilesystemStorage`
for development, any S3-compatible provider (Cloudflare R2 recommended, AWS
S3 or MinIO also work) in production via `S3Storage`. The interface has no
delete method — evidence is append-only. See
[`docs/storage.md`](docs/storage.md) for the full model and
[`docs/runbook.md`](docs/runbook.md) for running MinIO locally.

## 13. Running backend/frontend

The primary path is `docker compose up --build` (§9) — one command, no
separate terminals. The native/advanced path runs two independent
processes, always: the backend (`uvicorn`) and the frontend dev server
(`vite`), each in its own terminal — see §9's advanced section and
[`docs/runbook.md`](docs/runbook.md#11-starting-the-application).

## 14. Docker

`docker-compose.yml` (repository root) is the **local-only** development
stack — `docker compose up --build` (§9) starts the app, PostgreSQL 16,
and MinIO together, with automatic bucket creation and migrations before
the app starts. It has no bearing on production.

The underlying image is built the same way either locally or for
deployment:

```bash
docker build -t stranger-club .
```

A multi-stage build (Node build stage → Python 3.12-slim runtime, non-root
user, `HEALTHCHECK` against `/health`) — the **same image**, unmodified,
that Compose builds locally is what gets deployed to a platform such as
Render as a Docker Web Service, pointed at real managed PostgreSQL and
real S3-compatible storage instead of the local containers. Full sequence,
environment variables, and read-only-filesystem verification:
[`docs/runbook.md`](docs/runbook.md#15-docker).

## 15. Testing

```bash
pytest -q
```

runs the full suite against SQLite (no setup required). PostgreSQL-specific
behavior (row locking, `LISTEN`/`NOTIFY`, `JSONB`, composite foreign keys,
least-privilege roles) and S3-specific behavior only run when the
corresponding environment variables are set, and are otherwise skipped —
see [`docs/runbook.md`](docs/runbook.md#14-testing) for the exact variables
and how to run the PostgreSQL/MinIO suite locally. Frontend:
`npm run build` and `npm audit`.

## 16. Production deployment overview

```
Trusted reverse proxy / load balancer (TLS termination)
        │
1..N stateless application instances
        │
Managed PostgreSQL  +  private S3-compatible object storage
```

Every instance is interchangeable and stateless — sessions, rate limits,
and realtime fan-out all live in PostgreSQL, not process memory. `SC_ENV`
gates fail-fast configuration checks (PostgreSQL required, S3 storage
required, trusted proxy IPs required in `production`). Migrations are an
explicit release step (`python -m backend.app.migrate`), never an
application-startup side effect once more than one instance is running.
Full deployment order, required environment variables, and least-privilege
database roles: [`docs/runbook.md`](docs/runbook.md#16-production-configuration).

## 17. Backups/recovery overview

Two independent mechanisms: a managed PostgreSQL provider's point-in-time
recovery, and an independent `pg_dump` archive workflow
(`.github/workflows/backup.yml` — a **reviewed, tested template that is not
yet active**; see [`docs/backups.md`](docs/backups.md) for exactly what
that means). Restore correctness is verified by
`scripts/restore_test.py`, not assumed. Full procedures:
[`docs/runbook.md`](docs/runbook.md#18-backups) and
[`docs/disaster-recovery.md`](docs/disaster-recovery.md).

## 18. Security model

- Argon2 password hashing (organizer), OTP-over-phone (player) — never a
  shared credential store
- `HttpOnly`, `Secure` (outside local dev) session cookies for both
  identities, separate CSRF tokens per identity
- Every ownership check returns `404` (never `403`) on a mismatch, so a
  non-owner cannot even confirm a resource exists
- Distributed, fail-closed rate limiting on login, OTP request/verify, and
  payment-related endpoints
- Payment-proof storage credential has no delete permission; every read is
  authorized per-request through a short-lived presigned URL
- Reverse-proxy trust is explicit and opt-in (`SC_TRUSTED_PROXY_IPS`) —
  without it, forwarded headers are never trusted
- Full checklist before any production deployment:
  [`docs/runbook.md`](docs/runbook.md#24-security-checklist)

## 19. Operational documentation

| Document | Covers |
|---|---|
| [`docs/runbook.md`](docs/runbook.md) | **Start here for anything operational** — setup, testing, Docker, migrations, production configuration, backups/restore, troubleshooting |
| [`docs/architecture.md`](docs/architecture.md) | System design and the reasoning behind it |
| [`docs/database.md`](docs/database.md) | Connection pooling, roles, migrations, row locking, schema invariants |
| [`docs/storage.md`](docs/storage.md) | Object storage model, namespaces, payment-proof immutability |
| [`docs/backups.md`](docs/backups.md) | Backup mechanisms, RPO/RTO |
| [`docs/disaster-recovery.md`](docs/disaster-recovery.md) | Scenario-by-scenario incident response |
| [`docs/observability.md`](docs/observability.md) | What is logged, what is deliberately never logged |
| [`docs/deployment.md`](docs/deployment.md) | Environment variable reference, rollback |

## 20. Project status

Current implemented product boundary:

```
Registration → Payment → Confirmation → Teams → Matches → Results / MVP / Participation
```

Everything listed in [§4](#4-what-is-intentionally-not-included) is a
later, separate phase and does not exist in this codebase yet. Do not
assume a capability exists because it is a natural next step — check
[§3](#3-current-capabilities) and [§4](#4-what-is-intentionally-not-included) above.
