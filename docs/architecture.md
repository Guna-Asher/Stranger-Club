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

## Phase 4: Events, Teams, and Matches

Once a registration is `CONFIRMED`, an organizer can put players into Teams
and schedule Matches between them:

```
Event (Match model) -> Registrations -> Teams -> TeamMembership -> Matches (Fixture model)
```

**A naming note, since it looks confusing in the code otherwise**: the
existing `Match` SQLAlchemy class predates this phase and is actually the
*Event* entity (name, date, venue, capacity, fee — a cricket gathering). The
new "scheduled game between two teams" concept is internally named `Fixture`
to avoid colliding with it. Every user-facing string in the product still
says "Match"/"Matches" — this is a code-only naming choice, confined to
`models.py`, `services.py`, and `routers/fixtures.py`.

- **Team**: belongs to exactly one event. Deliberately minimal — no logos,
  sponsors, ranking points, or player ratings.
- **TeamMember**: links one `Registration` to one `Team`, both scoped to the
  same event. Keyed to `Registration`, not the global/mutable
  `PlayerProfile` — a `Registration` is already event-scoped and immutable
  in the sense that matters (cancelling one never reactivates or rewrites
  it; a new attempt is a brand-new row). This is what keeps team history
  independent of a player later editing their profile. Only a `CONFIRMED`
  registration is eligible; a registration cancellation cascades to remove
  any team membership. Once a team has played (any fixture reaches
  `IN_PROGRESS`/`COMPLETED`), its roster freezes — see `database.md`'s
  schema-invariants table.
- **Fixture** (UI: "Match"): a scheduled game between two teams of the same
  event. Lifecycle `SCHEDULED -> IN_PROGRESS -> COMPLETED`, or
  `-> CANCELLED` from either open state — forward-only, same reasoning as
  the Event's own `VALID_EVENT_TRANSITIONS`. No live scoring, innings, or
  result fields — that's a later phase (see below).
- **Cross-event integrity** (a team from Event 1 appearing in Event 2's
  match, or a registration from Event 2 becoming a member of Event 1's
  team) is enforced by composite foreign keys at the database level, not
  only application checks — see `database.md`.
- **Realtime**: reuses the exact Phase 3 `PostgresBroadcaster`/
  `InProcessBroadcaster` mechanism, publishing `TEAM_CREATED`,
  `TEAM_UPDATED`, `TEAM_REMOVED`, `TEAM_MEMBER_ASSIGNED`,
  `TEAM_MEMBER_MOVED`, `TEAM_MEMBER_REMOVED`, `FIXTURE_CREATED`,
  `FIXTURE_UPDATED`, `FIXTURE_STATUS_CHANGED` — no new realtime mechanism.

**Forward compatibility, not overbuilding**: no Result, MVP, or Media table
exists yet (a later phase). `Fixture.id` is a stable, never-reused, never-
deleted identity a future `Result`/`MatchMedia` row can reference without
any redesign here; the roster-lock rule above is what makes "who was
actually on the team when it played" a trustworthy fact by the time such a
phase needs to read it, without a per-fixture roster snapshot table that
nothing needs yet.

## Phase 5: post-match Results, Awards, and Participation

Once a match (Fixture) reaches `COMPLETED`, an organizer can manually record
what happened — never computed automatically:

```
Fixture (COMPLETED) -> MatchParticipant ("who actually played, for which team") -> MatchResult (winner/MVP/Best Batter/Best Bowler/notes)
```

Extending the Phase 4 chain: `Event -> Registration -> TeamMember -> Fixture
-> MatchParticipant -> MatchResult`.

- **MatchParticipant**: a lightweight per-fixture roster fact, deliberately
  smaller than a real scoring system — no innings, substitutions, bench, or
  stats. Its existence for a given (fixture, registration) pair *is* "this
  player played this match."
- **MatchResult**: one per fixture (`UNIQUE(fixture_id)`), organizer-entered
  only — `result_type` (`TEAM_A_WIN`/`TEAM_B_WIN`/`DRAW`/`NO_RESULT`), an
  optional winner, and three optional awards (MVP, Best Batter, Best
  Bowler). Corrections remain allowed indefinitely — there is no "locked"
  state; `finalized_at`/`finalized_by_organizer_id` are informational only,
  the same role `Payment.verified_at` already plays elsewhere.
- **Award eligibility is a database fact, not just a Python check**: each
  award field is a composite foreign key into `match_participants`
  (`(award_registration_id, fixture_id) -> match_participants(registration_id,
  fixture_id)`), not directly into `registrations`. Since SQL's standard
  multi-column FK semantics skip the check when any referencing column is
  NULL, an unset award is unconstrained — but a *set* one can only ever name
  someone with a real participation row for this exact fixture. See
  `database.md` for the full composite-FK chain, including the new
  `team_members(registration_id, team_id)` target that proves a participant
  was genuinely assigned to the team they're recorded as playing for.
- **Winner consistency is also a same-row database `CHECK`**, not only
  application logic: `MatchResult` snapshots `team_a_id`/`team_b_id` from
  the fixture at creation time specifically so "a `TEAM_A_WIN` must have
  `winning_team_id = team_a_id`" and "a draw/no-result must have a NULL
  winner" can be expressed as one `CHECK` constraint.
- **Realtime**: `MATCH_RESULT_CREATED`, `MATCH_RESULT_UPDATED`,
  `MATCH_PARTICIPATION_UPDATED` — same broadcaster, no new mechanism.
- **Explicitly not built**: Media Hub, photo/media uploads, Instagram/Reels/
  CricHeroes integration, live/ball-by-ball scoring, scorecards, rankings,
  Elo, leaderboards, AI analytics. Those remain later, separate phases.

### A concurrency-correctness finding from this phase, fixed on the spot

Investigating pre-existing intermittent test flakiness in the payment
domain (unrelated to this phase's new tables) surfaced two real bugs in
`services.py`, both fixed and re-verified against real PostgreSQL:

1. **Identity-map staleness**: `submit_payment_proof` (and several other
   "load, lock, reload" call sites, including this session's own Phase 4
   team-assignment code) re-queried an object already present in the
   session's identity map, expecting fresh post-lock data. With
   `expire_on_commit=False`, SQLAlchemy does not refresh an already-loaded
   object's columns or relationship collections on a plain re-`select()` —
   only `populate_existing=True` does. Fixed everywhere this pattern
   appears.
2. **Wrong lock target**: `review_payment`/`promote_waitlisted` locked the
   individual `Payment`/`Registration` row being acted on, not the `Match`
   the capacity decision is actually about — letting two different payments
   for the same nearly-full event both read the same under-capacity
   snapshot and both confirm. Fixed by locking `Match` first, matching
   `create_registration`'s already-correct precedent.

Both were reproduced deterministically before the fix and could not be
reproduced afterward (25/25 and 6/6 clean runs respectively) — see the
Phase 5 delivery report for the full evidence.
