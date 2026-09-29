# Stranger Club

A full-stack app for organizing pickup cricket games — registration, UPI
payment proof review, teams, fixtures, and results.

**Live demo:** https://stranger-club.onrender.com
**Code:** https://github.com/Guna-Asher/Stranger-Club

## A quick note on versions

The live demo runs a separate, stable branch (`deploy-v1.0.0-mvp`) that's
deployed on Render and left alone once it's live. `main` — this branch —
is where I'm actively building v2: new auth, teams, fixtures, results.
v2 isn't deployed anywhere yet, so what you see on the demo link is an
earlier version, not what's described below. I kept the two separate on
purpose so I can keep changing v2 without breaking the one link people
can actually click.

## What it does

**As a player:** find an event → sign up → pay the organizer directly over
UPI and upload a screenshot as proof → get confirmed or waitlisted → get
added to a team → play a fixture → see the result.

**As an organizer:** create an event → set UPI payment details → review
payment proofs and confirm or reject them → manage the waitlist → build
teams → schedule fixtures between them → record results.

The app never handles money itself — it generates the UPI payment request,
collects a screenshot as evidence, and gives the organizer a review
screen. There's no payment gateway, no live/ball-by-ball scoring, and no
multi-sport support. It's built to help one organizer run their own weekly
games well, not to be a general sports platform.

## What I built

- **Backend:** FastAPI + PostgreSQL, with SQLAlchemy models and 11 Alembic
  migrations. Events, registrations, payments, teams, fixtures, and
  results each have their own state machine (e.g. an event goes
  `DRAFT → OPEN → FULL → ONGOING → COMPLETED`)
- **Frontend:** React (Vite) — separate player-facing flow and organizer
  dashboard, talking to the backend over a small fetch-based API client
- **Authentication:** players sign up and log in with email + password
  (Argon2-hashed); organizers use a separate username/password login, also
  Argon2. Both get server-side sessions with hashed tokens, not JWTs
- **Registration/payment workflow:** a registration and its payment are
  separate records on purpose — see below
- **Capacity/waitlist logic:** registering into a full event puts you on a
  FIFO waitlist; cancelling a confirmed spot promotes the next waitlisted
  player automatically
- **Teams/fixtures/results:** organizers assign players to teams within an
  event, schedule fixtures between two teams, and record who won and who
  played
- **Validation:** request bodies are validated with Pydantic; but the
  rules that actually matter (capacity, one active registration per
  player, one pending payment proof per payment) are enforced by database
  constraints, not just application code
- **Testing:** a pytest suite covering the API layer, plus a
  PostgreSQL-only set of tests for things SQLite can't exercise (row
  locking, `LISTEN`/`NOTIFY`)
- **Docker/CI:** one Dockerfile used the same way locally and in
  deployment, and a GitHub Actions pipeline that runs both test suites
  plus a frontend build and a Docker build+healthcheck

## A few engineering decisions worth explaining

- **Payments are their own record, not a field on a registration.**
  A registration can exist without a confirmed payment (pending, rejected,
  re-submitted), and I wanted the history of proof uploads to be
  append-only — old proofs are superseded, never deleted or overwritten.
  That only works cleanly if payment is its own table.
- **Capacity checks use real row locks, not "check then write."**
  Registering for an event and promoting someone off a waitlist both lock
  the event row (`SELECT ... FOR UPDATE` on Postgres, `BEGIN IMMEDIATE` on
  SQLite) before touching capacity, so two people registering for the last
  spot at the same time can't both succeed.
- **Ownership checks return 404, not 403.** If you try to act on an event,
  team, or payment you don't own, the API says it doesn't exist rather
  than that you're forbidden — so you can't even confirm a resource is
  real by probing it.
- **Login failures take the same amount of time whether the account
  exists or not.** The organizer login always runs an Argon2 verify — against
  a real hash on a real account, or a dummy precomputed hash if the
  username doesn't exist — so a timing difference can't leak which
  usernames are valid.
- **The database is the source of truth for anything shared across
  requests.** Sessions, rate-limit counters, and realtime fan-out
  (Postgres `LISTEN`/`NOTIFY`) all live in PostgreSQL instead of
  in-process memory. It's slightly more work than a global dict, but it
  means the app doesn't quietly break the moment there's more than one
  process running.
- **Phone-OTP login still exists in the backend but isn't used by the
  current frontend.** I built email+password auth for players later and
  switched the UI to it; the OTP endpoints (`/api/player/otp/*`) are still
  there and tested, mainly so a real SMS provider can be wired back in
  later without redesigning auth.

## Architecture

```
Browser (React SPA)
        │
        ▼
FastAPI backend  ──►  PostgreSQL (source of truth)
        │                    │
        │                    └─ sessions, rate limits, audit log, LISTEN/NOTIFY
        ▼
S3-compatible object storage (payment-proof screenshots, QR images)
```

One FastAPI process serves both the API and the built frontend — no
separate frontend server, no message queue, no cache layer. More detail
(including why the ORM class is called `Match` but the product calls it
an "event"): [`docs/architecture.md`](docs/architecture.md). Full schema:
[`docs/database.md`](docs/database.md).

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | React 19, Vite 8, react-router-dom 7 |
| Backend | Python 3.12, FastAPI |
| Database | PostgreSQL 16, SQLite for dev/tests |
| ORM / migrations | SQLAlchemy 2.0, Alembic |
| Object storage | S3-compatible (boto3) — MinIO locally, R2/S3 in the intended deployment |
| Auth | Argon2 password hashing, server-side sessions |
| Containers | Docker (multi-stage build) |
| Testing | pytest, httpx |
| CI | GitHub Actions |

## Testing

```bash
pytest -q
```

Current run in this environment: **187 passed, 14 skipped** against
SQLite, no setup needed. The skipped tests need a real PostgreSQL and
S3-compatible endpoint (via env vars) and just skip, not fail, without
them. CI (`.github/workflows/ci.yml`) runs that SQLite suite, a second
suite against real PostgreSQL + MinIO service containers, a frontend
build, and a Docker build with a `/health` check, on every push/PR to
`main`.

`npm run build` builds cleanly; `npm audit` currently reports 0
vulnerabilities.

## Running it locally

```bash
git clone https://github.com/Guna-Asher/Stranger-Club.git
cd Stranger-Club
docker compose up --build
```

Opens on `http://localhost:8000`, with Postgres and MinIO started
alongside it and migrations already applied. A first organizer account is
created automatically (`organizer` / `local-development-only`).
`docker compose down` to stop, add `-v` to also wipe local data.

Without Docker (for hot-reload):

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
SC_DATA_DIR=./data SC_ADMIN_PASSWORD='local-dev-password' \
  .venv/bin/uvicorn backend.app.main:app --reload --port 8000

npm install && npm run dev   # http://localhost:5173
```

Full setup and environment variables: [`docs/runbook.md`](docs/runbook.md).

## Project structure

```
backend/app/      FastAPI app — routers, models, services, config
alembic/versions/ One migration file per schema revision
src/              React frontend (admin/, player/, components/)
tests/            pytest suite
scripts/          DB role setup, storage checks, restore testing
docs/             Architecture, database, deployment, runbook, backups,
                  disaster recovery, observability
```

## Documentation

This README is meant to be read in a few minutes. The `docs/` folder has
the longer version of everything above: [`architecture.md`](docs/architecture.md),
[`database.md`](docs/database.md), [`deployment.md`](docs/deployment.md),
[`runbook.md`](docs/runbook.md), [`backups.md`](docs/backups.md),
[`disaster-recovery.md`](docs/disaster-recovery.md),
[`observability.md`](docs/observability.md), and
[`storage.md`](docs/storage.md).

## Current status / limitations

- `main` (this branch) is active v2 development and isn't deployed
  anywhere yet — the public Render demo runs the older, separate v1
  branch, as noted above.
- Real SMS delivery for OTP isn't wired up — the backend's OTP path
  (currently unused by the frontend, see above) just prints codes to the
  console for local testing, behind a swappable provider interface.
- There's no license file yet, so all rights are reserved by default
  until one is added.
- A scheduled-backup GitHub Actions workflow exists in the repo but is a
  reviewed template, not an active job — it only starts running once its
  secrets are configured (see [`docs/backups.md`](docs/backups.md)).

---

Questions or "why did you build it this way" — open an issue.
