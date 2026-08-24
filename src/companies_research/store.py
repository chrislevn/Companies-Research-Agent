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
import uuid
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

SCHEMA_VERSION = 6

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

-- Every tool call, whether it ran or was refused. `args_hash` is a digest, not
-- the arguments: this table is an audit trail, and an audit trail that copies
-- email addresses and message bodies out of the database it is auditing is a
-- second place for them to leak from.
CREATE TABLE IF NOT EXISTS tool_calls (
    id            TEXT PRIMARY KEY,
    ts            TEXT NOT NULL,
    tool          TEXT NOT NULL,
    caller        TEXT NOT NULL DEFAULT '',
    args_hash     TEXT NOT NULL DEFAULT '',
    gate_results  TEXT NOT NULL DEFAULT '{}',
    denied_at     TEXT,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    trace_id      TEXT NOT NULL DEFAULT ''
);
-- One row per generated brief. `brief_json` is the whole document, so an
-- approved brief is exactly what the approver saw rather than something
-- re-assembled later from sources that may since have changed.
CREATE TABLE IF NOT EXISTS briefs (
    id            TEXT PRIMARY KEY,
    lead_id       TEXT NOT NULL DEFAULT '',
    domain        TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    generated_at  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',
    approved_by   TEXT NOT NULL DEFAULT '',
    approved_at   TEXT,
    brief_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_briefs_status ON briefs(status, generated_at);
CREATE INDEX IF NOT EXISTS idx_briefs_domain ON briefs(domain);

CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool, denied_at);

-- Long-term memory for the chat agent, stored as RAG rows: the text and its
-- embedding side by side. Unlike every other table this one stores content on
-- purpose — remembering is its job. The embedding is a JSON float array;
-- cosine search happens in Python, which at demo scale beats running a vector
-- database for the same answer.
CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL DEFAULT 'default',
    category     TEXT NOT NULL DEFAULT 'general',
    source       TEXT NOT NULL DEFAULT '',
    chunk_index  INTEGER NOT NULL DEFAULT 0,
    text         TEXT NOT NULL,
    embedding    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, created_at);
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

        v2 → v3 adds ``company_research``, v3 → v4 adds ``tool_calls``,
        v4 → v5 adds ``briefs`` and v5 → v6 adds ``memories``. All four are
        pure additions, which the ``CREATE TABLE IF NOT EXISTS`` statements
        in :data:`SCHEMA` handle on their own — no data to move, so this returns
        early for a v2-or-later database and lets SCHEMA do the work.
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

    # ---------- tool audit ----------

    def open_tool_call(
        self,
        *,
        call_id: str,
        tool: str,
        caller: str,
        args_hash: str,
        gate_results: dict[str, bool],
        trace_id: str = "",
    ) -> None:
        """Write the audit row *before* the tool runs.

        Recorded first so that a crash, a kill or a hang still leaves evidence
        that the call was attempted. A row with ``ok = 0`` and no error is a
        call that never came back.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tool_calls
                    (id, ts, tool, caller, args_hash, gate_results, denied_at,
                     duration_ms, ok, error, trace_id)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 0, 0, NULL, ?)
                """,
                # Not sort_keys: insertion order *is* the gate sequence, and an
                # audit row that shows the order they ran in is worth more than
                # one that is alphabetised.
                (call_id, _now(), tool, caller, args_hash,
                 json.dumps(gate_results), trace_id),
            )

    def close_tool_call(
        self,
        call_id: str,
        *,
        gate_results: dict[str, bool],
        denied_at: str | None,
        duration_ms: int,
        ok: bool,
        error: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tool_calls
                   SET gate_results = ?, denied_at = ?, duration_ms = ?, ok = ?, error = ?
                 WHERE id = ?
                """,
                (json.dumps(gate_results), denied_at,
                 duration_ms, 1 if ok else 0, error, call_id),
            )

    def recent_tool_calls(self, *, limit: int = 100, tool: str | None = None) -> list[dict]:
        sql = "SELECT * FROM tool_calls"
        params: list[object] = []
        if tool:
            sql += " WHERE tool = ?"
            params.append(tool)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(max(limit, 1))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["ok"] = bool(row["ok"])
            try:
                record["gate_results"] = json.loads(row["gate_results"])
            except (TypeError, ValueError):
                record["gate_results"] = {}
            out.append(record)
        return out

    # ---------- memories ----------

    def add_memory(self, *, text: str, embedding: list[float], category: str = "general",
                   source: str = "", chunk_index: int = 0, user_id: str = "default") -> str:
        memory_id = uuid.uuid4().hex[:16]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memories
                    (id, user_id, category, source, chunk_index, text, embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (memory_id, user_id, category, source, chunk_index, text,
                 json.dumps(embedding), _now()),
            )
        return memory_id

    def iter_memories(self, *, user_id: str = "default") -> list[dict]:
        """Every memory with its vector, for in-process similarity search."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            try:
                record["embedding"] = json.loads(row["embedding"])
            except (TypeError, ValueError):
                continue  # an unreadable vector cannot be searched; skip the row
            out.append(record)
        return out

    def replace_memories_for_source(self, source: str, *, user_id: str = "default") -> int:
        """Drop every memory row from one source, ahead of re-indexing it.

        Re-indexing a brief that already has rows would otherwise answer every
        recall twice; replacing by source keeps indexing idempotent.
        """
        if not source:
            return 0
        with self._connect() as conn:
            return conn.execute(
                "DELETE FROM memories WHERE user_id = ? AND source = ?",
                (user_id, source),
            ).rowcount

    def iter_research(self, *, limit: int = 500) -> list[dict]:
        """Successful research rows with parsed profiles, for indexing."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT domain, company_name, researched_at, profile_json
                FROM company_research WHERE ok = 1 AND profile_json IS NOT NULL
                ORDER BY researched_at DESC LIMIT ?
                """,
                (max(limit, 1),),
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            try:
                record["profile"] = json.loads(row["profile_json"])
            except (TypeError, ValueError):
                continue
            record.pop("profile_json", None)
            out.append(record)
        return out

    def list_memories(self, *, user_id: str = "default", limit: int = 200) -> list[dict]:
        """Memories without vectors, newest first — for showing, not searching."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, category, source, chunk_index, text, created_at
                FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT ?
                """,
                (user_id, max(limit, 1)),
            ).fetchall()
        return [dict(row) for row in rows]

    def memory_count(self, user_id: str | None = None) -> int:
        with self._connect() as conn:
            if user_id is None:
                row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE user_id = ?", (user_id,)
                ).fetchone()
        return int(row[0])

    # ---------- briefs ----------

    def save_brief(self, brief) -> str:
        """Persist a brief, replacing any earlier draft for the same lead.

        An approved or delivered brief is never overwritten by a regenerated
        draft: what somebody approved has to stay what they approved.
        """
        existing = self.get_brief_by_lead(brief.lead_id)
        if existing and existing["status"] in ("approved", "delivered"):
            log.info("Not replacing %s brief for %s", existing["status"], brief.lead_id)
            return existing["id"]

        brief_id = existing["id"] if existing else uuid.uuid4().hex[:16]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO briefs
                    (id, lead_id, domain, company, generated_at, status,
                     approved_by, approved_at, brief_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (brief_id, brief.lead_id, brief.domain, brief.company,
                 brief.generated_at.isoformat(), brief.status, brief.approved_by,
                 brief.approved_at.isoformat() if brief.approved_at else None,
                 brief.model_dump_json()),
            )
        return brief_id

    def get_brief(self, brief_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
        return self._brief_row(row)

    def get_brief_by_lead(self, lead_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM briefs WHERE lead_id = ? ORDER BY generated_at DESC LIMIT 1",
                (lead_id,),
            ).fetchone()
        return self._brief_row(row)

    def list_briefs(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM briefs"
        params: list[object] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY generated_at DESC LIMIT ?"
        params.append(max(limit, 1))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [r for r in (self._brief_row(row) for row in rows) if r]

    # A decision can be withdrawn; a delivery cannot.
    #
    # `approved` and `rejected` may both return to `draft`, because a human
    # changing their mind before anything is sent is the entire point of having
    # a human in the loop. Withdrawal goes to `draft` rather than straight to
    # the opposite verdict, so reconsidering still costs a fresh decision —
    # nobody flips a rejection into an approval in one click.
    #
    # `delivered` is terminal and stays that way. The brief has left the
    # machine; moving it back to `draft` would erase the record that it was
    # sent while looking like success in the UI, which is the failure this
    # table exists to prevent.
    BRIEF_TRANSITIONS = {
        "draft": {"approved", "rejected"},
        "approved": {"delivered", "rejected", "draft"},
        "rejected": {"draft"},
        "delivered": set(),
    }

    def set_brief_status(self, brief_id: str, status: str, *, approved_by: str = "") -> bool:
        record = self.get_brief(brief_id)
        if record is None:
            return False
        allowed = self.BRIEF_TRANSITIONS.get(record["status"], set())
        if status != record["status"] and status not in allowed:
            log.warning(
                "Refusing to move brief %s from %s to %s", brief_id, record["status"], status
            )
            return False
        brief = record["brief"]
        brief["status"] = status

        if status == "draft":
            # Withdrawn. The approver is cleared rather than kept, because a
            # draft that still names an approver reads as approved to anyone
            # scanning the list — and the person who withdrew it is in the log.
            log.info("Brief %s withdrawn to draft by %s (was %s, approved by %s)",
                     brief_id, approved_by or "operator", record["status"],
                     record["approved_by"] or "—")
            brief["approved_by"] = ""
            brief["approved_at"] = None
            with self._connect() as conn:
                conn.execute(
                    "UPDATE briefs SET status = ?, approved_by = '', approved_at = NULL, "
                    "brief_json = ? WHERE id = ?",
                    (status, json.dumps(brief), brief_id),
                )
            return True

        stamp = _now() if status in ("approved", "rejected") else record["approved_at"]
        if status in ("approved", "rejected"):
            brief["approved_by"] = approved_by
            brief["approved_at"] = stamp
        with self._connect() as conn:
            conn.execute(
                "UPDATE briefs SET status = ?, approved_by = ?, approved_at = ?, "
                "brief_json = ? WHERE id = ?",
                (status, approved_by or record["approved_by"], stamp,
                 json.dumps(brief), brief_id),
            )
        return True

    @staticmethod
    def _brief_row(row) -> dict | None:
        if row is None:
            return None
        record = dict(row)
        try:
            record["brief"] = json.loads(row["brief_json"])
        except (TypeError, ValueError):
            record["brief"] = None
        return record

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

        # A failed lookup must not destroy a good profile. Research can fail for
        # reasons that have nothing to do with the company — a rate limit, a
        # rejected schema, a network blip — and losing a working brief to a
        # transient error is a far worse outcome than serving one that is a few
        # hours stale. Record the failure, keep the evidence.
        if profile is None:
            existing = self.get_research(key)
            if existing and existing.get("profile"):
                log.warning(
                    "Keeping the cached profile for %s; this lookup failed: %s",
                    key, error or "unknown error",
                )
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE company_research SET error = ?, researched_at = ? "
                        "WHERE domain = ?",
                        (error, _now(), key),
                    )
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
        """Delete everything derived from one user's mail — the erasure path.

        Attribution first, deletion second: briefs and cached research carry no
        ``user_id`` column, so what belongs to this user is worked out from the
        rows that do — their processed messages name the briefs (``lead_id`` is
        ``domain:message_id``) and their known senders name the researched
        domains. Where a row cannot be attributed at all (a brief with no lead,
        a shared research entry for a domain this user's senders wrote from),
        erasure wins over precision and the row goes: over-deleting a cache
        that rebuilds itself is a cost; under-deleting personal data is a lie
        in the README.

        Delivered brief files in the delivery directory are removed by domain
        slug for the same reason. What this deliberately does not touch: the
        web login accounts in ``accounts.db`` (credentials, not mail-derived —
        deleted from the interface) and ``agent_state`` (scan timestamps only).
        """
        with self._connect() as conn:
            message_ids = {
                row["message_id"]
                for row in conn.execute(
                    "SELECT message_id FROM processed_messages WHERE user_id = ?",
                    (user_id,),
                )
                if row["message_id"]
            }
            domains = {
                row["domain"]
                for row in conn.execute(
                    "SELECT domain FROM known_senders WHERE user_id = ?", (user_id,)
                )
                if row["domain"]
            }

            brief_domains, brief_ids = set(), []
            for row in conn.execute("SELECT id, lead_id, domain FROM briefs"):
                _, _, lead_message = (row["lead_id"] or "").rpartition(":")
                attributed = (
                    lead_message in message_ids
                    or row["domain"] in domains
                    or not row["lead_id"]
                )
                if attributed:
                    brief_ids.append(row["id"])
                    if row["domain"]:
                        brief_domains.add(row["domain"])

            senders = conn.execute(
                "DELETE FROM known_senders WHERE user_id = ?", (user_id,)
            ).rowcount
            messages = conn.execute(
                "DELETE FROM processed_messages WHERE user_id = ?", (user_id,)
            ).rowcount
            # Memory rows written on this user's behalf mostly sit under the
            # 'default' user id (the chat runs single-user), so matching the
            # id alone would leave the indexed copies of their research and
            # briefs searchable after the purge. Attribute by source the same
            # way briefs are attributed above: erasure wins over precision.
            memories = conn.execute(
                "DELETE FROM memories WHERE user_id = ?", (user_id,)
            ).rowcount
            for domain in domains:
                memories += conn.execute(
                    "DELETE FROM memories WHERE source = ?", (f"research:{domain}",)
                ).rowcount
            for brief_id in brief_ids:
                memories += conn.execute(
                    "DELETE FROM memories WHERE source = ?", (f"brief:{brief_id}",)
                ).rowcount
            briefs = 0
            for brief_id in brief_ids:
                briefs += conn.execute(
                    "DELETE FROM briefs WHERE id = ?", (brief_id,)
                ).rowcount
            research = 0
            for domain in domains | brief_domains:
                research += conn.execute(
                    "DELETE FROM company_research WHERE domain = ?", (domain,)
                ).rowcount
            # The audit trail keeps its shape — timestamps, tools, gate results
            # stay, because "what ran" is not personal data. The free-text error
            # column is the one field a provider exception can smuggle an
            # address into, so it alone is scrubbed.
            errors = conn.execute(
                "UPDATE tool_calls SET error = '[purged]' WHERE error != ''"
            ).rowcount

        files = self._purge_brief_files(domains | brief_domains)
        log.info(
            "Purged user %s: %d senders, %d messages, %d memories, %d briefs, "
            "%d research rows, %d delivered files, %d tool-call errors scrubbed",
            user_id, senders, messages, memories, briefs, research, files, errors,
        )
        return senders + messages + memories + briefs + research + files

    @staticmethod
    def _purge_brief_files(domains: set[str]) -> int:
        """Remove delivered briefs for the purged domains from the delivery dir."""
        from .delivery.file import _slug

        directory = SETTINGS.delivery_dir
        if not directory.is_dir() or not domains:
            return 0
        slugs = {_slug(d) for d in domains if d}
        removed = 0
        for path in directory.glob("*.md"):
            # Filenames are "<date>-<time>-<slug>.md"; compare the whole slug,
            # not a substring, so purging "acme.io" never takes "acme.io.vn".
            stem_slug = path.stem.split("-", 2)[-1] if path.stem.count("-") >= 2 else ""
            if stem_slug in slugs:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    log.warning("Could not remove delivered brief %s", path)
        return removed

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
