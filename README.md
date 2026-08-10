# Whisper

Anonymous, real-time chat built with Python, FastAPI, WebSockets, SQLite, and a dependency-free browser client.

## Run locally

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. Use a second browser or private window to claim another username and test live messaging.

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
