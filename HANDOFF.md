# Whisper — Handoff

## Where to pick up next

The app is fully built and tested locally. The two remaining steps are:

1. **[Deploy to a server](#step-1--deploy-to-a-server)** — gets it on the internet with HTTPS
2. **[Install on a phone](#step-2--install-on-your-phone)** — HTTPS must be live first

Jump to whichever step you are on.

---

## What is still pending (optional)

Nothing blocks local use. These only matter before sharing the URL publicly or scaling up.

| # | Item | Effort | When needed | Details |
|---|------|--------|-------------|---------|
| 1 | **Deploy to server** | ~30 min | To use on real phones | [Step 1 below](#step-1--deploy-to-a-server) |
| 2 | **Install on phone** | ~5 min | After Step 1 | [Step 2 below](#step-2--install-on-your-phone) |
| 3 | **Rate-limit login endpoint** | ~30 min | Before making URL public | Add `slowapi` middleware on `POST /api/session` — stops username squatting bots |
| 4 | **Swap SQLite → PostgreSQL** | ~2 hrs | More than a handful of concurrent users | Replace `sqlite3` calls in `app/main.py` with `psycopg2`; schema DDL transfers directly; set `DATABASE_URL` env var |
| 5 | **Web Push (background notifications)** | ~3 hrs | Notifications when app is closed | Requires VAPID keys (`pywebpush` library), a `push_subscriptions` DB table, and a `/api/push/subscribe` endpoint |
| 6 | **Bump service worker cache version** | 1 min per deploy | Every UI update | Change `whisper-shell-v2` → `whisper-shell-v3` in `static/service-worker.js` before each deploy so users get fresh files |

---


## What was built

Anonymous real-time chat app built with Python (FastAPI), WebSockets, SQLite, and a dependency-free browser client. Installable as a PWA on mobile devices.

### Features
- Unique, case-insensitive usernames (anonymous, no email)
- Optional password-protected username reservation (PBKDF2-SHA256, salted)
- Saved contacts per user
- Real-time messaging via WebSockets with auto-reconnect
- In-app toast notifications + browser Notification API alerts for new messages (while app is open)
- Message expiry: 24-hour default, configurable per conversation (1 h – 30 days)
- Background cleanup task purges expired messages every 60 s
- Responsive layout: desktop sidebar + mobile back-navigation
- PWA: installable via "Add to Home Screen", offline shell cache

---

## Project layout

```
app/
  main.py          # FastAPI app — all routes, WebSocket hub, DB schema
static/
  index.html       # Single-page shell
  app.js           # All client logic (no framework)
  styles.css       # Responsive CSS
  service-worker.js# PWA offline cache (shell only, never chat data)
  manifest.webmanifest
  icons/           # icon-192.png, icon-512.png, icon-maskable-512.png, apple-touch-icon.png
tests/
  test_app.py      # Focused integration tests
requirements.txt   # Pinned Python deps
```

---

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. Use a private window for a second user.

Run tests:

```bash
pytest -q
```

---

## Step 1 — Deploy to a server

Pick one option. Fly.io is the fastest with zero server management.

### Option A — Fly.io (recommended, free tier, WebSocket support)

```bash
# Install Fly CLI: https://fly.io/docs/hands-on/install-flyctl/
fly auth login
fly launch          # accepts defaults; choose a region close to users
fly deploy
```

Fly auto-provisions HTTPS. The PWA install prompt appears immediately after first load.

---

### Option B — Render / Railway

1. Push repo to GitHub.
2. Create a new **Web Service** on Render or Railway, point to the repo.
3. Set **Start command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. HTTPS and a subdomain are provisioned automatically.

---

### Option C — Your own Linux VPS (Hetzner, DigitalOcean, etc.)

```bash
# On the server
sudo apt update && sudo apt install python3 python3-pip nginx certbot python3-certbot-nginx -y
git clone <your-repo> /opt/whisper && cd /opt/whisper
pip3 install -r requirements.txt

# Systemd service
sudo tee /etc/systemd/system/whisper.service <<EOF
[Unit]
Description=Whisper chat
After=network.target

[Service]
WorkingDirectory=/opt/whisper
ExecStart=/usr/local/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
Environment=CHAT_DB=/opt/whisper/data/chat.db

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now whisper
```

Nginx config (`/etc/nginx/sites-available/whisper`):

```nginx
server {
    listen 80;
    server_name yourchat.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name yourchat.example.com;

    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/whisper /etc/nginx/sites-enabled/
sudo certbot --nginx -d yourchat.example.com
sudo nginx -s reload
```

Once any option above is live, note your public URL (e.g. `https://yourchat.fly.dev`). You need it for Step 2.

---

## Step 2 — Install on your phone

> **Requires Step 1 to be complete.** The install prompt only appears over HTTPS.

### Android (Chrome or Edge)

1. Open your public URL in Chrome or Edge.
2. Tap the **⋮ menu → Add to Home Screen** — or wait for the banner that says "Add Whisper to Home Screen".
3. Tap **Install**. The app icon appears on your home screen like a native app.

### iPhone / iPad (Safari only)

1. Open your public URL in **Safari** (not Chrome — iOS restricts PWA install to Safari).
2. Tap the **Share button** (box with arrow pointing up).
3. Scroll down and tap **Add to Home Screen**.
4. Tap **Add**. The Whisper icon appears on your home screen.

### What the installed app does

- Opens full-screen with no browser chrome (looks native).
- Loads the login screen instantly from cache even with no signal.
- Sends and receives chat messages normally once connected.
- Shows notifications for new messages when the app is open.

---

## Before going live — checklist

Do these in order before sharing the URL with anyone.

| # | Action | How |
|---|--------|-----|
| 1 | **Enable HTTPS** | Handled automatically by Fly.io / Render / Railway. On a VPS, run `certbot --nginx`. |
| 2 | **Set `CHAT_DB` to a persistent path** | Set env var `CHAT_DB=/data/chat.db` (Fly.io: use a mounted volume). |
| 3 | **Add rate-limiting on login** | `pip install slowapi`, add `Limiter` to `app/main.py`, decorate `create_session` with `@limiter.limit("10/minute")`. |
| 4 | **Bump service worker version on every deploy** | Edit `CACHE_NAME = "whisper-shell-v2"` → `"whisper-shell-v3"` etc. in `static/service-worker.js` each time you ship a UI change. |
| 5 | **Swap SQLite → PostgreSQL when scaling** | Only needed beyond a handful of concurrent users — SQLite blocks on concurrent writes. |

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CHAT_DB` | `<project root>/chat.db` | SQLite database path |
| `PORT` | not read automatically | PaaS platforms set this; pass it explicitly: `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |

---

## PostgreSQL migration (summary)

Replace `sqlite3` calls in `app/main.py` with `psycopg2` (sync) or `asyncpg` (async). The schema DDL transfers directly. Set `DATABASE_URL` as an env var. Most PaaS platforms provision a Postgres instance with one click.
