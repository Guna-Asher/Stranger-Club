Yes. Since **v1.0.0-mvp is already verified**, I would now make the README much cleaner and make **Docker the primary local testing path**.

Your current README mixes architecture, security, API documentation, development setup, and deployment. For this release, I'd simplify it around:

> **Clone → Build → Run → Test → Reset**

And importantly, for local Docker testing, use `SC_COOKIE_SECURE=false` because you're accessing the app through plain `http://localhost:8000`. Setting it to `true` locally can prevent the browser from sending the session cookie.

---

# 1. First: safely stop/remove your current local container

If you currently have a container called `stranger-club`:

```bash
docker stop stranger-club 2>/dev/null || true
docker rm stranger-club 2>/dev/null || true
```

This removes the **container**, not the persistent volume.

Check your volumes:

```bash
docker volume ls
```

If you want a **completely fresh MVP test database**, remove the old Stranger Club volume:

```bash
docker volume rm stranger_club_data
```

If Docker says the volume doesn't exist, that's fine.

⚠️ **Only do this for your local test volume.** Don't do this against your eventual production volume because it deletes your local persisted database and uploaded files.

---

# 2. Build the v1.0.0-mvp image

From your project directory:

```bash
docker build -t stranger-club:v1.0.0-mvp .
```

Then verify:

```bash
docker images | grep stranger-club
```

You should see something like:

```text
stranger-club   v1.0.0-mvp
```

---

# 3. Create a fresh Docker volume

```bash
docker volume create stranger_club_data
```

This will hold:

```text
/data
├── stranger_club.db
└── uploads/
```

So deleting/recreating the container won't delete your application data.

---

# 4. Run Stranger Club locally

Use:

```bash
docker run -d \
  --name stranger-club \
  -p 8000:8000 \
  -v stranger_club_data:/data \
  -e SC_ENV=development \
  -e SC_ADMIN_USERNAME=organizer \
  -e SC_ADMIN_PASSWORD='stranger-club-local-test' \
  -e SC_COOKIE_SECURE=false \
  stranger-club:v1.0.0-mvp
```

Then check:

```bash
docker ps
```

You should see:

```text
stranger-club
```

---

# 5. Check the logs

Immediately after starting:

```bash
docker logs stranger-club
```

If you want to watch them live:

```bash
docker logs -f stranger-club
```

Press:

```text
Ctrl + C
```

to stop watching logs. This does **not** stop the container.

---

# 6. Check health

Open:

```text
http://localhost:8000/health
```

Or from Terminal:

```bash
curl http://localhost:8000/health
```

You should get something like:

```json
{
  "status": "ok"
}
```

Then:

```bash
curl http://localhost:8000/ready
```

This should confirm that the application can access the database.

---

# 7. Open the application

Go to:

```text
http://localhost:8000
```

Now test it as a real player.

### Public flow

```text
Event
 ↓
Registration
 ↓
Payment
 ↓
Screenshot upload
 ↓
Status
```

Then test the organiser side:

```text
http://localhost:8000/admin
```

Login:

```text
Username:
organizer

Password:
stranger-club-local-test
```

---

# 8. Test the important v1 workflow

I wouldn't just look at the UI.

Run this complete test:

### Player

```text
1. Open event
2. Register
3. Select player position
4. Submit
5. Upload payment screenshot
6. Check status
```

### Organiser

```text
1. Login
2. Open dashboard
3. Find registration
4. Open payment
5. Verify payment
6. Confirm player
7. Check event counter
```

### Real-time

Open the same event in two browser tabs.

```text
Tab A → Player
Tab B → Event page
```

Perform a registration in Tab A.

Tab B should update automatically.

---

# 9. Test persistence

This is **very important** because you're using SQLite inside Docker.

After registering a test player, run:

```bash
docker restart stranger-club
```

Wait a few seconds:

```bash
curl http://localhost:8000/health
```

Then open:

```text
http://localhost:8000
```

Your event and registration should still exist.

That's because:

```text
Docker container
      │
      ▼
/data
      │
      ▼
Docker volume
      │
      ├── stranger_club.db
      └── uploads/
```

---

# 10. Test the fresh-reset workflow

When you're finished testing and want a completely clean database:

```bash
docker stop stranger-club
docker rm stranger-club
docker volume rm stranger_club_data
```

Then recreate:

```bash
docker volume create stranger_club_data
```

and run the container again.

This gives you a **completely fresh Stranger Club instance**.

---

# 11. Now replace your README

I would delete the current README and create a cleaner one.

From your project root:

```bash
rm README.md
```

Then create:

```bash
touch README.md
```

Open it in your editor and paste this:

````markdown
# 🏏 Stranger Club

> A mobile-first cricket event management platform for weekly games with strangers.

Stranger Club replaces the manual:

**Google Form → UPI QR → Payment Screenshot → WhatsApp Confirmation**

workflow with a single platform for player registration and organiser operations.

---

## 🚀 Core MVP

### Player

- View cricket events
- Check live slot availability
- Register without creating an account
- Select preferred cricket position
- Pay through UPI
- Upload payment proof
- Track registration/payment status
- Join the waitlist when an event is full

### Organiser

- Secure organiser login
- Protected admin dashboard
- Create and manage multiple events
- View registrations
- Review payment proofs
- Confirm or reject payments
- Manage waitlists
- View event statistics

### Platform

- Real-time event counters
- Transaction-safe capacity management
- FIFO waitlist
- Separate registration and payment states
- Database migrations
- Audit records
- Secure payment-proof uploads
- Docker deployment
- Health/readiness checks

---

## 🏗️ Architecture

```text
                         Stranger Club
                              │
               ┌──────────────┴──────────────┐
               │                             │
            PUBLIC                       ORGANISER
               │                             │
          No account                    Login required
               │                             │
               ▼                             ▼
          Public API                  Protected API
               │                             │
               └──────────────┬──────────────┘
                              │
                           FastAPI
                              │
              ┌───────────────┼───────────────┐
              │               │               │
            Events       Registrations     Payments
              │               │               │
              └───────────────┼───────────────┘
                              │
                            SQLite
                              │
                         Domain Updates
                              │
                             SSE
                              │
                   Connected Clients
````

### Current stack

* React
* Vite
* FastAPI
* Python
* SQLite
* Server-Sent Events (SSE)
* Docker

The current MVP uses a single container and SQLite with persistent storage.

---

# 🐳 Run Locally with Docker

Docker is the recommended way to test the complete MVP locally.

## 1. Build the image

From the project root:

```bash
docker build -t stranger-club:v1.0.0-mvp .
```

## 2. Create persistent storage

```bash
docker volume create stranger_club_data
```

This volume stores:

```text
/data
├── stranger_club.db
└── uploads/
```

## 3. Start the application

```bash
docker run -d \
  --name stranger-club \
  -p 8000:8000 \
  -v stranger_club_data:/data \
  -e SC_ENV=development \
  -e SC_ADMIN_USERNAME=organizer \
  -e SC_ADMIN_PASSWORD='stranger-club-local-test' \
  -e SC_COOKIE_SECURE=false \
  stranger-club:v1.0.0-mvp
```

## 4. Check the container

```bash
docker ps
```

## 5. Check application logs

```bash
docker logs stranger-club
```

For live logs:

```bash
docker logs -f stranger-club
```

## 6. Check health

```bash
curl http://localhost:8000/health
```

Expected:

```json
{
  "status": "ok"
}
```

Check readiness:

```bash
curl http://localhost:8000/ready
```

## 7. Open the application

Public application:

```text
http://localhost:8000
```

Organiser dashboard:

```text
http://localhost:8000/admin
```

Local organiser credentials:

```text
Username: organizer
Password: stranger-club-local-test
```

Use a different strong password when deploying outside local development.

---

# 🧪 Local MVP Verification

After starting the Docker container, verify the complete workflow.

## Public player flow

```text
Open event
    ↓
View availability
    ↓
Register
    ↓
Select player position
    ↓
Submit registration
    ↓
Pay using UPI
    ↓
Upload payment screenshot
    ↓
Check registration status
```

## Organiser flow

```text
Open /admin
    ↓
Login
    ↓
Open dashboard
    ↓
View event
    ↓
Review registration
    ↓
Review payment proof
    ↓
Confirm / reject payment
    ↓
Verify updated event statistics
```

## Real-time test

Open the same event in two browser tabs.

Perform a registration or payment-state change in one tab.

The other connected tab should receive the updated event state without manually refreshing.

---

# 🔄 Persistence Test

The database and uploaded files are stored in the Docker volume.

After creating test data:

```bash
docker restart stranger-club
```

Then verify the application again:

```bash
curl http://localhost:8000/health
```

Existing events, registrations, payments, and uploads should remain available.

---

# 🧹 Reset Local Test Data

To completely reset the local MVP database:

```bash
docker stop stranger-club
docker rm stranger-club
docker volume rm stranger_club_data
```

Create a fresh volume:

```bash
docker volume create stranger_club_data
```

Then run the application again.

> ⚠️ Do not remove the production data volume when deploying the real application.

---

# 🔐 Security

Only organisers authenticate.

Players do not need accounts.

Organiser sessions use secure server-side authentication.

Important protections include:

* Argon2 password hashing
* HttpOnly session cookies
* CSRF protection
* Protected organiser APIs
* Server-side authorization
* Session expiration
* Login rate limiting
* Input validation
* Payment upload validation
* Duplicate registration protection
* Transaction-based capacity protection

Never commit production credentials or `.env` files to Git.

---

# 🗄️ Data Storage

The application stores persistent data under:

```text
/data
├── stranger_club.db
└── uploads/
```

The Docker volume keeps this data available across container restarts.

Database schema changes use non-destructive migrations.

Existing data is preserved during application upgrades.

---

# 📡 API

### Public

```text
GET  /api/events
GET  /api/events/{public_id}
GET  /api/events/{public_id}/summary
GET  /api/events/{public_id}/stream

POST /api/events/{public_id}/registrations

GET  /api/registrations/{public_id}

POST /api/registrations/{public_id}/payment
```

### Authentication

```text
POST /api/auth/login
GET  /api/auth/me
POST /api/auth/logout
```

### Organiser

```text
GET   /api/admin/events
POST  /api/admin/events

GET   /api/admin/events/{id}
PATCH /api/admin/events/{id}

GET   /api/admin/events/{id}/summary
GET   /api/admin/events/{id}/registrations

GET   /api/admin/payments/pending

POST  /api/admin/payments/{id}/confirm
POST  /api/admin/payments/{id}/reject

POST  /api/admin/registrations/{id}/promote
```

---

# 🧪 Development Checks

If you want to run the automated checks:

```bash
.venv/bin/pytest -q
```

Frontend production build:

```bash
npm run build
```

The project also includes Docker-based verification.

---

# 🌱 Development Without Docker

Docker is recommended for testing the complete application.

For direct local development:

```bash
npm install

python3 -m venv .venv

.venv/bin/python -m pip install -r requirements.txt
```

Start FastAPI:

```bash
SC_DATA_DIR=./data \
SC_ADMIN_PASSWORD='local-password' \
.venv/bin/uvicorn backend.app.main:app --reload --port 8000
```

In another terminal:

```bash
npm run dev
```

---

# 📦 Release

Current release:

**v1.0.0-mvp**

This is the first stable Core MVP release.

The release focuses on the essential Stranger Club workflow rather than a full enterprise platform.

---

# 🛣️ Roadmap

Future development will be driven by real-world usage and feedback.

Potential future areas:

* Player accounts
* Team formation
* Automated notifications
* Payment gateway integration
* Advanced organiser tools
* Multi-organiser support

These are intentionally outside the current Core MVP.

---

# 🎯 Product Philosophy

Stranger Club started with a simple problem:

> Weekly cricket was being organised through Google Forms, UPI screenshots, and WhatsApp messages.

The MVP turns that manual workflow into a single system.

The next stage is not about adding features for the sake of adding features.

It is:

**Build → Deploy → Observe → Learn → Improve**

---

## Author

**Guna**

````

