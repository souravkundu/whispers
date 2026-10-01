# Whisper

Anonymous, real-time chat built with Python, FastAPI, WebSockets, SQLAlchemy, and a dependency-free browser client.

## Architecture

| Layer | Technology |
|---|---|
| HTTP / WebSocket | FastAPI + Uvicorn |
| Persistence | SQLAlchemy 2.x; SQLite (local) or PostgreSQL (production) |
| Migrations | Alembic |
| Distributed coordination | Redis (optional — rate limiting, Pub/Sub, presence) |
| Frontend | Plain HTML/CSS/JS — no build step, no framework |
| PWA | Service worker + Web App Manifest |

**Database is always the source of truth.** Redis is used only for rate-limiting state and WebSocket event fanout. If Redis is unavailable the application degrades to single-instance mode rather than failing.

### WebSocket delivery architecture

- Single instance (no Redis): events are delivered directly to in-process WebSocket connections.
- Multi-instance (Redis configured): the sending instance publishes a lightweight event to a Redis Pub/Sub channel; each instance subscribes and delivers to its local connections.

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | No | — | Full SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@host/db`. Takes precedence over `CHAT_DB`. |
| `CHAT_DB` | No | `./chat.db` | Legacy: path to SQLite file. Ignored when `DATABASE_URL` is set. |
| `REDIS_URL` | No | — | Redis connection URL. Omit to run in single-instance mode. |
| `REDIS_REQUIRED` | No | `false` | If `true`, startup fails when Redis is unreachable. |
| `ADMIN_TOKEN` | No | *(disabled)* | Secret token for admin endpoints. Unset → 503. |
| `ENVIRONMENT` | No | `development` | Namespace prefix for Redis keys. |
| `SQL_ECHO` | No | `false` | Log all SQL statements (development only). |
| `PORT` | No | `8000` | Listening port. |

Configuration precedence: `DATABASE_URL` › `CHAT_DB` › default `chat.db`.

## Local SQLite development (no Docker)

```powershell
python -m pip install -r requirements.txt
alembic upgrade head          # create schema (or skip — app creates schema on startup)
python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. Use a second browser or private window to claim another username and test live messaging.

## Docker Compose development stack

Starts Whisper + PostgreSQL + Redis in one command:

```bash
docker compose up --build
```

The app will be available at http://localhost:8000.

Set `ADMIN_TOKEN` in your shell before starting:

```bash
export ADMIN_TOKEN=my_dev_token
docker compose up --build
```

## Run only Python + SQLite (no Docker)

```powershell
python -m uvicorn app.main:app --reload
```

No `REDIS_URL` → single-instance mode. No `DATABASE_URL` → SQLite at `./chat.db`.

## Alembic commands

```bash
# Apply all pending migrations
alembic upgrade head

# Generate a new migration from model changes
alembic revision --autogenerate -m "add_foo_column"

# Check current migration state
alembic current
```

## Run tests

```powershell
python -m pytest -q
```

Tests use a temporary SQLite database and do not require PostgreSQL, Redis, or Docker.

## Existing database migration (chat.db)

If you have an existing `chat.db` created before Phase 2:

```powershell
# 1. Back up
copy chat.db chat.db.bak

# 2. Mark existing schema as up-to-date (skips DDL, records migration state)
alembic stamp head

# 3. Verify
alembic current
```

To verify schema completeness without modifying data:

```powershell
python -c "
from app.config import Settings; from app.db import build_engine
from sqlalchemy import inspect
eng = build_engine(Settings().effective_database_url)
insp = inspect(eng)
print('Tables:', insp.get_table_names())
"
```

## Features

- **Anonymous usernames** – no email, no profile.
- **Reserved usernames** – optional password protection (PBKDF2-SHA256, 600,000 iterations).
- **Saved contacts** – private quick-access list.
- **1:1 real-time messaging** – WebSocket delivery with browser notifications.
- **Message expiry** – 24-hour default; configurable per conversation.
- **Rate limiting** – `POST /api/session` limited to 10/min/IP (Redis-backed when configured).
- **Block / unblock** – blocked users cannot send new messages.
- **Report users / messages** – reports stored for admin review.
- **Anonymous rooms** – create or join temporary rooms with shareable codes; members appear as random aliases.
- **Admin moderation** – protected by `ADMIN_TOKEN`; `/admin` page available.
- **PWA install support** – manifest, service worker, offline shell caching.
- **Health endpoints** – `GET /health`, `GET /health/dependencies`, `GET /ready`.

## API summary

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/session` | — | Create or reclaim a session (rate-limited) |
| `GET`  | `/api/session` | User | Get current session info |
| `POST` | `/api/session/reserve` | User | Password-protect your username |
| `GET`  | `/api/users/{username}` | User | Look up a user |
| `GET`/`POST` | `/api/contacts` | User | List / save contacts |
| `POST` | `/api/conversations` | User | Open or reuse a 1:1 conversation |
| `PATCH` | `/api/conversations/{id}` | User | Change message expiry |
| `GET`/`POST` | `/api/conversations/{id}/messages` | User | Read / send 1:1 messages |
| `POST` | `/api/block` | User | Block a user |
| `POST` | `/api/unblock` | User | Unblock a user |
| `GET`  | `/api/blocked` | User | List blocked users |
| `POST` | `/api/report` | User | Report a user or message |
| `POST` | `/api/rooms` | User | Create a room |
| `GET`  | `/api/rooms` | User | List joined rooms |
| `POST` | `/api/rooms/join` | User | Join a room by code |
| `GET`/`POST` | `/api/rooms/{code}/messages` | User | Read / send room messages |
| `GET`  | `/api/admin/reports` | Admin | List all reports |
| `POST` | `/api/admin/reports/{id}/status` | Admin | Update report status |
| `GET`  | `/api/admin/users` | Admin | List all users |
| `POST` | `/api/admin/users/{username}/disable` | Admin | Disable a user |
| `POST` | `/api/admin/users/{username}/enable` | Admin | Re-enable a user |
| `GET`  | `/health` | — | Liveness check |
| `GET`  | `/health/dependencies` | — | Database and Redis status |
| `GET`  | `/ready` | — | Readiness check |

Admin endpoints require `Authorization: Bearer <ADMIN_TOKEN>`.

## Production considerations

- Use PostgreSQL (`DATABASE_URL=postgresql+psycopg://...`) for multiple app instances.
- Configure `REDIS_URL` for distributed rate limiting, presence, and WebSocket delivery.
- Serve over HTTPS (required for WebSockets, push notifications, and PWA install).
- Set a strong `ADMIN_TOKEN`.
- Do not expose PostgreSQL or Redis ports publicly.
- Run `alembic upgrade head` before deploying a new version.
- Back up PostgreSQL regularly.
- Rotate `ADMIN_TOKEN` and session secrets periodically.
- Bump `CACHE_NAME` in `static/service-worker.js` after every frontend change.
- Monitor `/ready` for liveness and dependency health.

## Features

- **Anonymous usernames** – no email, no profile. Just a name.
- **Reserved usernames** – optionally protect your name with a password (PBKDF2-SHA256).
- **Saved contacts** – quick-access list, private to each user.
- **1:1 real-time messaging** – WebSocket delivery with browser notifications and auto-reconnect.
- **Message expiry** – 24-hour default; configurable 1 hour–30 days per conversation.
- **Rate limiting** – `POST /api/session` is limited to 10 attempts per minute per IP.
- **Block / unblock users** – blocked users cannot send you new messages; you can unblock at any time.
- **Report users / messages** – abuse reports are stored privately for admin review.
- **Anonymous rooms (group chat)** – create or join temporary rooms using a short shareable code. Room members appear under auto-generated aliases; real usernames are never revealed to other members.
- **Admin moderation page** – protected by `ADMIN_TOKEN`; available at `/admin`.
- **PWA install support** – manifest, service worker, and offline shell caching.

## Environment variables

| Variable  | Required | Default      | Description |
|-----------|----------|--------------|-------------|
| `CHAT_DB` | No       | `./chat.db`  | Path to the SQLite database file. |
| `ADMIN_TOKEN` | No   | *(disabled)* | Secret token for admin API and `/admin` page. If unset, all admin endpoints return 503. |

## API summary

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/session` | — | Create or reclaim a session (rate-limited) |
| `GET`  | `/api/session` | User | Get current session info |
| `POST` | `/api/session/reserve` | User | Password-protect your username |
| `GET`  | `/api/users/{username}` | User | Look up a user |
| `GET`/`POST` | `/api/contacts` | User | List / save contacts |
| `POST` | `/api/conversations` | User | Open or reuse a 1:1 conversation |
| `PATCH` | `/api/conversations/{id}` | User | Change message expiry |
| `GET`/`POST` | `/api/conversations/{id}/messages` | User | Read / send 1:1 messages |
| `POST` | `/api/block` | User | Block a user |
| `POST` | `/api/unblock` | User | Unblock a user |
| `GET`  | `/api/blocked` | User | List blocked users |
| `POST` | `/api/report` | User | Report a user or message |
| `POST` | `/api/rooms` | User | Create a room |
| `GET`  | `/api/rooms` | User | List joined rooms |
| `POST` | `/api/rooms/join` | User | Join a room by code |
| `GET`/`POST` | `/api/rooms/{code}/messages` | User | Read / send room messages |
| `GET`  | `/api/admin/reports` | Admin | List all reports |
| `POST` | `/api/admin/reports/{id}/status` | Admin | Update report status |
| `GET`  | `/api/admin/users` | Admin | List all users |
| `POST` | `/api/admin/users/{username}/disable` | Admin | Disable a user |
| `POST` | `/api/admin/users/{username}/enable` | Admin | Re-enable a user |

Admin endpoints require the header `Authorization: Bearer <ADMIN_TOKEN>`.

## Anonymous rooms

Room members are assigned random aliases (e.g. `QuietRiver`, `BlueFox`) when they join. The same alias is stable for the user within that room but may differ across rooms. Real usernames are never included in room message payloads delivered to other members.

## Install on a phone

Deploy the app over HTTPS, open it in a mobile browser, and choose **Install app** or **Add to Home Screen**.

## Test

```powershell
python -m pytest -q
```

## Before going live checklist

- [ ] Serve over **HTTPS** (required for WebSockets, notifications, and PWA installation).
- [ ] Set `CHAT_DB` to a persistent path (e.g. a mounted volume).
- [ ] Set a strong `ADMIN_TOKEN` and keep it secret.
- [ ] Review the rate limit (currently 10 sessions/min/IP in-memory).
- [ ] Bump `CACHE_NAME` in `static/service-worker.js` after every UI change.
- [ ] For multi-instance deployments, replace SQLite with a shared database (e.g. PostgreSQL).
