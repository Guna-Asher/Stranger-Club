# Stranger Club

Stranger Club is a full-stack app for organizing pickup cricket games. It
replaces the manual "Google Form → UPI QR code → payment screenshot →
WhatsApp confirmation" flow a lot of casual sports organizers still use,
and adds a layer on top of it: turning confirmed players into teams,
scheduling matches between them, and keeping a record of what happened.

- **Live demo (stable v1):** https://stranger-club.onrender.com
- **Code (this branch, v2, in development):** https://github.com/Guna-Asher/Stranger-Club

I built and maintain this project solo, as a way to go deep on a real
backend — not just CRUD, but the kind of correctness and concurrency
problems that show up once money and limited-capacity events are involved.

## Important: the demo link is v1, not this branch

The Render deploy above runs a separate, older, stable branch
(`deploy-v1.0.0-mvp`) that isn't touched once it's deployed. `main` (this
branch) is where active v2 development happens — new auth, teams,
fixtures, results. v2 is not deployed anywhere yet; the sections below
describe what's in this codebase, not necessarily what's live on the demo
link. This split exists so I can keep building v2 without breaking the one
deployment other people can actually click on.

## What it does

**Player flow:** discover an event → register → verify with OTP → pay the
organizer directly over UPI and upload a screenshot as proof → get
confirmed or waitlisted → get added to a team → play a fixture → see the
result.

**Organizer flow:** create an event → set UPI payment details → review
payment proofs (confirm/reject) → manage the registration/waitlist →
create teams and assign players → schedule fixtures → record results.

Stranger Club never touches money itself — payment happens externally over
UPI, and the app's job is to generate the payment request, collect proof,
and give the organizer a review screen.

**What it deliberately does not do:** live/ball-by-ball scoring, an
innings engine, automatic MVP/rating calculations, a payment gateway, or
anything multi-sport/multi-city. It's scoped to "help one organizer run
weekly games well," not to be a general sports platform.

## What I actually built

- A FastAPI + PostgreSQL backend with SQLAlchemy models and Alembic
  migrations (11 revisions so far) — event/registration/payment/team/
  fixture/result domain, each with real state machines
  (e.g. `DRAFT → OPEN → FULL → ONGOING → COMPLETED`)
- Two independent auth systems: phone-OTP for players, Argon2-hashed
  password auth for organizers, both with server-side sessions
- Capacity-safe registration and team assignment using row locks
  (`SELECT ... FOR UPDATE` / `BEGIN IMMEDIATE`) instead of hoping nothing
  races
- A React (Vite) frontend for both the player-facing flow and the
  organizer dashboard
- A Docker image that runs the same way in local dev and in production,
  with a `/health` and a `/ready` endpoint (the latter checks the DB
  migration head, not just "is the process up")
- Database-backed rate limiting and an append-only audit log, instead of
  in-memory counters that reset when a process restarts
- A pytest suite covering the API layer, plus a separate PostgreSQL-only
  set of tests for behavior SQLite can't exercise (row locking,
  `LISTEN`/`NOTIFY`, JSONB columns)

A couple of these (row-level locking, ownership checks that return `404`
instead of `403` so a non-owner can't even confirm a resource exists) came
from reading about real incidents elsewhere and deciding to build them in
from the start rather than bolting them on later.

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | React 19, Vite 8, react-router-dom 7 |
| Backend | Python 3.12, FastAPI |
| Database | PostgreSQL 16 (intended primary), SQLite (dev/test) |
| ORM / migrations | SQLAlchemy 2.0, Alembic |
| Object storage | Any S3-compatible provider (boto3) — Cloudflare R2, AWS S3, or MinIO for local dev |
| Auth | Argon2 (organizer passwords), phone OTP (players), server-side sessions |
| Containers | Docker, multi-stage build |
| Testing | pytest, httpx |
| CI | GitHub Actions |

## Architecture, briefly

```
Browser (React SPA)
        │
        ▼
FastAPI backend  ──►  PostgreSQL (source of truth)
        │                    │
        │                    └─ LISTEN/NOTIFY realtime, rate limits, audit log
        ▼
S3-compatible object storage (payment-proof screenshots, QR images)
```

One FastAPI process serves both the API and the built frontend — no
separate frontend server, no message queue, no cache layer. Everything
that needs to be shared across instances (sessions, rate limits, realtime)
lives in PostgreSQL rather than in process memory, which keeps the app
itself stateless.

More detail, including a note on a slightly confusing naming decision in
the schema (`Match` is the event, `Fixture` is what the UI calls a
"match"): [`docs/architecture.md`](docs/architecture.md). Full schema:
[`docs/database.md`](docs/database.md).

## Testing

```bash
pytest -q
```

Last run in this environment: **187 passed, 14 skipped** against SQLite,
with no setup required. The skipped tests are gated on environment
variables that point at a real PostgreSQL instance and S3-compatible
storage — they skip rather than fail when those aren't configured. CI
(`.github/workflows/ci.yml`) runs both the SQLite suite and a PostgreSQL +
MinIO suite against real service containers on every push/PR to `main`,
plus a frontend build and a Docker build with a `/health` check.

Frontend: `npm run build` builds cleanly; `npm audit` currently reports 0
vulnerabilities.

## Running it locally

Docker Compose is the easiest path — one command brings up the app,
PostgreSQL, and MinIO (a local S3-compatible store) together, migrated and
ready:

```bash
git clone https://github.com/Guna-Asher/Stranger-Club.git
cd Stranger-Club
docker compose up --build
```

Then open `http://localhost:8000`. A first organizer account is created
automatically (`organizer` / `local-development-only`, dev-only). Stop
with `docker compose down` (`-v` also to wipe local data).

Without Docker, for hot-reload editing:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
SC_DATA_DIR=./data SC_ADMIN_PASSWORD='local-dev-password' \
  .venv/bin/uvicorn backend.app.main:app --reload --port 8000

npm install
npm run dev   # http://localhost:5173
```

Full walkthrough and environment variables: [`docs/runbook.md`](docs/runbook.md).

## Project layout

```
backend/app/      FastAPI app — routers, models, services, config
alembic/versions/ One migration file per schema revision
src/              React frontend (admin/, player/, components/)
tests/            pytest suite
scripts/          Operational scripts (DB role setup, storage checks, restore testing)
docs/             Longer-form docs: architecture, database, deployment, runbook,
                  backups, disaster recovery, observability
```

The `docs/` folder goes deeper than this README on purpose — things like
the exact production environment variables, the backup/restore procedure,
and the reverse-proxy trust model are documented there rather than here.

## Deployment status, honestly

- The v1 MVP (`deploy-v1.0.0-mvp` branch) is deployed on Render and is
  what the live demo link runs.
- This branch (v2, `main`) has not been deployed anywhere yet.
  [`docs/deployment.md`](docs/deployment.md) documents the intended
  production setup (Docker image + managed PostgreSQL + S3-compatible
  storage) and is explicit about which parts are verified locally versus
  still needing a real provider account to confirm end-to-end.
- A scheduled-backup GitHub Actions workflow exists
  ([`.github/workflows/backup.yml`](.github/workflows/backup.yml)) but is
  a reviewed template, not an active job — it only starts running once
  its secrets are configured. See [`docs/backups.md`](docs/backups.md).
- Real SMS delivery for OTP isn't wired up yet — the current
  `ConsoleOtpProvider` prints codes for local testing, behind a swappable
  interface (`backend/app/otp/base.py`).

## License

No license file yet — all rights reserved by default until one is added.

---

Feedback, questions, or "why did you do X this way" — open an issue.
