"""Small account and trip store for the single-service Render deployment."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request, Response

COOKIE_NAME = "voyageos_session"
TOKEN_TTL = 60 * 60 * 24 * 7


def _data_path() -> Path:
    root = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
    root.mkdir(parents=True, exist_ok=True)
    return root / "voyageos.db"


@contextmanager
def database():
    connection = sqlite3.connect(_data_path(), timeout=8)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 8000")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    with database() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS trips (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                destination TEXT NOT NULL,
                title TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_trips_user_updated ON trips(user_id, updated_at DESC)")
        db.execute("PRAGMA optimize")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _secret() -> bytes:
    return os.getenv("AUTH_SECRET", "local-development-secret-change-me").encode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt$16384$8$1$" + _b64(salt) + "$" + _b64(digest)


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                                dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(digest, _unb64(expected))
    except (ValueError, TypeError):
        return False


def issue_token(user_id: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({"sub": user_id, "exp": int(time.time()) + TOKEN_TTL}, separators=(",", ":")).encode())
    signature = _b64(hmac.new(_secret(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def decode_token(token: str) -> str | None:
    try:
        header, payload, signature = token.split(".")
        expected = _b64(hmac.new(_secret(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(_unb64(payload))
        if int(data.get("exp", 0)) <= int(time.time()):
            return None
        return str(data["sub"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def public_user(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"], "email": row["email"]}


def create_user(name: str, email: str, password: str) -> dict:
    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        with database() as db:
            db.execute("INSERT INTO users(id, name, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                       (user_id, name.strip(), email.strip().casefold(), hash_password(password), now))
            row = db.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with this email already exists.") from None
    return public_user(row)


def authenticate(email: str, password: str) -> dict:
    with database() as db:
        row = db.execute("SELECT * FROM users WHERE email = ?", (email.strip().casefold(),)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        raise HTTPException(401, "Incorrect email or password.")
    return public_user(row)


def user_from_request(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME, "")
    user_id = decode_token(token) if token else None
    if not user_id:
        return None
    with database() as db:
        row = db.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()
    return public_user(row) if row else None


def require_user(request: Request) -> dict:
    user = user_from_request(request)
    if not user:
        raise HTTPException(401, "Sign in to save and sync trips.")
    return user


def set_session(response: Response, user_id: str) -> None:
    secure = os.getenv("COOKIE_SECURE", "true" if os.getenv("RENDER") else "false").lower() == "true"
    response.set_cookie(COOKIE_NAME, issue_token(user_id), max_age=TOKEN_TTL, httponly=True, secure=secure,
                        samesite="lax", path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", samesite="lax")


def save_trip(user_id: str, trip: dict) -> dict:
    trip_id = str(trip.get("id") or uuid.uuid4())[:80]
    brief = trip.get("brief") if isinstance(trip.get("brief"), dict) else {}
    destination = str(brief.get("destination") or "Trip")[:100]
    title = str(trip.get("title") or f"{destination} · {brief.get('days', '?')} days")[:160]
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(trip, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode()) > 180000:
        raise HTTPException(413, "This trip is too large to save.")
    with database() as db:
        owned = db.execute("SELECT created_at FROM trips WHERE id = ? AND user_id = ?", (trip_id, user_id)).fetchone()
        if owned:
            db.execute("UPDATE trips SET destination = ?, title = ?, payload = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                       (destination, title, payload, now, trip_id, user_id))
            created_at = owned["created_at"]
        else:
            db.execute("INSERT INTO trips(id, user_id, destination, title, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (trip_id, user_id, destination, title, payload, now, now))
            created_at = now
    return {"id": trip_id, "destination": destination, "title": title, "trip": trip,
            "created_at": created_at, "updated_at": now}


def list_trips(user_id: str) -> list[dict]:
    with database() as db:
        rows = db.execute("SELECT id, destination, title, payload, created_at, updated_at FROM trips WHERE user_id = ? ORDER BY updated_at DESC LIMIT 50",
                          (user_id,)).fetchall()
    result = []
    for row in rows:
        try:
            trip = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue
        result.append({"id": row["id"], "destination": row["destination"], "title": row["title"], "trip": trip,
                       "created_at": row["created_at"], "updated_at": row["updated_at"]})
    return result


def delete_trip(user_id: str, trip_id: str) -> None:
    with database() as db:
        cursor = db.execute("DELETE FROM trips WHERE id = ? AND user_id = ?", (trip_id, user_id))
    if not cursor.rowcount:
        raise HTTPException(404, "Saved trip not found.")
