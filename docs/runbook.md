# Runbook

This is the authoritative operational guide for Stranger Club: local
development, testing, Docker, PostgreSQL, S3-compatible storage,
migrations, production configuration, backups, restores, troubleshooting,
and deployment verification.

Every command below was run against this repository while writing this
document. Where a step depends on a real external provider (a managed
PostgreSQL account, a real R2/S3 bucket, a real reverse proxy), that is
stated explicitly — this repository does not assume any provider account
exists.

A note on terminology: the product always calls a scheduled game between
two teams a **"Match"**. Internally, the code names that concept `Fixture`
— chosen only because the codebase's pre-existing `Match` class is actually
the *Event* (the cricket gathering itself), and the two needed different
names. You will see `Fixture` in code, migrations, and internal comments;
you will never see it in the UI.

1. **[Local development: Docker Compose (primary workflow)](#2-local-development-docker-compose-primary-workflow)**
   — start here; this is the recommended way to run Stranger Club locally.
2. [Prerequisites](#3-prerequisites)
3. Advanced: native development (without Docker) — skip unless you're
   editing backend/frontend code outside a container:
   [Clone](#4-clone-and-verify-repository) ·
   [Python environment](#5-python-environment) ·
   [Frontend environment](#6-frontend-environment) ·
   [Local database](#8-local-database) ·
   [Local PostgreSQL](#9-local-postgresql) ·
   [Local S3 / MinIO](#10-local-s3--minio)
4. [Environment variables](#7-environment-variables)
5. [Database migrations](#11-database-migrations)
6. [Starting the application](#12-starting-the-application)
7. [First admin/organizer setup](#13-first-adminorganizer-setup)
8. [First end-to-end test](#14-first-end-to-end-test)
9. [Testing](#15-testing)
10. [Docker](#16-docker)
11. [Production configuration](#17-production-configuration)
12. [Production deployment order](#18-production-deployment-order)
13. [Backups](#19-backups)
14. [Restore / disaster recovery](#20-restore--disaster-recovery)
15. [SQLite → PostgreSQL migration](#21-sqlite--postgresql-migration)
16. [Routine operations](#22-routine-operations)
17. [Troubleshooting](#23-troubleshooting)
18. [Clean reset / local restart](#24-clean-reset--local-restart)
19. [Security checklist](#25-security-checklist)
20. [Release checklist](#26-release-checklist)

---

## 2. Local development: Docker Compose (primary workflow)

**This is the recommended way to run Stranger Club locally.** One command
starts the application, PostgreSQL 16, and MinIO (a local S3-compatible
store) together, fully migrated and ready.

`docker-compose.yml` (repository root) is **local-only** — it has no
bearing on, and is never used by, production. The same application image
it builds (`Dockerfile`, unmodified) is exactly what gets deployed to a
platform such as Render as a Docker Web Service — see
[§11](#11-database-migrations) and [§17](#17-production-configuration) for
how production differs (real managed PostgreSQL and a real S3-compatible
provider instead of these containers; the application code never knows the
difference, since PostgreSQL and object storage are both external services
from its point of view in either place).

### Requirements

Only Docker (with Compose v2 — this ships with current Docker Desktop, or
install the `docker compose` plugin separately on Linux):

```bash
docker --version
docker compose version
```

### Start

From the repository root:

```bash
git clone https://github.com/Guna-Asher/Stranger-Club.git
cd Stranger-Club
docker compose up --build
```

Then open:

```
http://localhost:8000
```

Log in to the organizer console (`http://localhost:8000/admin/login`) with
`organizer` / `local-development-only` (overridable — see "Configuration"
below).

### What starts, and in what order

```
postgres  → healthy
minio     → healthy
                ├─ minio-init  → creates the "stranger-club-local" bucket, exits 0
                └─ migrate     → runs every Alembic migration, exits 0
                                        ↓
                                       app → starts, becomes healthy
```

Five services, defined in `docker-compose.yml`:

| Service | Image | Role |
|---|---|---|
| `postgres` | `postgres:16-alpine` | The database. Healthcheck: `pg_isready`. |
| `minio` | `minio/minio` | Local S3-compatible object storage (payment-proof/QR images). Healthcheck: the real `/minio/health/live` endpoint. |
| `minio-init` | `minio/minio` (same image, one-shot) | Creates the `stranger-club-local` bucket using the image's own bundled `mc` client, then exits. `mc mb --ignore-existing` — safe to run again on every `up`; a second run reports the same success message, not an error. |
| `migrate` | This repository's own image (built from `Dockerfile`) | Runs `python -m backend.app.migrate` — the exact same release-step command §11 documents for production — then exits. `SC_AUTO_MIGRATE=false` on both `migrate` and `app`, so exactly one thing ever applies migrations, never every instance racing to do it itself. |
| `app` | This repository's own image (same build as `migrate`) | The actual application — built React frontend + FastAPI backend, same image deployed to production. |

`app` does not start until `postgres` reports healthy **and** both
`migrate` and `minio-init` have exited successfully (`condition:
service_completed_successfully` in `docker-compose.yml`) — not a bare
`depends_on: service_started`, which would only guarantee the *containers*
exist, not that the database is reachable or the bucket/schema are ready.

### Where data lives

Two named, Compose-managed volumes — durable across `stop`/`start` and
plain `docker compose down`, erased only by `down -v`:

| Volume | Contents |
|---|---|
| `stranger_club_postgres_data` | The PostgreSQL database |
| `stranger_club_minio_data` | Every object in the `stranger-club-local` bucket (payment-proof screenshots, QR images) |

The application container itself holds no durable state — consistent with
production, where every instance is stateless.

### Configuration

`docker compose up --build` needs **no setup at all** — every credential
has a local-development-only default baked into `docker-compose.yml`
(`${VAR:-default}` syntax). To override any of them (e.g. to pick a
different local admin password, or to move a port), copy `.env.example` to
`.env` and edit it:

```bash
cp .env.example .env
```

Docker Compose reads `.env` automatically. **A real `.env` must never be
committed** — it's already in `.gitignore`; `.env.example` (committed) is
the documentation of what's overridable.

### Commands

```bash
docker compose up --build       # start (foreground, logs streaming)
docker compose up -d --build    # start (detached / background)
docker compose ps               # status of every service
docker compose logs -f app      # follow the application's logs
docker compose logs -f postgres
docker compose logs -f minio
docker compose down             # stop — data volumes are kept
docker compose down -v          # stop AND DELETE local Postgres/MinIO data
```

**`docker compose down -v` destroys the local PostgreSQL and MinIO data
volumes.** This is a local development reset command, never something to
run against a real deployment — there is no equivalent "production"
version of this command; production data lives in your managed provider,
entirely outside anything Compose touches.

### Rebuilding after code changes

`docker compose up --build` always rebuilds the image from the current
source before starting — Docker's own layer cache makes this fast when
only application code (not `requirements.lock.txt`/`package-lock.json`)
changed. If you want to be certain of a fully fresh build:

```bash
docker compose build --no-cache
docker compose up
```

### How migrations happen here

The `migrate` service runs `python -m backend.app.migrate` — the
repository's real, existing migration entry point, unchanged — against
`postgres` once it's healthy, then exits. `app` waits for that exit to be
successful before it ever starts. This is deliberately not `create_all()`
and not each `app` replica deciding for itself whether to migrate (that's
exactly the trap `SC_AUTO_MIGRATE=false` on the `app` service avoids). See
[§11](#11-database-migrations) for what this command does in detail and
how it differs from a bare `alembic` CLI invocation (which does not work
standalone in this repository).

### How the MinIO bucket gets created

The `minio-init` service — a genuinely temporary container, using the
`minio/minio` image's own bundled `mc` (MinIO Client) rather than a custom
script — runs:

```
mc alias set local http://minio:9000 <root user> <root password>
mc mb --ignore-existing local/stranger-club-local
```

then exits 0. `--ignore-existing` is what makes every subsequent `up`
succeed cleanly instead of failing on "bucket already exists" — you'll see
the same `Bucket created successfully` message either way; what matters is
the exit code, not the wording.

### Running tests against this stack

The Compose containers expose the same ports the test suite's own
environment variables expect — see [§15](#15-testing):

```bash
SC_TEST_DATABASE_URL=postgresql+psycopg://stranger_club:local-development-only@localhost:5432/stranger_club \
SC_TEST_S3_BUCKET=stranger-club-local \
SC_TEST_S3_ENDPOINT_URL=http://localhost:9000 \
SC_TEST_S3_ACCESS_KEY_ID=local_storage_admin \
SC_TEST_S3_SECRET_ACCESS_KEY=local_storage_admin_password \
  pytest -q
```

(Run this from a native Python environment — §4 — with Compose's `postgres`
and `minio` up; `pytest` itself is not one of the Compose services, since
it needs to reach into the *test* database/bucket the suite manages
itself, including dropping/recreating schemas between runs — not something
you want the `app`/`migrate` services doing.)

### A known local-only limitation: viewing a payment-proof image in the browser

Payment-proof retrieval works by redirecting an authorized request to a
**presigned URL** (§ `docs/storage.md`). That presigned URL is signed
against whatever `SC_STORAGE_ENDPOINT_URL` the app was configured with —
inside this Compose stack, that's `http://minio:9000`, a hostname that only
resolves *inside* the Compose network. The backend-to-backend mechanics are
correct and verified (signed, time-limited, 401 for anonymous requests,
403 on direct anonymous bucket access) — but your **browser**, running on
your host machine, cannot resolve `minio` any more than any other host
process can, so following that redirect to actually render the image fails
outside a container. This is a generic characteristic of any local
Compose+MinIO setup, not specific to this application, and it does not
exist in a real deployment (a real R2/S3 endpoint is a real public
hostname any browser can resolve). Workaround, if you want to visually
inspect proof images from your host browser during local development: add
a hosts-file entry mapping `minio` to `127.0.0.1` (e.g.
`echo "127.0.0.1 minio" | sudo tee -a /etc/hosts` on macOS/Linux). Not
required for anything else — the MinIO console at `http://localhost:9001`
(same root credentials) already lets you browse/download objects directly
without this workaround.

### How production differs

| | Local (Compose) | Production |
|---|---|---|
| Database | `postgres` container, ephemeral local volume | A real managed PostgreSQL provider |
| Object storage | `minio` container | A real S3-compatible provider (Cloudflare R2 recommended) |
| Migration | `migrate` one-shot Compose service | An explicit `python -m backend.app.migrate` run in your deploy pipeline — see [§17](#17-production-configuration) |
| `SC_ENV` | `development` | `production` — enables every fail-fast check in [§7](#7-environment-variables) |
| Credentials | Baked-in local defaults | Real secrets, provisioned per [§17](#17-production-configuration)–[§18](#18-production-deployment-order) |
| Reverse proxy / TLS | None (plain HTTP to `localhost:8000`) | Required — see [§17](#17-production-configuration) |

Nothing in `backend/app/` contains Compose-specific or Render-specific
logic — the application only ever sees `DATABASE_URL` and `SC_STORAGE_*`
pointing at *something* that speaks PostgreSQL/S3; it has no idea whether
that's `postgres`/`minio` or a real provider.

---

## 3. Prerequisites

| Tool | Status | Verify with | Notes |
|---|---|---|---|
| Git | REQUIRED | `git --version` | |
| Docker, with Compose v2 | REQUIRED for the primary workflow (§2) | `docker --version`, `docker compose version` | Ships together in current Docker Desktop; installed separately as the `docker compose` plugin on Linux |
| Python 3.12 | REQUIRED only for native/advanced development (§4) | `python3 --version` | Pinned in `Dockerfile` (`python:3.12-slim`) and `.github/workflows/ci.yml`. A newer 3.x will likely work but is not what CI/production run. |
| Node.js 22 | REQUIRED only for native/advanced frontend development (§4) | `node --version` | Pinned in `Dockerfile` (`node:22-alpine`) and CI. |
| npm | REQUIRED only for native/advanced frontend development (§4) | `npm --version` | Ships with Node. |
| `psql` (PostgreSQL client) | OPTIONAL — inspecting a Postgres database directly, or running `scripts/provision_database_roles.sql` | `psql --version` | |

DEVELOPMENT-ONLY vs PRODUCTION-ONLY is called out per-section below, not
here — several tools above (Docker, `psql`) are used in both contexts.

---

## 4. Clone and verify repository

**Everything from here through §10 (Local S3 / MinIO) is the native/advanced
development path** — editing backend/frontend code with native hot reload,
outside a container. If you followed §2 (Docker Compose), you already have
a running application and can skip straight to
[§11](#11-database-migrations) onward.

From wherever you keep projects:

```bash
git clone https://github.com/Guna-Asher/Stranger-Club.git
cd Stranger-Club
git status
```

A clean clone's `git status` reports:

```
On branch main
Your branch is up to date with 'origin/main'.

nothing to commit, working tree clean
```

If you see anything else (modified files, untracked files you didn't
create), investigate before proceeding — don't assume it's safe to
overwrite.

---

## 5. Python environment

Supported version: **3.12** (see [§3](#3-prerequisites)).

From the repository root:

```bash
python3 -m venv .venv
```

Activate it (this is per-shell; repeat in every new terminal you use):

```bash
source .venv/bin/activate
```

On Windows (PowerShell), the equivalent is `.venv\Scripts\Activate.ps1` —
not otherwise covered here, since every command below assumes a
Unix-style shell (macOS/Linux), the only platform this project's own CI and
Docker image target.

Install dependencies — from the repository root, with the venv activated:

```bash
python -m pip install -r requirements.txt
```

`requirements.txt` pins version *ranges*; `requirements.lock.txt` is a
fully resolved, pinned snapshot of what CI and the Docker image actually
install (`pip install -r requirements.lock.txt`) — reproducible builds
without a surprise transitive dependency bump. Use `requirements.txt` for
everyday development (as above); use the lockfile if you need to reproduce
exactly what production runs.

Verify the install:

```bash
python -m pip check
pytest -q
```

`pip check` reports dependency conflicts (should print nothing). `pytest -q`
runs the full SQLite-backed test suite (see [§15](#15-testing)) — a clean
run is the real verification that the environment is usable.

If you did not activate the venv, every `python`/`pytest`/`alembic`/`uvicorn`
command elsewhere in this document must be prefixed with `.venv/bin/` (e.g.
`.venv/bin/uvicorn ...`, `.venv/bin/pytest ...`) — this document uses the
activated form throughout for readability.

---

## 6. Frontend environment

Node 22 required (see [§3](#3-prerequisites)). No separate virtual
environment concept for Node — `npm` installs into `node_modules/` inside
the repository, ignored by `.gitignore`.

From the repository root:

```bash
npm ci
```

`npm ci` installs the *exact* versions in `package-lock.json` (what
`Dockerfile` uses: `RUN npm ci`) — use this the first time, or whenever
`package-lock.json` has changed. Use `npm install` only when you are
deliberately adding/updating a dependency yourself.

Start the frontend dev server:

```bash
npm run dev
```

```
VITE v8.2.1  ready in ...ms
  ➜  Local:   http://localhost:5173/
```

The dev server (port **5173**) proxies `/api` and `/health` requests to
`http://localhost:8000` (see `vite.config.js`) — it does **not** proxy
`/ready` or `/internal/diagnostics`; check those directly against the
backend's own port (`http://localhost:8000/ready`).

Production build:

```bash
npm run build
```

Output goes to `dist/` (gitignored; this is what `Dockerfile`'s frontend
build stage produces and copies into the image as `frontend_dist/`).

Dependency audit:

```bash
npm audit
```

The backend (FastAPI/uvicorn) always runs separately, on port 8000 — see
[§12](#12-starting-the-application).

---

## 7. Environment variables

Read and validated once, at startup, by `backend/app/config.py` —
`ConfigError` is raised (and the process refuses to start) for anything
required but missing. Nothing here is invented; every row corresponds to
an actual `os.getenv`/`os.environ` read in the code.

### Database

| Variable | Required? | Development | Production | Meaning |
|---|---|---|---|---|
| `DATABASE_URL` | Required in staging/production. Optional in development (falls back to SQLite). | Unset → `sqlite:///<SC_DATA_DIR>/stranger_club.db` | `postgresql+psycopg://user:pass@host/db` — **must** be PostgreSQL; app refuses to start otherwise | Full SQLAlchemy connection URL |
| `SC_DATA_DIR` | Optional | Default `data` (relative to working directory) | Not used (production requires `DATABASE_URL`/S3, never the local-filesystem fallback) | Where the SQLite file and local uploads live in development |
| `SC_DB_POOL_SIZE` | Optional | Default `5` | Tune per `docs/database.md`'s sizing formula | PostgreSQL connection pool size |
| `SC_DB_MAX_OVERFLOW` | Optional | Default `5` | Tune per `docs/database.md` | PostgreSQL pool overflow |
| `SC_AUTO_MIGRATE` | Optional | Default `true` | Set `false` once running more than one instance — see [§11](#11-database-migrations) | Whether the app applies migrations itself on startup |

### Auth / sessions

| Variable | Required? | Development | Production | Meaning |
|---|---|---|---|---|
| `SC_ADMIN_USERNAME` | Optional | Default `organizer` | Same | Username of the auto-created first organizer |
| `SC_ADMIN_PASSWORD` | **Required** the first time no organizer account exists yet — app refuses to start without it in that case | `<SET_THIS_SECRET>` — pick any local password | `<SET_THIS_SECRET>` — a real secret, never reused from dev | Password for the auto-created first organizer (see [§13](#13-first-adminorganizer-setup)) |
| `SC_SESSION_HOURS` | Optional | Default `12` | Same | Organizer session lifetime |
| `SC_PLAYER_SESSION_DAYS` | Optional | Default `30` | Same | Player session lifetime |

### Security / cookies

| Variable | Required? | Development | Production | Meaning |
|---|---|---|---|---|
| `SC_ENV` | Optional | Default `development` | Set explicitly to `production` (or `staging`) — gates every fail-fast check in this table | `development` \| `staging` \| `production` |
| `SC_COOKIE_SECURE` | Optional | Default off in `development` | Default **on** outside `development`; override with `true`/`false` | Sets the `Secure` flag on session cookies |
| `SC_TRUSTED_PROXY_IPS` | **Required in `production`** | Unset (no proxy trust) | Comma-separated IP(s)/CIDR(s) of your reverse proxy — `<SET_THIS>` | Which peer's `X-Forwarded-For`/`X-Forwarded-Proto` to trust — see [§17](#17-production-configuration) |
| `SENTRY_DSN` | Optional | Unset | Set if using Sentry | Error tracking; only initializes if set |

### Object storage

| Variable | Required? | Development | Production | Meaning |
|---|---|---|---|---|
| `SC_STORAGE_BACKEND` | Optional | Default `local` | **Must** be `s3` — app refuses to start otherwise | `local` (filesystem, dev/test only) or `s3` |
| `SC_STORAGE_BUCKET` | Required when `SC_STORAGE_BACKEND=s3` | — | `<SET_THIS>` | Bucket name |
| `SC_STORAGE_ENDPOINT_URL` | Required when `SC_STORAGE_BACKEND=s3` | `http://localhost:9000` for local MinIO | `<SET_THIS>` — your R2/S3 endpoint | S3-compatible endpoint |
| `SC_STORAGE_REGION` | Optional | Default `auto` | Set per provider if required | Region |
| `SC_STORAGE_ACCESS_KEY_ID` | Required when `SC_STORAGE_BACKEND=s3` | Local MinIO root user — LOCAL DEVELOPMENT ONLY, never a production credential | `<SET_THIS_SECRET>` | Runtime storage credential — must have **no delete permission** on `proofs/`/`qr/` (see `docs/storage.md`) |
| `SC_STORAGE_SECRET_ACCESS_KEY` | Required when `SC_STORAGE_BACKEND=s3` | Local MinIO root password — LOCAL DEVELOPMENT ONLY | `<SET_THIS_SECRET>` | — |
| `SC_STORAGE_ADMIN_ACCESS_KEY_ID` | Required only to run `scripts/reconcile_storage.py --confirm-delete` or `scripts/configure_bucket_protection.py` | LOCAL DEVELOPMENT ONLY value if used locally | A **separate**, more-privileged credential — never the runtime one | Administrative storage credential (delete/bucket-config capable) |
| `SC_STORAGE_ADMIN_SECRET_ACCESS_KEY` | Same as above | — | `<SET_THIS_SECRET>` | — |

### Realtime / rate limiting

No environment variables — the realtime backend (PostgreSQL `LISTEN`/`NOTIFY`
vs. an in-process fallback) and the rate-limiter backend are both selected
automatically from `DATABASE_URL`'s dialect. Nothing to configure.

### Docker

| Variable | Required? | Notes |
|---|---|---|
| `SC_DATA_DIR` | Set to `/data` inside the image (`Dockerfile`'s `ENV`) | Only meaningful if you run the container against SQLite/local storage (development only) |

### Testing only (never used by the application itself — see [§15](#15-testing))

| Variable | Purpose |
|---|---|
| `SC_TEST_DATABASE_URL` | Runs the PostgreSQL-specific portion of the test suite against a real instance |
| `SC_TEST_S3_BUCKET`, `SC_TEST_S3_ENDPOINT_URL`, `SC_TEST_S3_ACCESS_KEY_ID`, `SC_TEST_S3_SECRET_ACCESS_KEY`, `SC_TEST_S3_REGION` | Runs the real-S3 (MinIO) storage tests |
| `SC_TEST_APP_ROLE_URL`, `SC_TEST_BACKUP_ROLE_URL` | Runs the least-privilege database role boundary tests |

### Backups (GitHub Actions secrets, not application environment variables)

Referenced by `.github/workflows/backup.yml` (a template — see
[§19](#19-backups)): `BACKUP_DATABASE_URL`, `BACKUP_STORAGE_ACCESS_KEY_ID`,
`BACKUP_STORAGE_SECRET_ACCESS_KEY`, `BACKUP_STORAGE_ENDPOINT_URL`,
`BACKUP_STORAGE_BUCKET`, `BACKUP_STORAGE_REGION`. These are GitHub
repository secrets/variables, set under *Settings → Secrets and variables →
Actions* — never in a local `.env` file.

**No `.env.example` file exists in this repository.** Export the variables
you need directly in your shell, or use a local `.env`-loading tool of your
own choice (not provided or assumed here).

---

## 8. Local database

Two supported options.

### SQLite (development default)

No setup required. If `DATABASE_URL` is unset, the app uses
`sqlite:///<SC_DATA_DIR>/stranger_club.db` (default `SC_DATA_DIR` is
`data`, relative to wherever you run the process from).

What happens on first run against a database file that does not exist yet
(`backend/app/database.py`'s `make_session_factory`):

1. `Base.metadata.create_all(engine)` — creates every table from the
   current SQLAlchemy models directly.
2. The frozen, historical `backend/app/migrations.py` hand-rolled upgrade
   runs (pre-Alembic legacy bootstrap, kept only for schema compatibility).
3. Alembic **stamps** the database straight to the current head — it does
   **not** run the real incremental upgrade chain for a fresh SQLite file.

For an *existing* SQLite file, step 3 instead runs the real
`alembic upgrade head`.

`create_all()` is used **only** for this SQLite dev/test bootstrap. It is
never used for PostgreSQL outside migration `0000_postgres_bootstrap.py`
(see [§11](#11-database-migrations)).

**SQLite is not suitable for production** — `config.py` refuses to start
with `SC_ENV=staging` or `production` unless `DATABASE_URL` is a
PostgreSQL URL.

### PostgreSQL (recommended for local parity with production)

**PostgreSQL is production-authoritative.** If you want your local setup to
exercise the same code paths production does (row-level locking,
`LISTEN`/`NOTIFY`, `JSONB`, composite foreign keys), run PostgreSQL locally
— see [§9](#9-local-postgresql).

---

## 9. Local PostgreSQL

Using Docker (no local PostgreSQL install needed):

```bash
docker run -d \
  --name stranger-club-postgres \
  -e POSTGRES_USER=stranger_club \
  -e POSTGRES_PASSWORD=local-dev-password \
  -e POSTGRES_DB=stranger_club \
  -p 5432:5432 \
  postgres:16-alpine
```

`postgres:16-alpine` matches the version CI runs against
(`.github/workflows/ci.yml`). Port **5432** is the standard PostgreSQL
port and what this example uses — if it's already taken on your machine
(e.g. by another local Postgres), change the host side of `-p` (e.g.
`-p 5433:5432`) and adjust the connection URL below to match.

Verify it's accepting connections:

```bash
docker exec stranger-club-postgres pg_isready -U stranger_club
```

Expected: `/var/run/postgresql:5432 - accepting connections`

Connection URL for the app (`DATABASE_URL`):

```
postgresql+psycopg://stranger_club:local-dev-password@localhost:5432/stranger_club
```

Inspect logs:

```bash
docker logs -f stranger-club-postgres
```

Stop / restart (data persists across a restart — this container has no
volume mount, so data is lost if the container is **removed**, not merely
stopped):

```bash
docker stop stranger-club-postgres
docker start stranger-club-postgres
```

Remove the container and all its disposable local data:

```bash
docker rm -f stranger-club-postgres
```

This is a local development container, not production infrastructure —
removing it never affects anything outside your machine.

---

## 10. Local S3 / MinIO

Using Docker, matching exactly what `.github/workflows/ci.yml` runs:

```bash
docker run -d \
  --name stranger-club-minio \
  -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=local_storage_admin \
  -e MINIO_ROOT_PASSWORD=local_storage_admin_password \
  minio/minio server /data --console-address ":9001"
```

**`local_storage_admin` / `local_storage_admin_password` are LOCAL
DEVELOPMENT ONLY credentials.** They are not, and must never become,
production credentials.

Verify it's up:

```bash
curl -sf http://localhost:9000/minio/health/live && echo OK
```

Web console (optional, for browsing objects visually): `http://localhost:9001`

Create the bucket the application will use (MinIO does not create buckets
for you) — from the repository root, with the venv active:

```bash
python -c "
import boto3
client = boto3.client(
    's3', endpoint_url='http://localhost:9000',
    aws_access_key_id='local_storage_admin', aws_secret_access_key='local_storage_admin_password',
    region_name='us-east-1',
)
client.create_bucket(Bucket='stranger-club-local')
print('bucket created')
"
```

Application environment variables to point at this bucket:

```bash
export SC_STORAGE_BACKEND=s3
export SC_STORAGE_BUCKET=stranger-club-local
export SC_STORAGE_ENDPOINT_URL=http://localhost:9000
export SC_STORAGE_ACCESS_KEY_ID=local_storage_admin
export SC_STORAGE_SECRET_ACCESS_KEY=local_storage_admin_password
```

Verify an upload worked, and inspect objects, via the same boto3 pattern:

```bash
python -c "
import boto3
client = boto3.client(
    's3', endpoint_url='http://localhost:9000',
    aws_access_key_id='local_storage_admin', aws_secret_access_key='local_storage_admin_password',
    region_name='us-east-1',
)
for obj in client.list_objects_v2(Bucket='stranger-club-local').get('Contents', []):
    print(obj['Key'], obj['Size'], 'bytes')
"
```

(or use the web console at `http://localhost:9001` — same MinIO root
credentials.)

Reset disposable local storage (deletes every object; the bucket itself
goes with the container):

```bash
docker rm -f stranger-club-minio
```

Then re-run the `docker run` and bucket-creation commands above.

---

## 11. Database migrations

Alembic is the sole authoritative migration system from its baseline
(`0000_postgres_bootstrap.py`) forward. Current head: check
`alembic/versions/` — the highest-numbered file — or run:

```bash
alembic history
```

This works directly (no database connection needed — it only reads the
`versions/` directory) and prints every revision, newest first.

**Important, verified gotcha**: this repository's `alembic.ini` does not
set `sqlalchemy.url`, so the *bare* `alembic current` / `alembic upgrade`
/ `alembic downgrade` CLI commands fail with `KeyError: 'url'` — they are
not how migrations are actually run here. The two real, working entry
points are:

1. **The application itself**, automatically, on startup (governed by
   `SC_AUTO_MIGRATE`, default `true`) — see [§12](#12-starting-the-application).
2. **The explicit release-step command**, which reads `DATABASE_URL` from
   the environment via `config.py`:

   ```bash
   DATABASE_URL=postgresql+psycopg://... python -m backend.app.migrate
   ```

   Exit code `0` and a final `migration_run_complete` log line mean
   success; any other exit code means it failed (see the logged
   traceback).

   **Verified behavior difference by dialect**: against PostgreSQL, this
   command works correctly at any state, including a genuinely fresh,
   empty database (it runs migration `0000`'s full bootstrap). Against
   SQLite, it does **not** work against a database file that does not
   exist yet — it will fail partway through with `no such table: matches`,
   because the "fresh SQLite" bootstrap path (`create_all()` + stamp) only
   exists inside the application's own startup code, not in this
   standalone command. **For SQLite, do not use this command to create a
   new database — just start the application** (§12), which bootstraps
   correctly; use this command against SQLite only to apply migrations to
   a database that has already been bootstrapped at least once.

To run `alembic current` / `downgrade` / any other CLI-shaped command
against a *specific* database from a terminal (for inspection, or to
rehearse a downgrade — this is the exact method used to verify every
migration in this repository, including the SQLite-vs-PostgreSQL check
above):

```bash
python -c "
from alembic import command
from alembic.config import Config
config = Config('alembic.ini')
config.set_main_option('sqlalchemy.url', '<DATABASE_URL>')
command.current(config)          # or: command.upgrade(config, 'head')
                                  # or: command.downgrade(config, '<revision>')
"
```

To check a live database's actual current revision directly (works for
either dialect, needs only a DB client):

```bash
psql "$DATABASE_URL" -c "SELECT version_num FROM alembic_version;"
```

`/ready` performs exactly this comparison itself, against the constant
`ALEMBIC_EXPECTED_HEAD` in `backend/app/main.py` — a mismatch means new
application code was deployed before its migration ran (or a migration
partially failed). See [§23](#23-troubleshooting).

**`SC_AUTO_MIGRATE`**: `true` (default) — the application runs migrations
itself at startup, convenient for local dev and a single instance.
`false` — migrations become a separate, explicit step you must run
yourself (§18) before starting/rolling application instances; **required**
once you run more than one instance, so two instances don't both decide to
migrate.

**Advisory lock**: every PostgreSQL migration run (inline or via the CLI
command) acquires `pg_advisory_lock(875219003)` first and releases it in a
`finally` block — two callers racing to migrate the same database serialize
instead of corrupting each other's work, and a killed process never leaves
the lock stuck (Postgres releases it when the holding connection drops).

---

## 12. Starting the application

This section is the **native/advanced path** (§4–§10). If you're using
Docker Compose (§2), `docker compose up --build` already does all of this —
there is nothing further to run.

Two terminals, both from the repository root.

**Terminal 1 — backend:**

```bash
source .venv/bin/activate
SC_DATA_DIR=./data SC_ADMIN_PASSWORD='local-dev-password' \
  uvicorn backend.app.main:app --reload --port 8000
```

(Replace the `SC_DATA_DIR`/env vars with `DATABASE_URL=...`,
`SC_STORAGE_BACKEND=s3` etc. if you've set up local PostgreSQL/MinIO per
§9/§10.)

**Terminal 2 — frontend:**

```bash
npm run dev
```

| Endpoint | URL | Meaning |
|---|---|---|
| Player app | `http://localhost:5173/` | React dev server |
| Organizer login | `http://localhost:5173/admin/login` | |
| Backend directly | `http://localhost:8000/` | Same API the frontend proxies to |
| Health | `http://localhost:8000/health` | Process is alive. Always `{"status":"ok"}` if the process is up at all. |
| Readiness | `http://localhost:8000/ready` | Database reachable **and** at the expected Alembic revision. `200 {"status":"ready"}` or `503` — see [§23](#23-troubleshooting). |

---

## 13. First admin/organizer setup

There is no separate setup command. The first organizer account is created
**automatically, at application startup**, if none exists yet
(`backend/app/main.py`'s `lifespan`):

- Username: `SC_ADMIN_USERNAME` (default `organizer`)
- Password: `SC_ADMIN_PASSWORD` — **the application refuses to start** if
  no organizer exists yet and this is unset (`RuntimeError`: "No organizer
  account exists and SC_ADMIN_PASSWORD is not set").
- Role: `ORGANIZER` (not `PLATFORM_ADMIN`) — this is a plain organizer
  account that owns whatever events it creates, nothing more.

Verify login: `POST /api/auth/login` with `{"username": ..., "password": ...}`,
or simply log in through `http://localhost:5173/admin/login`.

**Promoting an account to `PLATFORM_ADMIN`** (the role that can act on
*every* organizer's events/teams/matches/payments, and access
`/internal/diagnostics`) has no API or CLI in this codebase — it is a
direct database update:

```bash
# SQLite:
sqlite3 data/stranger_club.db "UPDATE organizers SET role = 'PLATFORM_ADMIN' WHERE username = 'organizer';"

# PostgreSQL:
psql "$DATABASE_URL" -c "UPDATE organizers SET role = 'PLATFORM_ADMIN' WHERE username = 'organizer';"
```

Verify: log in again (existing sessions don't pick up a role change until
re-login) and confirm `GET /api/auth/me` reports `"role": "PLATFORM_ADMIN"`.

---

## 14. First end-to-end test

A complete local walkthrough of the real product workflow. Assumes the
backend and frontend are both running (§12) against either SQLite or local
PostgreSQL — either works identically for this walkthrough.

**If you're using Docker Compose (§2)**: everything below works the same,
except there is no separate frontend dev server — the built frontend is
served by the same app on `http://localhost:8000`, so read every
`localhost:5173` below as `localhost:8000` and skip straight to step 2 (the
OTP code in step 5 appears in `docker compose logs -f app` instead of a
bare terminal).

1. **Start database, storage, backend, frontend** — §8–§12 above. (Local
   filesystem storage, the SQLite-dev default, is fine for this walkthrough
   too — S3/MinIO is not required just to test the workflow.)
2. **Log in as organizer**: `http://localhost:5173/admin/login` with
   `organizer` / your `SC_ADMIN_PASSWORD`.
3. **Create an event**: Organizer HQ → *New Match* — name, date, times,
   venue, capacity, fee, registration deadline, and your organizer UPI ID
   (this is *your own* UPI handle for local testing — no real payment
   happens). Save.
4. **Register as a player**: open the event's public link
   (`http://localhost:5173/m/<public_id>`) in a private/incognito window
   (so it doesn't share the organizer's session), tap *Join Match*, enter a
   name, and request an OTP for any 10-digit phone number you like.
5. **Read the OTP code from the backend terminal** — the dev
   `ConsoleOtpProvider` never sends a real SMS; it prints
   `[DEV OTP] phone=<phone> code=<code>` directly to the backend terminal's
   stdout (or `docker logs` if running under Docker). Enter that code.
6. **Pay externally / upload proof**: the app shows a UPI deep link/QR and
   an upload dropzone. This is the point where, in real use, the player
   would actually pay via a UPI app — Stranger Club never processes that
   payment itself. For local testing, upload any JPEG/PNG/WEBP image as
   the "screenshot".
7. **Organizer verifies**: back in the organizer tab, open the event →
   *Payments to review* → open the submitted proof → *Confirm Payment*.
   The registration becomes `CONFIRMED` (or `WAITLISTED` if the event was
   already full).
8. **Create teams**: event → *Teams* tab → create at least two teams (e.g.
   "Team A", "Team B").
9. **Assign the confirmed player**: within a team, *Assign Player* → pick
   the now-confirmed registration.
10. **Create a match**: event → *Matches* tab → *Schedule Match* → pick
    Team A, Team B, a scheduled time.
11. **Mark the match completed**: on the match card, *Start Match* then
    *Mark Completed*.
12. **Record the result**: the completed match card now shows a Result
    form — pick the winner (or draw/no result), check off who actually
    participated, optionally pick Player of the Match / Best Batter / Best
    Bowler (only selectable from checked participants), optional notes,
    *Save Result*.
13. **Verify the player sees it**: back in the player's tab/window, refresh
    the status page — it now shows the assigned team, the match, and (once
    completed) the winner/awards/"did I play" — confirming the whole loop
    end to end.

---

## 15. Testing

```bash
pytest -q
```

runs the entire suite against SQLite — no external services required. This
is what CI's `backend-sqlite` job runs.

Run one file:

```bash
pytest tests/test_api.py -q
```

Run one test:

```bash
pytest tests/test_api.py::test_health_public_event_and_migration -q
```

Run by keyword:

```bash
pytest -k "concurrent" -q
```

### PostgreSQL-specific tests

Row locking, `LISTEN`/`NOTIFY`, `JSONB`, composite foreign keys, and
least-privilege role boundaries only run against a real PostgreSQL
instance — set `SC_TEST_DATABASE_URL` (§9 above for a local instance):

```bash
SC_TEST_DATABASE_URL=postgresql+psycopg://stranger_club:local-dev-password@localhost:5432/stranger_club \
  pytest -q
```

This **replaces the schema** of that database on every run (`DROP SCHEMA
public CASCADE; CREATE SCHEMA public`) — always point it at a disposable
local/CI database, never anything you care about.

### S3/MinIO tests

Set the `SC_TEST_S3_*` variables (§10 above for local MinIO):

```bash
SC_TEST_S3_BUCKET=stranger-club-local \
SC_TEST_S3_ENDPOINT_URL=http://localhost:9000 \
SC_TEST_S3_ACCESS_KEY_ID=local_storage_admin \
SC_TEST_S3_SECRET_ACCESS_KEY=local_storage_admin_password \
  pytest -q
```

### Role-gated tests

The least-privilege database role boundary tests additionally need
`SC_TEST_APP_ROLE_URL` and `SC_TEST_BACKUP_ROLE_URL` pointing at the
`stranger_club_app`/`stranger_club_backup` roles from
`scripts/provision_database_roles.sql`, run against the **same** database
as `SC_TEST_DATABASE_URL`.

### Why tests are skipped

Any test gated on an environment variable above prints as `s` (skipped),
not failed, when that variable is unset — this is expected, not a problem.
Run `pytest -q -rs` to see the skip reasons printed explicitly.

### Frontend checks

```bash
npm run build
npm audit
```

### Docker verification

See [§16](#16-docker).

---

## 16. Docker

If you just want a running app, use **§2 (Docker Compose)** —
`docker compose up --build` does everything below for you, with real
PostgreSQL and MinIO containers wired up automatically. What follows is
the manual, single-container path: useful for understanding exactly what
the image contains, for the read-only/hardening verification later in this
section, and as the native building block Compose itself builds on top of
(`docker-compose.yml`'s `app`/`migrate` services build this exact
`Dockerfile`, unmodified).

Build the image — from the repository root:

```bash
docker build -t stranger-club .
```

This is a two-stage build: `node:22-alpine` builds the frontend
(`npm ci && npm run build`), then the result is copied into a
`python:3.12-slim` runtime image alongside the backend, running as a
non-root user (`stranger_club`). This is the same image used by
`docker-compose.yml` (local) and the one you deploy to Render or any other
Docker host (production) — nothing about the image itself is
environment-specific; only the environment variables handed to it differ.

**Advanced/manual path** — run it against local PostgreSQL and MinIO
containers by hand (§9/§10 — start those first, then connect the app
container to them via `host.docker.internal`, or put all three containers
on one Docker network). Compose (§2) does this same wiring for you
automatically; this is the manual equivalent, useful for isolating a
single-container question without the rest of the stack:

```bash
docker network create stranger-club-net   # once

docker run -d --name stranger-club-postgres --network stranger-club-net \
  -e POSTGRES_USER=stranger_club -e POSTGRES_PASSWORD=local-dev-password -e POSTGRES_DB=stranger_club \
  postgres:16-alpine

docker run -d --name stranger-club-app --network stranger-club-net \
  -p 8000:8000 \
  -e SC_ENV=development \
  -e DATABASE_URL=postgresql+psycopg://stranger_club:local-dev-password@stranger-club-postgres:5432/stranger_club \
  -e SC_ADMIN_PASSWORD='local-dev-password' \
  stranger-club
```

This variant uses the default local-filesystem storage backend (writes to
`/app/data` inside the container) — fine for a quick check, but **not**
compatible with `--read-only` (see below).

Health check:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

Docker's own `HEALTHCHECK` (`curl -f http://localhost:8000/health` every
30s) is visible via:

```bash
docker inspect --format '{{json .State.Health.Status}}' stranger-club-app
```

**Read-only filesystem verification** — proves the application needs no
writable root filesystem at all when configured with PostgreSQL + S3
storage (the production configuration). This genuinely requires **both**
`--read-only --tmpfs /tmp` **and** a fully configured S3 backend together
— `--read-only` combined with the default local-filesystem storage backend
fails outright (`OSError: [Errno 30] Read-only file system: 'data'`), which
is expected: local storage is a development/testing fallback that needs a
writable directory, by design never used in production. Start MinIO first
(§10), create its bucket, then:

```bash
docker run -d --name stranger-club-app --network stranger-club-net \
  -p 8000:8000 \
  --read-only --tmpfs /tmp \
  -e SC_ENV=development \
  -e DATABASE_URL=postgresql+psycopg://stranger_club:local-dev-password@stranger-club-postgres:5432/stranger_club \
  -e SC_ADMIN_PASSWORD='local-dev-password' \
  -e SC_STORAGE_BACKEND=s3 \
  -e SC_STORAGE_BUCKET=stranger-club-local \
  -e SC_STORAGE_ENDPOINT_URL=http://stranger-club-minio:9000 \
  -e SC_STORAGE_ACCESS_KEY_ID=local_storage_admin \
  -e SC_STORAGE_SECRET_ACCESS_KEY=local_storage_admin_password \
  stranger-club
```

(`stranger-club-minio` here must be on the same `--network
stranger-club-net` — re-run its `docker run` from §10 with
`--network stranger-club-net` added, and create the bucket against
whatever host port you mapped for it.) `curl http://localhost:8000/ready`
returning `200` with the container fully read-only is the actual proof —
not the absence of a startup error alone.

**Non-root verification**:

```bash
docker exec stranger-club-app whoami
```

Expected: `stranger_club` (never `root`).

Logs:

```bash
docker logs -f stranger-club-app
```

Stop everything:

```bash
docker rm -f stranger-club-app stranger-club-postgres stranger-club-minio
docker network rm stranger-club-net
```

The commands above (plain `docker run` + a user-defined network) are the
verified manual way to run the full stack with Docker, container by
container. For normal day-to-day local development, use `docker-compose.yml`
(§2) instead — it does exactly this, deterministically, as one command.

---

## 17. Production configuration

Intended architecture:

```
Trusted reverse proxy / load balancer (TLS termination here)
        │
1..N stateless application instances
        │
Managed PostgreSQL   +   private S3-compatible object storage
```

None of the infrastructure below (a managed Postgres instance, a real
bucket, a real reverse proxy) exists in this repository or this
development environment — every item is something **you** provision.

**This is not the same stack as §2's Docker Compose environment.** Compose
exists only to make `localhost` development a single command; nothing about
it is deployed anywhere. In production, the same `Dockerfile` image runs
against a real managed PostgreSQL and a real S3-compatible bucket (e.g.
Cloudflare R2) — MinIO is never used outside local development. If your
deployment platform is Render specifically: deploy this repository as a
**Docker Web Service** (it builds `Dockerfile` directly, the same image
Compose builds locally), attach a Render managed PostgreSQL instance for
`DATABASE_URL`, and set the `SC_STORAGE_*` variables to a real R2/S3
bucket's credentials — Render itself needs no repository-specific
configuration beyond the environment variables in
[§7](#7-environment-variables), since the app has no Render-specific code
path (see `docs/deployment.md` for the full local-vs-production
comparison). No such deployment has actually been created from this
environment — this section documents the intended pattern, not a live
service.

### Required production environment variables

See the full table in [§7](#7-environment-variables). At minimum for
`SC_ENV=production`: `DATABASE_URL` (PostgreSQL), `SC_ADMIN_PASSWORD`,
`SC_STORAGE_BACKEND=s3` + its four required variables,
`SC_TRUSTED_PROXY_IPS`. The app fails fast at startup if any of these is
missing or invalid — this is enforced by code, not just documented here.

### Database roles

Three separate PostgreSQL roles, never one shared credential — provisioned
once via `scripts/provision_database_roles.sql` (PROVIDER/OPERATOR STEP —
run by a superuser/admin against your real database):

```bash
psql "<your-superuser-connection-string>" -f scripts/provision_database_roles.sql
```

Edit the placeholder passwords in that file (`CHANGE_ME_MIGRATOR`,
`CHANGE_ME_APP`, `CHANGE_ME_BACKUP`) before running it — see the file's own
comments for exactly what each role can and cannot do. Then, as documented
inside the script itself: run `python -m backend.app.migrate` as
`stranger_club_migrator` *before* the grants at the bottom of the script
(they need the tables to already exist).

- `stranger_club_migrator` — DDL, schema owner. Used only for migrations.
  Never the running application's `DATABASE_URL`.
- `stranger_club_app` — the running application's `DATABASE_URL`. No
  `CREATE`/`DROP`/`ALTER`.
- `stranger_club_backup` — read-only, used only by `pg_dump`.

### Storage credentials

Runtime `SC_STORAGE_*` credential: IAM-scoped (PROVIDER STEP, on your real
provider's console/API) to `GetObject`/`PutObject` only on the relevant
prefixes — **no** `DeleteObject`. A separate, more-privileged
`SC_STORAGE_ADMIN_*` credential is used only for the manual scripts
(`reconcile_storage.py`, `configure_bucket_protection.py`).

### Trusted proxy / TLS / request limits

- TLS terminates at your proxy/load balancer — the application itself
  speaks plain HTTP.
- `SC_TRUSTED_PROXY_IPS` must be your proxy's *real* IP(s)/CIDR(s) — never
  a placeholder or wildcard. Without it correctly set, forwarded headers
  are never trusted (safe default), and `SC_ENV=production` refuses to
  start at all without it set to *something*.
- Add a request body size cap at the proxy (e.g. `client_max_body_size 6m;`
  in Nginx) ahead of the app's own 5MB/2MB upload limits —
  PROVIDER/OPERATOR STEP, not something the app enforces.
- Request timeouts: handled at the proxy layer — PROVIDER/OPERATOR STEP.

### Session/cookie requirements

`SC_COOKIE_SECURE` defaults to **on** for any `SC_ENV` other than
`development` — leave it on in production; only ever override to `false`
for a specific debugging need, never as a standing production setting.

### `SC_AUTO_MIGRATE`

Leave `true` for a single instance. Set `false` once you run more than one
application instance, and run `python -m backend.app.migrate` as an
explicit step in your deploy pipeline instead (§18) — otherwise two
instances starting at once could both attempt to migrate simultaneously
(safe, thanks to the advisory lock, but an explicit step is simpler to
reason about and to gate a deploy on).

---

## 18. Production deployment order

Each step is labeled **[IMPLEMENTATION]** (something this repository's code
already does for you) or **[PROVIDER/OPERATOR]** (something you must do
against real infrastructure this repository cannot provision for you).

1. **[PROVIDER/OPERATOR]** Provision a managed PostgreSQL instance (16+).
2. **[PROVIDER/OPERATOR]** Provision the three database roles —
   `scripts/provision_database_roles.sql` (§17).
3. **[PROVIDER/OPERATOR]** Provision a private S3-compatible bucket.
4. **[IMPLEMENTATION, run by you]** Enable and verify bucket versioning:
   `python scripts/configure_bucket_protection.py --bucket <bucket> --enable`
   — confirm the printed result is `VERIFIED_ENABLED`, not merely "no
   error" (see `docs/storage.md`).
5. **[PROVIDER/OPERATOR]** Configure secrets (database URLs, storage
   credentials, `SC_ADMIN_PASSWORD`, `SC_TRUSTED_PROXY_IPS`) in your
   deployment platform's secret store.
6. **[PROVIDER/OPERATOR]** Deploy the application image
   (`docker build -t stranger-club .` — §16 — pushed to your registry, or
   built directly by your platform from `Dockerfile`). On Render
   specifically: create a Docker Web Service pointed at this repository —
   Render builds `Dockerfile` itself, no registry push needed.
7. **[IMPLEMENTATION, run by you]** Run migrations as the explicit release
   step: `DATABASE_URL=<migrator-role-url> python -m backend.app.migrate`.
8. **[IMPLEMENTATION]** Verify `/health` returns `200`.
9. **[IMPLEMENTATION]** Verify `/ready` returns `200` (confirms DB
   reachability *and* the correct Alembic head).
10. **[IMPLEMENTATION, run by you]** Verify storage: upload a test payment
    proof through the real UI and confirm it's retrievable.
11. **[IMPLEMENTATION, run by you]** Verify authentication: log in as the
    organizer created from `SC_ADMIN_PASSWORD`.
12. **[IMPLEMENTATION, run by you]** Verify the registration/payment
    workflow end to end — the same steps as [§14](#14-first-end-to-end-test),
    against the real deployment.
13. **[IMPLEMENTATION, run by you]** Verify teams/matches/results the same
    way.
14. **[PROVIDER/OPERATOR]** Configure monitoring/alerting on your platform
    (this repository provides structured logs + correlation IDs +
    `/internal/diagnostics`, not a metrics pipeline — see
    `docs/observability.md`).
15. **[PROVIDER/OPERATOR]** Review, commit, and configure secrets for
    `.github/workflows/backup.yml` to actually enable scheduled backups
    (§19 — it does nothing until you do this).
16. **[IMPLEMENTATION, run by you]** Perform a restore drill —
    `scripts/restore_test.py` — before considering backups "done" (§20).

---

## 19. Backups

Two independent mechanisms — see `docs/backups.md` for full detail.

1. **Managed PostgreSQL point-in-time recovery** — a property of your
   chosen provider/plan. **[PROVIDER/OPERATOR]**: verify your specific
   tier actually includes PITR before relying on it.
2. **Independent `pg_dump` archives** — `.github/workflows/backup.yml`.
   **This workflow is a reviewed, tested template that is NOT currently
   active.** It only starts running once you (a) review it, (b) commit it
   to the repository's default branch, and (c) add every secret it
   references under *Settings → Secrets and variables → Actions*. Until
   then, its presence in this repository means nothing is scheduled.

The dump command it runs, using the read-only backup role (§17):

```bash
pg_dump --dbname="$BACKUP_DATABASE_URL" -Fc -f "stranger-club-$(date -u +%Y-%m-%dT%H-%M-%SZ).dump"
```

`-Fc` (custom format): compressed, supports selective table restore.

Backup credential separation: the backup role can only read; the object
storage credential the workflow uploads with is scoped only to the
`backups/postgres/` prefix and must not have delete permission there
either — retention/expiry is a deliberate lifecycle-policy decision, never
something a routine backup run performs itself.

**A backup succeeding proves nothing about whether it can be restored** —
see [§20](#20-restore--disaster-recovery).

---

## 20. Restore / disaster recovery

Run the automated restore test — this performs the full documented
procedure against a disposable target database and prints a **measured**
RTO:

```bash
python scripts/restore_test.py \
  --backup-file /path/to/backup.dump \
  --target-url postgresql+psycopg://user:pass@scratch-host/db \
  --storage-bucket <bucket> --storage-endpoint-url <url> \
  --admin-access-key-id <admin-key> --admin-secret-access-key <admin-secret>
```

Required inputs: a real `pg_dump` custom-format backup file, and a
connection URL to an **empty** target PostgreSQL database (the script
refuses to run against a target that already has tables — this is a
restore-into-a-fresh-instance test, never a merge). The `--storage-*`
arguments are optional but needed to also verify payment-proof objects
(next paragraph).

What it does and what "success" looks like:

1. `pg_restore` into the empty target.
2. **Alembic head validation** — a dedicated, explicit check
   (`validate_alembic_head`), not merely inferred from `/ready` (which
   would otherwise be silently "fixed" by auto-migrate before you could
   observe a genuine mismatch). Prints the actual vs. expected revision.
3. Schema/foreign-key/product-invariant validation (the same checks
   `scripts/migrate_sqlite_to_postgres.py` uses).
4. Application-level verification: starts the app against the restored
   database and hits `/ready` and a handful of real endpoints.
5. If storage arguments were given: delegates to `reconcile_storage.py` to
   confirm every `PaymentProof`/QR `storage_key` referenced in the
   restored database still resolves to a real object.

A clean run prints the measured restore time and reports no failed check.
Record that number in `docs/backups.md`'s RTO row — **do not** treat the
RPO/RTO targets there as met without actually running this and reading the
real output.

**Object storage recovery**: PostgreSQL restore alone is not sufficient —
proof screenshots live in object storage, not the database. After *any*
database restore, always separately run:

```bash
python scripts/reconcile_storage.py --database-url <restored-db-url> --bucket <bucket>
```

A non-empty `missing_from_storage` list means evidence is genuinely
unavailable and must be investigated immediately — see
`docs/disaster-recovery.md` scenario F. A restore is not "done" until this
report is clean.

---

## 21. SQLite → PostgreSQL migration

One-time data migration when cutting an existing SQLite deployment over to
PostgreSQL. This is a row-by-row copy, not a schema transformation — the
destination must already be at the current Alembic head first:

```bash
python -m backend.app.migrate   # against the destination PostgreSQL, first
```

Then, with the source application **stopped** (this script assumes a
static SQLite file for the whole run — see the script's own docstring,
reproduced by `--help`):

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --sqlite-path data/stranger_club.db \
  --postgres-url postgresql+psycopg://user:pass@host/db \
  [--report-path migration_report.json] \
  [--dry-run] \
  [--copy-objects-from data/uploads]
```

- **Prerequisites**: destination already migrated to head (above); source
  application stopped for the duration.
- **`--dry-run`**: runs the full copy and validation inside a transaction,
  then rolls back instead of committing — rehearse a cutover against a
  disposable destination first.
- **`--copy-objects-from <uploads dir>`**: also copies local proof/QR
  binaries into the destination S3 bucket (reads `SC_STORAGE_*` for the
  destination). Omit it and the row data still migrates; run
  `scripts/reconcile_storage.py` afterward either way to confirm every
  referenced key resolves.
- **Row-count / FK / invariant validation**: built into the script — it
  validates before committing, inside the same transaction, so any
  failure rolls the *entire* copy back, leaving the destination exactly as
  it was before the run (empty, for a fresh cutover). Nothing is left
  half-migrated.
- **Sequence validation**: primary keys are copied verbatim; each table's
  auto-increment sequence is advanced past the highest copied id
  afterward, so the running application's next `INSERT` gets a
  non-colliding id.
- **Not migrated** (by design — see the script's docstring):
  `rate_limit_buckets` (transient counters) and
  `alembic_version`/`schema_migrations` (the destination already has its
  own correct bookkeeping from having run migrations itself).
- **Cutover considerations**: run during an explicit maintenance window;
  point `DATABASE_URL`/`SC_STORAGE_*` at the new infrastructure and start
  the application only after this script and `reconcile_storage.py` both
  report clean.
- **Rollback considerations**: since the entire copy is one transaction, a
  failed run needs no cleanup on the PostgreSQL side. Rolling back the
  *cutover itself* (after a successful copy, if you decide to revert) means
  pointing the application back at the original SQLite file — anything
  written only to PostgreSQL after cutover would need to be exported back
  manually; this is not a normal or expected path.

---

## 22. Routine operations

**Check health / readiness:**

```bash
curl https://<host>/health
curl https://<host>/ready
```

**Inspect operational internals** (requires a `PLATFORM_ADMIN` organizer
session cookie — see [§13](#13-first-adminorganizer-setup)):

```bash
curl -b <organizer-session-cookie> https://<host>/internal/diagnostics
```

Returns DB pool stats, realtime backend type + active topic/subscriber
counts, and which storage backend is active — never connection strings,
credentials, or user/payment data.

**Inspect logs**: `docker logs -f <container>` (Docker) or your platform's
log aggregation — every line carries a `request_id` (see
`docs/observability.md`).

**Inspect migration state**: `psql "$DATABASE_URL" -c "SELECT version_num FROM alembic_version;"`
(§11).

**Restart application**: platform-dependent; instances are stateless, so a
normal restart is always safe (no drain procedure needed beyond what your
platform already does for a deploy).

**Restart database / storage**: provider-dependent — see
`docs/disaster-recovery.md` scenarios B/C for what the application does
automatically when either restarts (reconnects, resyncs realtime clients).

**Rotate secrets / DB credentials**: provision the new credential alongside
the old one, update the environment variable, roll instances one at a time
(zero-downtime with 2+ instances), verify the new credential works, *then*
revoke the old one — see `docs/disaster-recovery.md` scenario H.

**Verify proxy configuration**: confirm the application is unreachable
except through your chosen proxy, and that a forged `X-Forwarded-For` sent
*directly* to the application (bypassing the proxy) is not trusted — see
[§17](#17-production-configuration).

---

## 23. Troubleshooting

### Docker Compose (§2 — most local development)

| Problem | Likely cause | Diagnostic | Fix |
|---|---|---|---|
| `Cannot connect to the Docker daemon` | Docker Desktop/daemon not running | `docker info` | Start Docker Desktop (or your Docker daemon), then retry |
| `Bind for 0.0.0.0:8000 failed: port is already allocated` (or 5432/9000/9001) | Something else on your machine already uses that host port | `lsof -i :8000` (or the relevant port) | Stop the other process, or set `APP_HOST_PORT`/`POSTGRES_HOST_PORT`/`MINIO_API_HOST_PORT`/`MINIO_CONSOLE_HOST_PORT` in a `.env` file (§2, `.env.example`) to an unused port — the app still talks to `postgres:5432`/`minio:9000` internally regardless of the host mapping |
| `postgres` never becomes healthy | Corrupted/incompatible data in the named volume, or the container crashed | `docker compose logs postgres` | `docker compose down -v` for a full reset (destroys local data — see below), then `docker compose up --build` |
| `minio` never becomes healthy | Same as above, MinIO side | `docker compose logs minio` | Same: `docker compose down -v` then `docker compose up --build` |
| `migrate` service exits non-zero | A real migration error (bad `DATABASE_URL`, a broken migration) — `app` will never start, by design, since it depends on `migrate` completing successfully | `docker compose logs migrate` | Fix the underlying issue; `docker compose up --build` re-runs `migrate` (it's idempotent — already-applied migrations are a no-op) |
| `minio-init` service exits non-zero | MinIO unreachable, or bad `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` — `mc alias set` or `mc mb` failed | `docker compose logs minio-init` | Confirm `minio` is healthy first; fix credentials if you overrode them in `.env`; re-run `docker compose up --build` |
| `app` never becomes healthy | Usually one of the above still resolving, or a genuine startup error in the app itself | `docker compose ps`; `docker compose logs app` | Check `postgres`/`migrate`/`minio-init` are all healthy/exited-0 first, then read the app's own log for the real error |
| `curl http://localhost:8000/ready` returns `503` even though `docker compose ps` shows `app` as healthy | `/health` (what Docker's `HEALTHCHECK` polls) only proves the process is alive — `/ready` separately checks DB connectivity and the Alembic head; a `503` here after `migrate` already completed suggests the app started before migrate's result was visible, or an unrelated DB connectivity issue | `docker compose logs app`; `docker compose logs migrate` | Should not happen given `migrate: condition: service_completed_successfully` — if it does, `docker compose restart app` and check logs for the actual `readiness_failed reason=...` |
| Code changes don't seem to take effect | Compose reused a stale built image | — | `docker compose up --build` (the `--build` flag is what forces a rebuild — a bare `docker compose up` reuses whatever image already exists) |
| Old/stale containers or volumes causing confusing behavior | Leftover state from a previous run | `docker compose ps -a`; `docker volume ls \| grep stranger_club` | `docker compose down` (keeps volumes/data) or `docker compose down -v` (destroys `stranger_club_postgres_data`/`stranger_club_minio_data` — full reset, local dev data only, see [§24](#24-clean-reset--local-restart)) |
| Want to completely start over | Any of the above, or just want a clean slate | — | `docker compose down -v` then `docker compose up --build` — see [§24](#24-clean-reset--local-restart) |

### Native/advanced path (§4–§10) and production

| Problem | Likely cause | Diagnostic | Fix |
|---|---|---|---|
| Backend won't start: `RuntimeError: No organizer account exists and SC_ADMIN_PASSWORD is not set` | First run, no organizer yet, password unset | — | Set `SC_ADMIN_PASSWORD` |
| Backend won't start: `ConfigError: SC_ENV=... requires a PostgreSQL DATABASE_URL` | `SC_ENV` is `staging`/`production` with a SQLite/unset `DATABASE_URL` | `echo $DATABASE_URL $SC_ENV` | Set a real PostgreSQL `DATABASE_URL`, or use `SC_ENV=development` locally |
| Backend won't start: `ConfigError: SC_ENV=production requires SC_TRUSTED_PROXY_IPS` | Deploying with `SC_ENV=production` but no proxy IP configured | — | Set `SC_TRUSTED_PROXY_IPS` to your real proxy's IP(s) |
| Frontend won't start | `node_modules` missing/stale, or wrong Node version | `node --version`; `npm ci` | Reinstall with `npm ci` on Node 22 |
| `sqlalchemy.exc.OperationalError: connection refused` | Postgres container not running / wrong host-port | `docker ps`; `docker exec <pg> pg_isready` | Start/fix the container (§9); check `DATABASE_URL` matches the mapped port |
| `python -m backend.app.migrate` fails with `no such table: matches` on SQLite | Ran it against a SQLite file that doesn't exist yet | — | Don't — start the application instead for a fresh SQLite DB (§11) |
| Bare `alembic current`/`upgrade` fails with `KeyError: 'url'` | `alembic.ini` has no `sqlalchemy.url` — expected in this repo | — | Use `python -m backend.app.migrate`, or the `Config.set_main_option` snippet in §11 |
| `/ready` returns 503, `readiness_failed reason=alembic_head_mismatch` | New code deployed before its migration ran, or a migration partially failed | `psql "$DATABASE_URL" -c "SELECT version_num FROM alembic_version;"` vs. `ALEMBIC_EXPECTED_HEAD` in `main.py` | Run the pending migration (§11); see `docs/disaster-recovery.md` scenario G |
| `/ready` returns 503, `readiness_failed reason=database_unavailable` | DB unreachable/credentials wrong | Check connectivity directly with `psql` | Fix connectivity/credentials |
| Migration hangs | Another process/instance holds the advisory lock | Check for a concurrent migration run | Wait — it self-releases if the holder disconnects; never manually kill the DB connection unless certain no legitimate migration is in progress |
| Storage upload fails, `503 STORAGE_UNAVAILABLE` | S3-compatible endpoint unreachable, wrong credentials/bucket | Check `SC_STORAGE_*`; `curl` the endpoint directly | Fix endpoint/credentials/bucket; see `storage_put_failed` in logs |
| Presigned URL failure / object not found | Object genuinely missing, or wrong bucket/prefix | `python scripts/reconcile_storage.py --database-url ... --bucket ...` | Investigate immediately if `missing_from_storage` is non-empty — see `docs/disaster-recovery.md` scenario F |
| `401` on organizer/player endpoints | No/expired session cookie | Check cookies are being sent (`credentials: same-origin` in the frontend's own fetch wrapper); re-login | Log in again |
| `403 CSRF_INVALID` | Missing/stale `X-CSRF-Token` header, usually a stale frontend tab | `csrf_invalid` in logs | Reload the frontend tab to fetch a fresh CSRF token |
| `429` rate-limited | Real rate limit hit, or (rare) `rate_limit_backend_unavailable` (fails closed) | Check logs for `rate_limit_backend_unavailable` | If the backend itself is down, fix DB connectivity — rate limiting fails closed by design |
| Docker `HEALTHCHECK` failing | App not listening yet, or crashed | `docker logs <container>`; `docker inspect --format '{{json .State.Health}}' <container>` | Check startup logs for the real error |
| Permission denied errors from `stranger_club_app` role | Attempting DDL with the app role, or role misconfigured | Which role is `DATABASE_URL` using? | The app role deliberately cannot `CREATE`/`DROP`/`ALTER` — use the migrator role for schema changes |
| Forwarded headers not trusted / wrong client IP seen | `SC_TRUSTED_PROXY_IPS` unset or doesn't match the real proxy | Check the entrypoint's uvicorn args (`docker exec <c> ps aux`) | Set `SC_TRUSTED_PROXY_IPS` to the proxy's actual peer IP/CIDR |
| Tests skipped unexpectedly | An `SC_TEST_*` variable is unset | `pytest -q -rs` | Set the relevant variable (§15) if you meant to run that suite |
| PostgreSQL-specific test failure only | Real Postgres-only behavior (locking, composite FK, JSONB) — genuinely different from SQLite | Re-run in isolation: `pytest tests/test_x.py -k name -q` against `SC_TEST_DATABASE_URL` | Investigate the specific assertion; don't assume it's environmental without isolating it |
| Backup workflow failing in GitHub Actions | Dump under the size floor, or upload/verify step failed | Read the failed step's log — it prints the dump size | Investigate the database/credentials for that step; a red run is the intended signal |
| Restore fails validation | Backup itself is bad, or target wasn't empty | `scripts/restore_test.py`'s own printed report | Re-run against a genuinely empty target; investigate the specific failed check |

---

## 24. Clean reset / local restart

Every command below is **local development only** — none of it is ever
appropriate against a production database, bucket, or deployment. Read the
label on each block before running it.

### Docker Compose (§2 — most local development)

```bash
docker compose down          # stop containers, keep stranger_club_postgres_data / stranger_club_minio_data
docker compose up -d         # restart with existing data intact
```

```bash
docker compose down -v       # stop containers AND delete stranger_club_postgres_data / stranger_club_minio_data
docker compose up --build    # fresh Postgres + fresh (empty) bucket, migrations re-run from scratch
```

`down -v` is a **local-development-only, destructive** command — it
deletes both named volumes entirely. It is never appropriate against
anything with a real hostname or managed-provider connection, and it has
no equivalent meaning in production (there is no `docker-compose.yml`
there — see [§17](#17-production-configuration)).

```bash
docker compose logs -f app       # tail one service's logs
docker compose logs -f postgres
docker compose logs -f minio
docker compose ps                # see what's running/healthy/exited
```

### Native/advanced path (§4–§10)

**Reset application code** (discard local uncommitted changes — be certain
before running):

```bash
git status        # look first
git stash -u       # or: git checkout -- <specific files>
```

**Reset SQLite** (safe — `data/` is gitignored, dev-only):

```bash
rm -rf data
```

Next backend start recreates it fresh.

**Reset local PostgreSQL data** — LOCAL DEVELOPMENT CONTAINER ONLY, never
run against anything with a real hostname/managed-provider URL:

```bash
docker rm -f stranger-club-postgres
# then re-run the docker run command from §9
```

**Reset local MinIO data** — LOCAL DEVELOPMENT CONTAINER ONLY:

```bash
docker rm -f stranger-club-minio
# then re-run the docker run + bucket-creation commands from §10
```

**Reset all local Docker containers for this project**:

```bash
docker rm -f stranger-club-app stranger-club-postgres stranger-club-minio 2>/dev/null
docker network rm stranger-club-net 2>/dev/null
```

None of the commands in this section can reach a production database or
bucket — they only ever address container names/paths you created
yourself while following this document.

---

## 25. Security checklist

Before any production deployment:

- [ ] Every required production environment variable is set (§7) — no
      value copied from a local/dev example
- [ ] No development fallback credentials anywhere in production
      configuration (check `SC_ADMIN_PASSWORD`, storage credentials)
- [ ] `SC_AUTO_MIGRATE=false` once running more than one instance
- [ ] Migration role (`stranger_club_migrator`) is separate from the
      running application's role
- [ ] Application database role (`stranger_club_app`) verified to have no
      `CREATE`/`DROP`/`ALTER` privilege
- [ ] Object storage bucket is private (no public read)
- [ ] Bucket versioning enabled and **verified** via
      `configure_bucket_protection.py --enable` (`VERIFIED_ENABLED`, not
      just "no error")
- [ ] `SC_TRUSTED_PROXY_IPS` set to the real proxy's IP(s)/CIDR(s) — never
      a placeholder
- [ ] TLS terminated at the proxy; HTTP → HTTPS redirect enforced there
- [ ] Application port not directly internet-reachable — only the proxy is
- [ ] `SC_COOKIE_SECURE` on (the default outside `development`)
- [ ] `.github/workflows/backup.yml` reviewed, committed, and its secrets
      configured — or an equivalent backup mechanism is genuinely running
- [ ] A restore has actually been tested with `scripts/restore_test.py`
      against this deployment's real backup, not assumed
- [ ] `/ready` passing
- [ ] `psql "$DATABASE_URL" -c "SELECT version_num FROM alembic_version;"`
      matches `ALEMBIC_EXPECTED_HEAD` in `backend/app/main.py`
- [ ] Logs reviewed to confirm no secrets/PII appear (see
      `docs/observability.md` for what should never be logged)

---

## 26. Release checklist

Before every production deployment:

- [ ] `pytest -q` green (SQLite)
- [ ] `pytest -q` green against real PostgreSQL (`SC_TEST_DATABASE_URL`)
- [ ] New/changed migrations reviewed (up **and** down, where a down
      exists) — see [§11](#11-database-migrations)
- [ ] `npm run build` succeeds
- [ ] `npm audit` reviewed (informational in CI — `continue-on-error`, but
      still worth reading)
- [ ] `docker build -t stranger-club .` succeeds
- [ ] Image tag/version identified and recorded
- [ ] Migration executed against production as an explicit step (§18)
- [ ] `/ready` healthy post-deploy
- [ ] Smoke test performed against the real deployment (§14's steps, or a
      subset)
- [ ] Logs checked for unexpected errors immediately after deploy
- [ ] Backup status checked (last successful run, if the backup workflow is
      active)
