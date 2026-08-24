"""User accounts for the web app: create one, log in, hold a session.

Why this exists at all, stated plainly. The server binds to 127.0.0.1 and is
already gated by a per-run token, so on a single-user laptop this login adds no
security the loopback bind does not already provide. It earns its place for two
other reasons: the app is graded coursework whose brief asks for account
creation and login, and the moment this is ever run for more than one person —
a shared demo box, a small team — "whose mailbox and whose briefs" becomes a
real question that a token cannot answer and an account can.

Built on the standard library on purpose. A password store is exactly the wrong
place to add a dependency that might go unpatched: `hashlib.pbkdf2_hmac` is in
Python itself, is the algorithm a hand-rolled scheme would be trying to
reimplement, and cannot rot. 240k iterations of SHA-256 is deliberately slow —
the whole point of a password hash is to cost an attacker time.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass

from ..config import SETTINGS

log = logging.getLogger(__name__)

# Serialises the first-account claim across both signup doors (password and
# Google). The lockdown is a check-then-insert, and without this two concurrent
# signups with different emails both read user_count()==0 and both succeed —
# exactly the public-tunnel case the lockdown exists to stop. Process-wide,
# which covers single-process uvicorn; a workers>1 deploy would additionally
# want a UNIQUE claim row in the table.
_signup_lock = __import__("threading").Lock()
# Serialises the one-time column upgrade so concurrent first-connections do not
# race the ALTER (which otherwise yields either a duplicate-column error for the
# loser or write-lock contention). Only contended during the brief first-upgrade
# window: once the columns exist, table_info shows them and the lock is skipped.
_migration_lock = __import__("threading").Lock()

_PBKDF2_ROUNDS = 240_000
_SESSION_TTL = 30 * 24 * 3600          # 30 days; a local tool is not a bank
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL DEFAULT '',
    pw_hash       TEXT NOT NULL,
    pw_salt       TEXT NOT NULL,
    rounds        INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    last_login_at REAL,
    -- 'local' (email + password) or 'google' (OAuth, no password). Defaulted so
    -- an ALTER on an existing db backfills every prior row to 'local'.
    auth_provider TEXT NOT NULL DEFAULT 'local',
    -- Reserved for a future minimal-scope openid login that would bind Google's
    -- stable subject id. NULL under the current no-scope-change design.
    google_sub    TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


class AuthError(Exception):
    """A failure the user should see and can act on — never a stack trace."""


@dataclass(frozen=True)
class User:
    id: str
    email: str
    name: str


def _db_path() -> str:
    # Alongside the agent database, not inside it: a corrupt accounts file must
    # not take the mail state with it, and vice versa.
    return str(SETTINGS.db_path.parent / "accounts.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # SCHEMA only creates tables that do not yet exist, so a column added to an
    # already-created users table would be silently ignored — the reason this
    # file needs a hand-rolled migration where store.py has a version gate. The
    # ALTER backfills every existing row to auth_provider='local', keeping
    # password login working after the upgrade.
    needed = (
        ("auth_provider",
         "ALTER TABLE users ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'local'"),
        ("google_sub", "ALTER TABLE users ADD COLUMN google_sub TEXT"),
    )
    have = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if any(column not in have for column, _ in needed):
        # Fast path skipped: a column is missing, so this is (probably) the
        # first connection after an upgrade. Serialise the ALTER so only one
        # connection writes it — no concurrent-ALTER means neither a
        # duplicate-column error nor write-lock contention between upgraders.
        with _migration_lock:
            have = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
            for column, ddl in needed:
                if column in have:
                    continue
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as exc:
                    # Belt-and-braces: if a column was added out from under us
                    # anyway, treat it as done rather than 500 the request.
                    if "duplicate column name" not in str(exc):
                        raise
    return conn


# --- password hashing -------------------------------------------------------


def _hash(password: str, salt: bytes, rounds: int) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return dk.hex()


def _verify(password: str, salt_hex: str, expected_hex: str, rounds: int) -> bool:
    got = _hash(password, bytes.fromhex(salt_hex), rounds)
    # Constant-time: a fast "wrong" and a slow "wrong" leak how much matched.
    return hmac.compare_digest(got, expected_hex)


# --- account lifecycle ------------------------------------------------------


def user_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT count(*) FROM users").fetchone()[0]


def create_user(*, email: str, password: str, name: str = "") -> User:
    email = (email or "").strip().lower()
    if not _EMAIL.match(email):
        raise AuthError("That does not look like an email address.")
    if len(password or "") < 8:
        raise AuthError("Use a password of at least 8 characters.")

    # First-user-owns-the-box. On a public tunnel, open signup means any visitor
    # can create an account and drive the agent against the operator's real
    # mailbox — so the default is that the first account claims the instance and
    # further signups are refused. SIGNUP_OPEN=true reopens it when the operator
    # genuinely wants more accounts. The check lives here, not in the endpoint,
    # so the CLI and any future caller inherit it too.
    salt = secrets.token_bytes(16)
    user_id = secrets.token_hex(12)
    now = _now()
    with _signup_lock:
        if user_count() > 0 and not SETTINGS.signup_open:
            raise AuthError(
                "This instance has already been claimed by its first account. "
                "Log in instead — or, if you are the operator and want to allow "
                "more accounts, set SIGNUP_OPEN=true and restart."
            )
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO users (id, email, name, pw_hash, pw_salt, rounds, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, email, name.strip(),
                     _hash(password, salt, _PBKDF2_ROUNDS),
                     salt.hex(), _PBKDF2_ROUNDS, now),
                )
        except sqlite3.IntegrityError:
            # Not a leak on a local single-user tool: telling the owner their
            # own address is already registered is a kindness. A shared
            # deployment would soften this.
            raise AuthError(
                "An account with that email already exists. Log in instead."
            )
    log.info("Created account for %s", _redact(email))
    return User(id=user_id, email=email, name=name.strip())


def authenticate(*, email: str, password: str) -> User:
    email = (email or "").strip().lower()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    # Verify against a decoy hash even when the user is unknown, so a missing
    # account and a wrong password take the same time to reject.
    if row is None:
        _verify(password, secrets.token_hex(16),
                secrets.token_hex(32), _PBKDF2_ROUNDS)
        raise AuthError("Email or password is incorrect.")
    if row["auth_provider"] != "local":
        # A Google account has no password (pw_hash='' , rounds=0). Rejecting it
        # here does two things: it stops rounds=0 reaching pbkdf2 (which would
        # raise rather than return False), and it keeps the failure timing and
        # message identical to a wrong password, so "this email is Google-only"
        # is not observable to someone guessing.
        _verify(password, secrets.token_hex(16),
                secrets.token_hex(32), _PBKDF2_ROUNDS)
        raise AuthError("Email or password is incorrect.")
    if not _verify(password, row["pw_salt"], row["pw_hash"], row["rounds"]):
        raise AuthError("Email or password is incorrect.")
    with _connect() as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                     (_now(), row["id"]))
    return User(id=row["id"], email=row["email"], name=row["name"])


def login_or_create_google(*, email: str, name: str = "") -> User:
    """Log in by proven control of a Gmail address — no password.

    The email arrives from ``users.getProfile`` after a completed OAuth consent
    on this machine, so it is Google-verified: the caller demonstrably controls
    that mailbox. Three cases:

    * **No account** — create one, subject to the same first-user lockdown that
      ``create_user`` enforces. The lockdown lives in that function, so this
      path has to re-check it or a claimed instance would quietly allow open
      self-registration through the Google door.
    * **Existing account** — log in. This covers both a returning Google
      account and a *local password* account with the same address. Linking is
      additive: the password row is never touched, so the password keeps
      working and Google becomes a second way in. That is safe precisely
      because the address is verified — anyone who can complete this OAuth
      already controls the mailbox the app exists to read, so there is nothing
      further a login could hand them. The reverse (password-logging-in a
      Google-only row) is refused in ``authenticate``.
    """
    email = (email or "").strip().lower()
    if not _EMAIL.match(email):
        raise AuthError("That sign-in produced no usable email address.")

    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row is not None:
        with _connect() as conn:
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                         (_now(), row["id"]))
        log.info("Google sign-in for existing account %s", _redact(email))
        return User(id=row["id"], email=row["email"], name=row["name"])

    # Same lock and same order as create_user: the lockdown check and the insert
    # are one critical section, shared across both doors, so a Google signup and
    # a password signup cannot both slip through as "the first account".
    user_id = secrets.token_hex(12)
    now = _now()
    with _signup_lock:
        # Re-read inside the lock — another door may have claimed the box while
        # we waited, and the pre-lock select is only a fast path.
        with _connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row is not None:
            with _connect() as conn:
                conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                             (_now(), row["id"]))
            return User(id=row["id"], email=row["email"], name=row["name"])
        if user_count() > 0 and not SETTINGS.signup_open:
            raise AuthError(
                "This instance has already been claimed by its first account. "
                "Sign in with the account that claimed it — or, if you are the "
                "operator and want to allow more accounts, set SIGNUP_OPEN=true "
                "and restart."
            )
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO users (id, email, name, pw_hash, pw_salt, rounds, "
                    "created_at, auth_provider) VALUES (?, ?, ?, '', '', 0, ?, "
                    "'google')",
                    (user_id, email, name.strip(), now),
                )
        except sqlite3.IntegrityError:
            with _connect() as conn:
                row = conn.execute("SELECT * FROM users WHERE email = ?",
                                   (email,)).fetchone()
            return User(id=row["id"], email=row["email"], name=row["name"])
    log.info("Created Google account for %s", _redact(email))
    return User(id=user_id, email=email, name=name.strip())


# --- sessions ---------------------------------------------------------------


def open_session(user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, user.id, now, now + _SESSION_TTL),
        )
    return token


def user_for_session(token: str) -> User | None:
    if not token:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT u.id, u.email, u.name, s.expires_at "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    if row is None:
        return None
    if row["expires_at"] < _now():
        close_session(token)
        return None
    return User(id=row["id"], email=row["email"], name=row["name"])


def close_session(token: str) -> None:
    if not token:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def is_owner(user: User) -> bool:
    """Is this the account that claimed the instance?

    The first account to sign up is the operator; anyone after them exists
    because the operator opened SIGNUP_OPEN for a demo window. Accounts gate
    entry, but settings, the API key and purge change what the *agent* does
    with the operator's mailbox — those stay with the owner, so a demo visitor
    can look around without being able to reconfigure or erase.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM users ORDER BY created_at, rowid LIMIT 1"
        ).fetchone()
    return row is not None and row["id"] == user.id


def _now() -> float:
    return time.time()


def _redact(email: str) -> str:
    """Never log an address whole."""
    name, _, domain = email.partition("@")
    head = (name[:2] + "…") if len(name) > 2 else "…"
    return f"{head}@{domain}"
