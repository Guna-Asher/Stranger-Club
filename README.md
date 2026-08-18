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

