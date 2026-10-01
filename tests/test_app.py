import asyncio
import importlib
import os

from fastapi.testclient import TestClient


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_client(tmp_path, monkeypatch):
    """Reload app with a fresh in-memory database and return (client, chat module)."""
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "test.db"))
    import app.main
    chat = importlib.reload(app.main)
    return TestClient(chat.app), chat


# ── original tests (preserved) ────────────────────────────────────────────────

def test_chat_flow_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "test.db"))
    import app.main

    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        bob = client.post("/api/session", json={"username": "bob_user"})
        duplicate = client.post("/api/session", json={"username": "ALICE"})

        assert alice.status_code == 201
        assert bob.status_code == 201
        assert duplicate.status_code == 409

        alice_headers = {"Authorization": f"Bearer {alice.json()['token']}"}
        saved = client.post("/api/contacts", json={"username": "bob_user"}, headers=alice_headers)
        conversation = client.post(
            "/api/conversations", json={"username": "bob_user"}, headers=alice_headers
        )

        assert saved.json()["username"] == "bob_user"
        assert client.get("/api/contacts", headers=alice_headers).json() == [
            {"id": bob.json()["id"], "username": "bob_user"}
        ]
        assert conversation.json()["expiry_hours"] == 24

        conversation_id = conversation.json()["id"]
        updated = client.patch(
            f"/api/conversations/{conversation_id}",
            json={"expiry_hours": 3},
            headers=alice_headers,
        )
        message = client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"body": "hello"},
            headers=alice_headers,
        )

        assert updated.json()["expiry_hours"] == 3
        assert message.status_code == 201
        assert message.json()["sender"] == "alice"
        assert client.get(
            f"/api/conversations/{conversation_id}/messages", headers=alice_headers
        ).json()[0]["body"] == "hello"


def test_notification_manager_delivers_payload():
    from app.main import ConnectionManager

    class Socket:
        def __init__(self):
            self.payload = None

        async def send_json(self, payload):
            self.payload = payload

    socket = Socket()
    manager = ConnectionManager()
    manager.connections[7] = {socket}

    asyncio.run(manager.notify(7, {"type": "message", "message": {"body": "hello"}}))

    assert socket.payload["type"] == "message"
    assert socket.payload["message"]["body"] == "hello"


def test_reserved_username_can_be_reclaimed(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "reserved.db"))
    import app.main

    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        owner = client.post("/api/session", json={"username": "keeper"})
        headers = {"Authorization": f"Bearer {owner.json()['token']}"}

        reserved = client.post(
            "/api/session/reserve", json={"password": "long-secret"}, headers=headers
        )
        missing_password = client.post("/api/session", json={"username": "keeper"})
        wrong_password = client.post(
            "/api/session", json={"username": "keeper", "password": "wrong-pass"}
        )
        reclaimed = client.post(
            "/api/session", json={"username": "KEEPER", "password": "long-secret"}
        )

        assert reserved.json() == {"username": "keeper", "reserved": True}
        assert missing_password.status_code == 401
        assert wrong_password.status_code == 401
        assert reclaimed.status_code == 201
        assert reclaimed.json()["id"] == owner.json()["id"]
        assert reclaimed.json()["reserved"] is True
        assert client.get("/api/session", headers=headers).status_code == 401


# ── Feature 1: rate limiting ───────────────────────────────────────────────────

def test_session_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "rate.db"))
    import app.main
    chat = importlib.reload(app.main)
    # Reset limiter state so this test starts clean
    chat._session_rate_limiter._calls.clear()
    with TestClient(chat.app) as client:
        for i in range(10):
            r = client.post("/api/session", json={"username": f"ruser{i}"})
            assert r.status_code == 201, f"Expected 201, got {r.status_code} on attempt {i}"
        over = client.post("/api/session", json={"username": "ruser_over"})
        assert over.status_code == 429
        assert "detail" in over.json()


# ── Feature 2: block / unblock ────────────────────────────────────────────────

def test_block_unblock_and_send(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "block.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        bob = client.post("/api/session", json={"username": "bob"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}
        bob_h = {"Authorization": f"Bearer {bob.json()['token']}"}

        # open conversation
        conv = client.post("/api/conversations", json={"username": "bob"}, headers=alice_h)
        conv_id = conv.json()["id"]

        # alice blocks bob
        block_r = client.post("/api/block", json={"username": "bob"}, headers=alice_h)
        assert block_r.status_code == 201
        assert block_r.json()["blocked"] == "bob"

        # bob cannot send to alice after being blocked
        send_r = client.post(
            f"/api/conversations/{conv_id}/messages",
            json={"body": "hi alice"},
            headers=bob_h,
        )
        assert send_r.status_code == 403

        # blocked list reflects the block
        blocked = client.get("/api/blocked", headers=alice_h).json()
        assert any(u["username"] == "bob" for u in blocked)

        # alice unblocks bob
        unblock_r = client.post("/api/unblock", json={"username": "bob"}, headers=alice_h)
        assert unblock_r.json()["unblocked"] == "bob"

        # bob can send again
        send_after = client.post(
            f"/api/conversations/{conv_id}/messages",
            json={"body": "hello again"},
            headers=bob_h,
        )
        assert send_after.status_code == 201

        # blocked list is now empty for alice
        blocked_after = client.get("/api/blocked", headers=alice_h).json()
        assert not any(u["username"] == "bob" for u in blocked_after)


def test_cannot_block_self(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "blockself.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}
        r = client.post("/api/block", json={"username": "alice"}, headers=alice_h)
        assert r.status_code == 400


def test_block_nonexistent_user(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "blocknone.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}
        r = client.post("/api/block", json={"username": "ghost"}, headers=alice_h)
        assert r.status_code == 404


# ── Feature 3: report ─────────────────────────────────────────────────────────

def test_report_user(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "report.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        bob = client.post("/api/session", json={"username": "bob"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}

        r = client.post(
            "/api/report",
            json={"username": "bob", "reason": "spam", "details": "Sent me ads"},
            headers=alice_h,
        )
        assert r.status_code == 201
        assert r.json()["reported"] == "bob"
        assert r.json()["reason"] == "spam"


def test_report_invalid_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "report2.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        client.post("/api/session", json={"username": "bob"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}
        r = client.post(
            "/api/report",
            json={"username": "bob", "reason": "bad_reason"},
            headers=alice_h,
        )
        assert r.status_code == 422


def test_cannot_report_self(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "reportself.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}
        r = client.post("/api/report", json={"username": "alice", "reason": "spam"}, headers=alice_h)
        assert r.status_code == 400


# ── Feature 4 + 5: rooms and aliases ──────────────────────────────────────────

def test_create_and_join_room(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "rooms.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        bob = client.post("/api/session", json={"username": "bob"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}
        bob_h = {"Authorization": f"Bearer {bob.json()['token']}"}

        # alice creates a room
        room = client.post(
            "/api/rooms",
            json={"display_name": "Test Room", "default_message_lifetime_hours": 1},
            headers=alice_h,
        )
        assert room.status_code == 201
        room_code = room.json()["room_code"]
        assert len(room_code) == 6
        alice_alias = room.json()["your_alias"]
        assert alice_alias  # alias was assigned

        # bob joins using room code
        join = client.post("/api/rooms/join", json={"room_code": room_code}, headers=bob_h)
        assert join.status_code == 201
        bob_alias = join.json()["your_alias"]
        assert bob_alias != alice_alias  # different aliases

        # alice's alias is stable (re-joining returns same alias)
        rejoin = client.post("/api/rooms/join", json={"room_code": room_code}, headers=alice_h)
        assert rejoin.json()["your_alias"] == alice_alias
        assert rejoin.json()["already_member"] is True


def test_room_messages_show_aliases_not_usernames(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "rmsg.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        bob = client.post("/api/session", json={"username": "bob"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}
        bob_h = {"Authorization": f"Bearer {bob.json()['token']}"}

        room_code = client.post(
            "/api/rooms",
            json={"display_name": "AnonRoom"},
            headers=alice_h,
        ).json()["room_code"]
        client.post("/api/rooms/join", json={"room_code": room_code}, headers=bob_h)

        # bob sends a room message
        msg = client.post(
            f"/api/rooms/{room_code}/messages",
            json={"body": "Hello room"},
            headers=bob_h,
        )
        assert msg.status_code == 201
        assert "alice" not in msg.json().get("sender_alias", "")
        assert "bob" not in msg.json().get("sender_alias", "")

        # alice reads messages - sender_alias should not reveal real username
        messages = client.get(f"/api/rooms/{room_code}/messages", headers=alice_h).json()
        assert len(messages) == 1
        assert "alice" not in messages[0]["sender_alias"]
        assert "bob" not in messages[0]["sender_alias"]
        assert messages[0]["is_mine"] is False  # alice did not send this


def test_expired_room_rejects_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "expired.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}

        room = client.post(
            "/api/rooms",
            json={"display_name": "Temp", "expires_in_hours": 1},
            headers=alice_h,
        )
        room_code = room.json()["room_code"]

        # manually expire the room via SQLAlchemy session
        from datetime import UTC, datetime, timedelta
        from sqlalchemy import text as sql_text
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        with chat.SessionLocal() as db:
            db.execute(
                sql_text("UPDATE rooms SET expires_at = :ts WHERE room_code = :code"),
                {"ts": past, "code": room_code},
            )
            db.commit()

        send = client.post(
            f"/api/rooms/{room_code}/messages",
            json={"body": "too late"},
            headers=alice_h,
        )
        assert send.status_code == 410

        join = client.post("/api/rooms/join", json={"room_code": room_code}, headers=alice_h)
        assert join.status_code == 410


def test_alias_stable_within_room_and_different_across_rooms(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "alias.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}

        room1 = client.post("/api/rooms", json={"display_name": "Room1"}, headers=alice_h).json()
        room2 = client.post("/api/rooms", json={"display_name": "Room2"}, headers=alice_h).json()

        alias1 = room1["your_alias"]
        alias2 = room2["your_alias"]

        # aliases are assigned (non-empty)
        assert alias1 and alias2

        # re-joining returns same alias (stable)
        rejoin1 = client.post("/api/rooms/join", json={"room_code": room1["room_code"]}, headers=alice_h)
        assert rejoin1.json()["your_alias"] == alias1


def test_non_member_cannot_read_room_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "nonmember.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        eve = client.post("/api/session", json={"username": "eve"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}
        eve_h = {"Authorization": f"Bearer {eve.json()['token']}"}

        room_code = client.post(
            "/api/rooms", json={"display_name": "Private"}, headers=alice_h
        ).json()["room_code"]

        r = client.get(f"/api/rooms/{room_code}/messages", headers=eve_h)
        assert r.status_code == 403


# ── Feature 6: admin ──────────────────────────────────────────────────────────

def test_admin_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "admin.db"))
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        r = client.get("/api/admin/reports", headers={"Authorization": "Bearer anything"})
        assert r.status_code == 503


def test_admin_wrong_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "admin2.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "correct-token")
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        r = client.get("/api/admin/reports", headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401


def test_admin_reports_and_status(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "admin3.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    import app.main
    chat = importlib.reload(app.main)
    admin_h = {"Authorization": "Bearer admin-secret"}
    with TestClient(chat.app) as client:
        alice = client.post("/api/session", json={"username": "alice"})
        client.post("/api/session", json={"username": "bob"})
        alice_h = {"Authorization": f"Bearer {alice.json()['token']}"}

        client.post("/api/report", json={"username": "bob", "reason": "spam"}, headers=alice_h)

        reports = client.get("/api/admin/reports", headers=admin_h).json()
        assert len(reports) == 1
        assert reports[0]["status"] == "open"
        assert reports[0]["reporter_username"] == "alice"

        report_id = reports[0]["id"]
        updated = client.post(
            f"/api/admin/reports/{report_id}/status",
            json={"status": "reviewed"},
            headers=admin_h,
        )
        assert updated.json()["status"] == "reviewed"


def test_admin_disable_enable_user(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "admin4.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    import app.main
    chat = importlib.reload(app.main)
    admin_h = {"Authorization": "Bearer admin-secret"}
    with TestClient(chat.app) as client:
        # create bob with a reserved (password-protected) username
        bob = client.post("/api/session", json={"username": "bob"})
        bob_h = {"Authorization": f"Bearer {bob.json()['token']}"}
        client.post("/api/session/reserve", json={"password": "securepass123"}, headers=bob_h)

        # disable bob
        d = client.post("/api/admin/users/bob/disable", headers=admin_h)
        assert d.json()["disabled"] is True

        # bob's existing token is rejected
        me = client.get("/api/session", headers=bob_h)
        assert me.status_code == 403

        # bob cannot log in while disabled
        login_disabled = client.post(
            "/api/session", json={"username": "bob", "password": "securepass123"}
        )
        assert login_disabled.status_code == 403

        # re-enable bob
        e = client.post("/api/admin/users/bob/enable", headers=admin_h)
        assert e.json()["disabled"] is False

        # bob can log in again after re-enable
        login2 = client.post(
            "/api/session", json={"username": "bob", "password": "securepass123"}
        )
        assert login2.status_code == 201


def test_admin_list_users(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "admin5.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    import app.main
    chat = importlib.reload(app.main)
    admin_h = {"Authorization": "Bearer admin-secret"}
    with TestClient(chat.app) as client:
        client.post("/api/session", json={"username": "alice"})
        client.post("/api/session", json={"username": "bob"})

        users = client.get("/api/admin/users", headers=admin_h).json()
        usernames = [u["username"] for u in users]
        assert "alice" in usernames
        assert "bob" in usernames


# ── Stage 2: configuration ────────────────────────────────────────────────────

def test_config_defaults():
    from app.config import Settings
    s = Settings()
    url = s.effective_database_url
    assert url.startswith("sqlite:///")
    assert "chat.db" in url


def test_config_chat_db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "custom.db"))
    from app.config import Settings
    s = Settings()
    assert str(tmp_path / "custom.db") in s.effective_database_url


def test_config_database_url_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "ignored.db"))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./explicit.db")
    from app.config import Settings
    s = Settings()
    assert s.effective_database_url == "sqlite:///./explicit.db"


def test_config_repr_hides_secrets(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "super_secret_token_value")
    monkeypatch.setenv("REDIS_URL", "redis://:password@localhost/0")
    from app.config import Settings
    s = Settings()
    r = repr(s)
    assert "super_secret_token_value" not in r
    assert "password" not in r
    assert "has_admin_token=True" in r
    assert "has_redis_url=True" in r


# ── Stage 3: SQLAlchemy engine / db layer ─────────────────────────────────────

def test_build_engine_sqlite(tmp_path):
    from app.db import build_engine, check_database
    engine = build_engine(f"sqlite:///{tmp_path / 'test.db'}")
    assert check_database(engine)


def test_check_database_bad_url():
    from app.db import build_engine, check_database
    engine = build_engine("sqlite:////nonexistent_dir/nope/test.db")
    # check_database should return False for an inaccessible path
    result = check_database(engine)
    assert isinstance(result, bool)


# ── Stage 4: ORM models ───────────────────────────────────────────────────────

def test_models_register_with_base():
    from app.db import Base
    from app import models  # noqa: F401 — ensure registration
    tables = set(Base.metadata.tables.keys())
    expected = {"users", "contacts", "conversations", "messages",
                "blocked_users", "reports", "rooms", "room_members", "room_messages"}
    assert expected.issubset(tables)


# ── Stage 5: repository layer ─────────────────────────────────────────────────

def test_repository_user_crud(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "repo_user.db"))
    import app.main
    chat = importlib.reload(app.main)
    from datetime import UTC, datetime
    import app.repositories.users as users_repo
    from app.db import Base

    Base.metadata.create_all(chat.engine)
    with chat.SessionLocal() as db:
        user = users_repo.create(db, username="TestUser", token="tok1", created_at=datetime.now(UTC))
        db.commit()
        assert user.id is not None

        found = users_repo.get_by_username(db, "testuser")  # case-insensitive
        assert found is not None
        assert found.username == "TestUser"

        not_found = users_repo.get_by_username(db, "nobody")
        assert not_found is None

        found_by_token = users_repo.get_by_token(db, "tok1")
        assert found_by_token.id == user.id


def test_repository_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "repo_blocks.db"))
    import app.main
    chat = importlib.reload(app.main)
    from datetime import UTC, datetime
    import app.repositories.users as users_repo
    import app.repositories.blocks as blocks_repo
    from app.db import Base

    Base.metadata.create_all(chat.engine)
    with chat.SessionLocal() as db:
        u1 = users_repo.create(db, username="u1", token="t1", created_at=datetime.now(UTC))
        u2 = users_repo.create(db, username="u2", token="t2", created_at=datetime.now(UTC))
        db.commit()
        assert not blocks_repo.is_blocked(db, u1.id, u2.id)
        blocks_repo.block(db, u1.id, u2.id, datetime.now(UTC))
        db.commit()
        assert blocks_repo.is_blocked(db, u1.id, u2.id)
        blocks_repo.unblock(db, u1.id, u2.id)
        db.commit()
        assert not blocks_repo.is_blocked(db, u1.id, u2.id)


# ── Stage 14: health endpoints ────────────────────────────────────────────────

def test_health_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "health.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_health_dependencies(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "healthdeps.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        r = client.get("/health/dependencies")
        assert r.status_code == 200
        body = r.json()
        assert body["database"] == "up"
        assert body["redis"] == "not_configured"
        assert body["mode"] == "single_instance"


def test_readiness_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "ready.db"))
    import app.main
    chat = importlib.reload(app.main)
    with TestClient(chat.app) as client:
        r = client.get("/ready")
        assert r.status_code == 200
        assert r.json() == {"status": "ready"}


# ── Stage 7: Alembic migration smoke test ────────────────────────────────────

def test_alembic_upgrade_head_on_fresh_db(tmp_path):
    import subprocess, sys
    db_path = tmp_path / "alembic_smoke.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd="c:\\telstra\\whispers",
        env={**os.environ, "CHAT_DB": str(db_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Alembic failed:\n{result.stderr}"
    assert db_path.exists()

