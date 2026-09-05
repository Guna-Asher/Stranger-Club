# Deployment

## Environment variables

### Required in staging/production

| Variable | Purpose |
|---|---|
| `SC_ENV` | `production` or `staging` — gates every fail-fast check below |
| `DATABASE_URL` | `postgresql+psycopg://user:pass@host/db` — must be PostgreSQL |
| `SC_ADMIN_PASSWORD` | Bootstrap organizer password (only used the first time; fails loudly if unset and no organizer exists yet) |
| `SC_STORAGE_BACKEND=s3` | Local filesystem storage is refused outside development |
| `SC_STORAGE_BUCKET`, `SC_STORAGE_ENDPOINT_URL`, `SC_STORAGE_ACCESS_KEY_ID`, `SC_STORAGE_SECRET_ACCESS_KEY` | Object storage credentials |
| `SC_TRUSTED_PROXY_IPS` | Comma-separated IP(s)/CIDR(s) of the reverse proxy — see `reverse-proxy` below |

### Optional

| Variable | Default | Purpose |
|---|---|---|
| `SC_STORAGE_REGION` | `auto` | |
| `SC_AUTO_MIGRATE` | `true` | Set `false` once multi-instance — migrations become an explicit release step |
| `SC_DB_POOL_SIZE` / `SC_DB_MAX_OVERFLOW` | `5` / `5` | See `database.md`'s sizing formula |
| `SC_COOKIE_SECURE` | on in staging/production, off in dev | Force override either way |
| `SC_ADMIN_USERNAME` | `organizer` | |
| `SC_SESSION_HOURS` / `SC_PLAYER_SESSION_DAYS` | `12` / `30` | |
| `SENTRY_DSN` | unset | Optional error tracking — only initializes if set |

Never place any of the above secrets in source, Docker layers, or frontend
assets. `backend/app/config.py` fails fast with a specific message for
every missing required variable — it never silently falls back to an
insecure default.

## Migration as a release step

```bash
# Once, before starting new instances against a schema change:
DATABASE_URL=postgresql+psycopg://stranger_club_migrator:...@host/db \
  python -m backend.app.migrate
```

Set `SC_AUTO_MIGRATE=false` for the actual running application instances
once you have more than one — see `database.md` for why (advisory-lock
protected, but an explicit step is simpler to reason about and to gate a
deploy pipeline on than "whichever instance boots first").

## Docker

```bash
docker build -t stranger-club .
docker run -d \
  --read-only --tmpfs /tmp \
  -e SC_ENV=production \
  -e DATABASE_URL=... -e SC_ADMIN_PASSWORD=... \
  -e SC_STORAGE_BACKEND=s3 -e SC_STORAGE_BUCKET=... -e SC_STORAGE_ENDPOINT_URL=... \
  -e SC_STORAGE_ACCESS_KEY_ID=... -e SC_STORAGE_SECRET_ACCESS_KEY=... \
  -e SC_TRUSTED_PROXY_IPS=<your-proxy-ip> \
  -p 8000:8000 stranger-club
```

**VERIFIED LOCALLY**: the image runs as a non-root user (`stranger_club`),
passes its `HEALTHCHECK`, and serves the full registration →
payment-proof-upload → organizer-review flow with **no writable filesystem
at all** (`--read-only --tmpfs /tmp`) when using a real PostgreSQL
container and a real S3-compatible (MinIO) container for storage — the
only filesystem writes this application ever needs (`SC_DATA_DIR` for
SQLite/local storage) are development-only. **Not verified against a real
managed PostgreSQL provider or a real R2/S3 account** — only against
disposable local containers, since no production accounts exist in this
environment (**IMPLEMENTED BUT REQUIRES REAL PROVIDER CONFIGURATION** to
confirm end-to-end against the actual chosen providers).

`SC_TRUSTED_PROXY_IPS` is consumed by `docker-entrypoint.sh`, which passes
`--proxy-headers --forwarded-allow-ips=<value>` to uvicorn when set —
without it, forwarded headers are never trusted (see `reverse-proxy`
below).

## Rollback

- **Application code**: redeploy the previous image tag. Stateless
  instances — no special drain procedure beyond what the platform already
  does for a normal deploy.
- **Migrations**: `0006`, `0007`, `0008`, and `0009` all have real, tested
  `downgrade()` implementations. `0001`–`0005` have downgrades except `0005` (a one-way
  data migration, documented in its own file — reconstructing the old
  schema from a redesigned one would itself be lossy). Roll back a bad
  migration with `alembic downgrade <revision>` using the same
  `stranger_club_migrator` credential, or restore from backup if the
  migration already ran destructively.
- **Database engine**: the SQLite code path remains fully functional in
  the codebase (dialect-detected, not removed) — a single-instance
  emergency rollback to SQLite is a configuration change, though any data
  written only to PostgreSQL since cutover would need to be exported back
  (not a normal or recommended path; documented only for completeness).
- **Storage backend**: `SC_STORAGE_BACKEND=local` is a fully supported
  configuration value — rolling back from S3 is a config change, though
  objects already written only to S3 would need `scripts/migrate_sqlite_to_postgres.py`'s
  `copy_local_objects_to_s3` pattern run in reverse if a full storage
  rollback were ever genuinely required.

## Reverse proxy / trust boundary

- TLS terminates at the proxy/LB.
- HTTP → HTTPS redirect enforced at the proxy.
- `SC_COOKIE_SECURE` (on by default outside `SC_ENV=development`) sets the
  `Secure` flag on both session cookies. **VERIFIED LOCALLY** (unit test).
- `SC_TRUSTED_PROXY_IPS` → `docker-entrypoint.sh` translates it into
  uvicorn's `--proxy-headers --forwarded-allow-ips=<value>` — the
  application only trusts `X-Forwarded-For`/`X-Forwarded-Proto` from a
  connection whose direct TCP peer is this configured proxy IP/CIDR.
  Without this set correctly (or with it unset), forwarded headers are
  never trusted at all and `request.client.host` is the direct TCP peer —
  the safe default. `SC_ENV=production` refuses to start without this set
  (`backend/app/config.py`).
  - **VERIFIED LOCALLY**: the entrypoint script's own logic — that it adds
    the flag with the exact configured value(s) when the variable is set,
    and adds no forwarding trust at all when it's unset — is covered by an
    automated test (`test_entrypoint_trusts_*` in
    `tests/test_phase3_infrastructure.py`) that captures the real arguments
    the script would hand to uvicorn, without starting a real server.
  - **DEPLOYMENT-TIME VERIFICATION REQUIRED**: that the *actual* production
    load balancer/proxy (whichever is chosen) sends requests from the exact
    IP(s)/CIDR(s) configured in `SC_TRUSTED_PROXY_IPS`, and that no other
    path to the application exists that bypasses it (e.g. the application
    port is not directly internet-reachable — only the proxy is). This
    cannot be verified in this environment, since no real production
    proxy/LB exists here. Before going live: confirm the application is
    unreachable except through the chosen proxy, and that a request with a
    forged `X-Forwarded-For` sent *directly* to the application (bypassing
    the proxy) does not get trusted — the fact that direct access should be
    network-blocked in the first place is itself a deployment configuration
    requirement, not something this application's code can enforce.
  - Development/local defaults (`SC_TRUSTED_PROXY_IPS` unset) must always be
    replaced with the real proxy's address(es) before deploying — never
    reuse a placeholder or wildcard value.
- Add a request body size cap at the proxy (e.g. Nginx
  `client_max_body_size 6m;`) ahead of the application's own 5MB/2MB upload
  validation — defense in depth against many concurrent large requests
  before the app-level check rejects any single one. **DEPLOYMENT-TIME
  VERIFICATION** (proxy-specific configuration, not exercised here).
- Request timeouts are handled at the proxy layer (e.g. 30s) — no
  additional application middleware. **DEPLOYMENT-TIME VERIFICATION.**
