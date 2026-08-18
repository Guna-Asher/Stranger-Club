# Stranger Club

Stranger Club is a mobile-first cricket event workflow: players join a public event, pay by UPI, upload a proof, and organisers securely review registrations and payments.

## MVP architecture

- **React + Vite** presents public event and organiser screens.
- **FastAPI** owns event, registration, payment, and organiser-session rules.
- **SQLite** persists data in `/data/stranger_club.db`; uploaded proofs live in `/data/uploads`.
- The existing `matches` table remains the physical event table. Startup runs an append-only migration that adds the MVP domain fields/tables without dropping data.
- A lightweight in-process **SSE** broadcaster publishes committed event-summary updates. It is appropriate for the current single-container deployment.

Registration and payment have separate states:

- Registration: `PENDING`, `CONFIRMED`, `WAITLISTED`, `REJECTED`
- Payment: no row until submitted, then `SUBMITTED`, `VERIFIED`, or `REJECTED`

## Security

Only organisers authenticate. Public players do not need an account.

- Passwords are Argon2 hashes.
- `/api/admin/*` and payment-proof files require a server-side session cookie.
- State-changing organiser calls require the per-session `X-CSRF-Token` returned by login or `/api/auth/me`.
- Sessions expire after `SC_SESSION_HOURS` (12 by default) and logout invalidates them.
- Login failures are rate-limited in-process; use a shared limiter when moving to multiple app instances.

Set these before a production deployment:

```bash
export SC_ADMIN_USERNAME=organizer
export SC_ADMIN_PASSWORD='use-a-long-unique-password'
export SC_COOKIE_SECURE=true
export SC_ENV=production
```

In development only, a temporary `organizer` / `stranger-club-dev` account is seeded if `SC_ADMIN_PASSWORD` is not supplied. Do not use that fallback in production.

## Routes

Public UI: `/m/{public_id}` (the `/events/{public_id}` SPA path is also recognised).

Public API:

- `GET /api/events`
- `GET /api/events/{public_id}`
- `GET /api/events/{public_id}/summary`
- `GET /api/events/{public_id}/stream`
- `POST /api/events/{public_id}/registrations`
- `GET /api/registrations/{public_id}`
- `POST /api/registrations/{public_id}/payment`

Organiser API:

- `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout`
- `GET, POST /api/admin/events`
- `GET, PATCH /api/admin/events/{id}`
- `GET /api/admin/events/{id}/summary`
- `GET /api/admin/events/{id}/registrations`
- `GET /api/admin/payments/pending`
- `POST /api/admin/payments/{id}/confirm|reject`
- `POST /api/admin/registrations/{id}/promote`

Legacy `/api/matches/*` read/registration and `/api/admin/matches/*` aliases remain available for existing clients.

## Local development

```bash
npm install
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
SC_DATA_DIR=./data SC_ADMIN_PASSWORD='local-password' .venv/bin/uvicorn backend.app.main:app --reload --port 8000
```

In another terminal run `npm run dev`. Vite proxies API calls to port 8000.

Run checks:

```bash
.venv/bin/pytest -q
npm run build
```

## Docker

```bash
docker build -t stranger-club .
docker volume create stranger_club_data
docker run -d --name stranger-club -p 8000:8000 \
  -v stranger_club_data:/data \
  -e SC_ADMIN_USERNAME=organizer \
  -e SC_ADMIN_PASSWORD='use-a-long-unique-password' \
  -e SC_COOKIE_SECURE=true \
  -e SC_ENV=production \
  stranger-club
```

The volume preserves the database and uploads through container restarts. `/health` reports process health and `/ready` verifies database accessibility.

By Guna 

