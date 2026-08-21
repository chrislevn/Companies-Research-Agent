"""Local state: which senders we've seen, which messages we've processed.

This is what makes "new customer or partner" answerable — a sender is new if we
have never recorded them (or their domain) for this user before.

Everything is scoped by ``user_id`` so one deployment can watch many people's
mailboxes without them contaminating each other's history. Messages are keyed by
:attr:`EmailMessage.uid` (``provider:account:message_id``) because provider
message ids only unique within a single mailbox.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .config import SETTINGS
from .models import CompanyProfile, EmailMessage, TriageResult

log = logging.getLogger(__name__)

# A failed lookup is worth remembering for long enough to stop a scan loop
# retrying it, and no longer — the cause is usually transient.
FAILED_RESEARCH_TTL = timedelta(hours=6)

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS known_senders (
    user_id        TEXT NOT NULL DEFAULT 'default',
    email          TEXT NOT NULL,
    domain         TEXT NOT NULL DEFAULT '',
    display_name   TEXT NOT NULL DEFAULT '',
    company_name   TEXT NOT NULL DEFAULT '',
    relationship   TEXT NOT NULL DEFAULT 'unknown',
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    message_count  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, email)
);
CREATE INDEX IF NOT EXISTS idx_senders_domain ON known_senders(user_id, domain);

CREATE TABLE IF NOT EXISTS processed_messages (
    uid            TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL DEFAULT 'default',
    account_id     TEXT NOT NULL DEFAULT '',
    provider       TEXT NOT NULL DEFAULT '',
    message_id     TEXT NOT NULL DEFAULT '',
    thread_id      TEXT NOT NULL DEFAULT '',
    sender_email   TEXT NOT NULL DEFAULT '',
    subject        TEXT NOT NULL DEFAULT '',
    received_at    TEXT,
    processed_at   TEXT NOT NULL,
    triage_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_processed_sender ON processed_messages(user_id, sender_email);
CREATE INDEX IF NOT EXISTS idx_processed_account ON processed_messages(account_id);

CREATE TABLE IF NOT EXISTS agent_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Research is keyed by domain, not by message or user: two people writing from
-- the same company are one company to research, and the answer is public
-- information rather than anything user-specific.
CREATE TABLE IF NOT EXISTS company_research (
    domain         TEXT PRIMARY KEY,
    company_name   TEXT NOT NULL DEFAULT '',
    researched_at  TEXT NOT NULL,
    provider       TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    ok             INTEGER NOT NULL DEFAULT 1,
    error          TEXT NOT NULL DEFAULT '',
    profile_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_time ON company_research(researched_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or SETTINGS.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._migrate(conn)
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- migrations ----------

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """v1 (single-user, Gmail-only) → v2 (multi-user, multi-provider).

        v2 → v3 only adds ``company_research``, which the ``CREATE TABLE IF NOT
        EXISTS`` in :data:`SCHEMA` handles on its own — no data to move, so this
        returns early for a v2 database and lets SCHEMA do the work.
        """
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return

        existing = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "known_senders" not in existing:
            return  # fresh database, SCHEMA creates everything

        def columns(table: str) -> set[str]:
            return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

        senders_stale = "user_id" not in columns("known_senders")
        messages_stale = (
            "processed_messages" in existing and "uid" not in columns("processed_messages")
        )
        if not (senders_stale or messages_stale):
            return

        log.info("Migrating local database to schema v%d", SCHEMA_VERSION)

        # Rename every stale table *before* running SCHEMA — its CREATE INDEX
        # statements reference columns that only exist in the new shape.
        if senders_stale:
            conn.execute("ALTER TABLE known_senders RENAME TO known_senders_v1")
        if messages_stale:
            conn.execute("ALTER TABLE processed_messages RENAME TO processed_messages_v1")
        conn.executescript(SCHEMA)

        if senders_stale:
            conn.execute(
                """
                INSERT OR IGNORE INTO known_senders
                    (user_id, email, domain, display_name, company_name,
                     relationship, first_seen_at, last_seen_at, message_count)
                SELECT 'default', email, domain, display_name, company_name,
                       relationship, first_seen_at, last_seen_at, message_count
                FROM known_senders_v1
                """
            )
            conn.execute("DROP TABLE known_senders_v1")

        if messages_stale:
            # Everything in v1 came from the single default Gmail account.
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_messages
                    (uid, user_id, account_id, provider, message_id, thread_id,
                     sender_email, subject, received_at, processed_at, triage_json)
                SELECT 'gmail:default:' || message_id, 'default', 'default', 'gmail',
                       message_id, thread_id, sender_email, subject,
                       received_at, processed_at, triage_json
                FROM processed_messages_v1
                """
            )
            conn.execute("DROP TABLE processed_messages_v1")

    # ---------- senders ----------

    def is_known_sender(
        self, email: str, *, user_id: str = "default", match_domain: bool = True
    ) -> bool:
        """Known if we've seen this address, or (optionally) anyone at its domain."""
        email = email.lower()
        domain = email.split("@")[-1] if "@" in email else ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM known_senders WHERE user_id = ? AND email = ?",
                (user_id, email),
            ).fetchone()
            if row:
                return True
            if match_domain and domain:
                row = conn.execute(
                    "SELECT 1 FROM known_senders WHERE user_id = ? AND domain = ? LIMIT 1",
                    (user_id, domain),
                ).fetchone()
                return row is not None
        return False

    def record_sender(
        self,
        message: EmailMessage,
        *,
        user_id: str = "default",
        company_name: str = "",
        relationship: str = "unknown",
    ) -> None:
        email = message.sender.email.lower()
        if not email:
            return
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO known_senders
                    (user_id, email, domain, display_name, company_name, relationship,
                     first_seen_at, last_seen_at, message_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(user_id, email) DO UPDATE SET
                    last_seen_at  = excluded.last_seen_at,
                    message_count = known_senders.message_count + 1,
                    display_name  = COALESCE(NULLIF(known_senders.display_name, ''),
                                             excluded.display_name),
                    company_name  = COALESCE(NULLIF(excluded.company_name, ''),
                                             known_senders.company_name),
                    relationship  = CASE
                                        WHEN known_senders.relationship = 'unknown'
                                        THEN excluded.relationship
                                        ELSE known_senders.relationship
                                    END
                """,
                (
                    user_id,
                    email,
                    message.sender.domain,
                    message.sender.name,
                    company_name,
                    relationship,
                    now,
                    now,
                ),
            )

    def sender_count(self, user_id: str | None = None) -> int:
        with self._connect() as conn:
            if user_id is None:
                return conn.execute("SELECT COUNT(*) AS n FROM known_senders").fetchone()["n"]
            return conn.execute(
                "SELECT COUNT(*) AS n FROM known_senders WHERE user_id = ?", (user_id,)
            ).fetchone()["n"]

    # ---------- messages ----------

    def processed_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM processed_messages").fetchone()["n"]

    def is_processed(self, message: EmailMessage) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_messages WHERE uid = ?", (message.uid,)
            ).fetchone()
        return row is not None

    def mark_processed(
        self,
        message: EmailMessage,
        triage: TriageResult | None = None,
        *,
        user_id: str = "default",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO processed_messages
                    (uid, user_id, account_id, provider, message_id, thread_id,
                     sender_email, subject, received_at, processed_at, triage_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET
                    processed_at = excluded.processed_at,
                    triage_json  = excluded.triage_json
                """,
                (
                    message.uid,
                    user_id,
                    message.account_id,
                    message.provider,
                    message.message_id,
                    message.thread_id,
                    message.sender.email,
                    message.subject,
                    message.received_at.isoformat() if message.received_at else None,
                    _now(),
                    triage.model_dump_json() if triage else None,
                ),
            )

    # ---------- company research ----------

    def get_research(self, domain: str, *, max_age: timedelta | None = None) -> dict | None:
        """Cached research for a domain, or ``None`` if absent or too old.

        Failures are cached too, but with a much shorter life — a company that
        could not be researched at lunchtime may well be researchable tonight,
        and re-trying every scan is how a transient outage becomes a bill.
        """
        key = (domain or "").strip().lower()
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM company_research WHERE domain = ?", (key,)
            ).fetchone()
        if row is None:
            return None

        if max_age is not None:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(row["researched_at"])
            except (TypeError, ValueError):
                return None
            limit = max_age if row["ok"] else min(max_age, FAILED_RESEARCH_TTL)
            if age > limit:
                return None

        record = dict(row)
        record["ok"] = bool(row["ok"])
        try:
            record["profile"] = json.loads(row["profile_json"]) if row["profile_json"] else None
        except (TypeError, ValueError):
            record["profile"] = None
        return record

    def save_research(
        self,
        domain: str,
        profile: CompanyProfile | None,
        *,
        company_name: str = "",
        provider: str = "",
        model: str = "",
        error: str = "",
    ) -> None:
        key = (domain or "").strip().lower()
        if not key:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO company_research
                    (domain, company_name, researched_at, provider, model, ok, error, profile_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    company_name  = excluded.company_name,
                    researched_at = excluded.researched_at,
                    provider      = excluded.provider,
                    model         = excluded.model,
                    ok            = excluded.ok,
                    error         = excluded.error,
                    profile_json  = excluded.profile_json
                """,
                (
                    key,
                    company_name or (profile.name if profile else ""),
                    _now(),
                    provider,
                    model,
                    1 if profile is not None else 0,
                    error,
                    profile.model_dump_json() if profile is not None else None,
                ),
            )

    def research_count(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM company_research WHERE ok = 1"
            ).fetchone()["n"]

    def recent_leads(
        self, *, limit: int = 200, only_research: bool = True, user_id: str | None = None
    ) -> list[dict]:
        """Triaged messages, newest first — what the dashboard renders.

        ``triage_json`` is parsed in Python rather than filtered in SQL so this
        works on SQLite builds without the JSON1 extension.
        """
        sql = "SELECT * FROM processed_messages WHERE triage_json IS NOT NULL"
        params: list[object] = []
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY COALESCE(received_at, processed_at) DESC LIMIT ?"
        params.append(max(limit, 1) * (4 if only_research else 1))

        leads: list[dict] = []
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            # Joined in Python rather than SQL: research is keyed by domain and
            # the lead's domain lives inside triage_json, which SQLite cannot
            # reach without the JSON1 extension.
            research = {
                r["domain"]: r
                for r in conn.execute("SELECT * FROM company_research WHERE ok = 1")
            }

        for row in rows:
            try:
                triage = json.loads(row["triage_json"])
            except (TypeError, ValueError):
                continue
            if only_research and not triage.get("should_research"):
                continue

            profile = None
            found = research.get((triage.get("company_domain") or "").strip().lower())
            if found is not None:
                try:
                    profile = json.loads(found["profile_json"])
                except (TypeError, ValueError):
                    profile = None

            leads.append(
                {
                    "uid": row["uid"],
                    "provider": row["provider"],
                    "account_id": row["account_id"],
                    "thread_id": row["thread_id"],
                    "sender_email": row["sender_email"],
                    "subject": row["subject"],
                    "received_at": row["received_at"],
                    "processed_at": row["processed_at"],
                    "triage": triage,
                    "research": profile,
                    "researched_at": found["researched_at"] if found is not None else None,
                }
            )
            if len(leads) >= limit:
                break
        return leads

    def purge_user(self, user_id: str) -> int:
        """Delete everything for one user — the GDPR/PDPA erasure path."""
        with self._connect() as conn:
            senders = conn.execute(
                "DELETE FROM known_senders WHERE user_id = ?", (user_id,)
            ).rowcount
            messages = conn.execute(
                "DELETE FROM processed_messages WHERE user_id = ?", (user_id,)
            ).rowcount
        log.info("Purged user %s: %d senders, %d messages", user_id, senders, messages)
        return senders + messages

    def purge_older_than(self, cutoff: datetime) -> int:
        """Retention policy: drop processed-message records past their window."""
        with self._connect() as conn:
            return conn.execute(
                "DELETE FROM processed_messages WHERE processed_at < ?",
                (cutoff.isoformat(),),
            ).rowcount

    # ---------- state ----------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM agent_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_json_state(self, key: str, default: object = None) -> object:
        raw = self.get_state(key)
        return json.loads(raw) if raw else default
