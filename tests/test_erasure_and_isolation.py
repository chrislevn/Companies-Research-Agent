"""The three promises the release audit caught overstating, now enforced.

Each test here pins a sentence in the docs to behaviour in the code:

* "purge deletes everything derived from that person's mail" — so purging must
  reach the tables and files that carry no user_id column, by attribution.
* senders never appear in INFO logs — because INFO is mirrored into the web
  job pane and persisted by the container deploy.
* settings, the API key and purge answer only to the first account — because
  during a SIGNUP_OPEN window every later account is a visitor, not an operator.
"""

from __future__ import annotations

import logging
import pathlib

import pytest

from companies_research.config import SETTINGS
from companies_research.models import EmailAddress, EmailMessage
from companies_research.store import Store


def _email(sender: str = "linh@acme-corp.example") -> EmailMessage:
    return EmailMessage(
        message_id="m-1", thread_id="t-1", subject="Enquiry from Acme",
        sender=EmailAddress(name="Linh Tran", email=sender),
        to=[EmailAddress(email="owner@example.com")],
        body_text="We would like a demo.", snippet="We would like a demo.",
        received_at=None,
    )


def _seed_everything(store: Store) -> pathlib.Path:
    """One user's full data trail: sender, message, brief, research, error, file."""
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO known_senders (user_id, email, domain, first_seen_at, "
            "last_seen_at) VALUES ('default', 'linh@acme-corp.example', "
            "'acme-corp.example', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO processed_messages (uid, user_id, message_id, "
            "sender_email, subject, processed_at) VALUES ('u-1', 'default', "
            "'m-1', 'linh@acme-corp.example', 'Enquiry from Acme', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO briefs (id, lead_id, domain, company, generated_at, "
            "brief_json) VALUES ('b-1', 'acme-corp.example:m-1', "
            "'acme-corp.example', 'Acme', '2026-01-02', '{}')"
        )
        conn.execute(
            "INSERT INTO company_research (domain, company_name, researched_at, "
            "profile_json) VALUES ('acme-corp.example', 'Acme', '2026-01-02', '{}')"
        )
        conn.execute(
            "INSERT INTO tool_calls (id, ts, tool, caller, args_hash, error) "
            "VALUES ('tc-1', '2026-01-02', 'gmail_read', 'scan', 'x', "
            "'IMAP said: mailbox linh@acme-corp.example is locked')"
        )
        conn.execute(
            "INSERT INTO memories (id, user_id, text, embedding, created_at) "
            "VALUES ('mem-1', 'default', 'Linh at Acme asked for a demo', "
            "'[0.1]', '2026-01-02')"
        )
    delivered = SETTINGS.delivery_dir
    delivered.mkdir(parents=True, exist_ok=True)
    path = delivered / "20260102-090000-acme-corp-example.md"
    path.write_text("<!-- prepared for: someone -->\ncontact: Linh Tran\n")
    return path


def test_purge_reaches_every_mail_derived_row_and_file():
    store = Store()
    delivered = _seed_everything(store)

    removed = store.purge_user("default")

    assert removed >= 5, "sender + message + brief + research + file at minimum"
    with store._connect() as conn:
        for table in ("known_senders", "processed_messages", "briefs",
                      "company_research", "memories"):
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert count == 0, f"{table} still holds {count} row(s) after purge"
        error = conn.execute("SELECT error FROM tool_calls").fetchone()["error"]
        assert "acme-corp" not in error and error == "[purged]"
        # The audit trail's shape survives: what ran is not personal data.
        assert conn.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"] == 1
    assert not delivered.exists(), "the delivered brief file must go too"


def test_purge_matches_delivered_files_by_whole_slug():
    """Purging acme.io must never take acme.io.vn's brief with it."""
    store = Store()
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO known_senders (user_id, email, domain, first_seen_at, "
            "last_seen_at) VALUES ('default', 'a@acme.io', 'acme.io', "
            "'2026-01-01', '2026-01-01')"
        )
    directory = SETTINGS.delivery_dir
    directory.mkdir(parents=True, exist_ok=True)
    mine = directory / "20260101-000000-acme-io.md"
    other = directory / "20260101-000000-acme-io-vn.md"
    mine.write_text("x")
    other.write_text("x")

    store.purge_user("default")

    assert not mine.exists()
    assert other.exists(), "a different company's brief survived the purge"


# --- INFO never carries an address or a subject ------------------------------


class _StubBackend:
    name = "stub"
    model = "stub"

    def describe(self) -> str:
        return "stub"

    def complete(self, *, system: str, user: str, schema: dict):
        from companies_research.agents.backends import Completion

        return Completion(error="stub declines")   # fallback path logs too


def test_no_address_or_subject_reaches_info_logs(caplog):
    from companies_research.agents.triage import TriageAgent

    agent = TriageAgent(backend=_StubBackend())
    agent.batch_size = 1
    with caplog.at_level(logging.INFO, logger="companies_research"):
        agent.triage([_email()])

    info_text = " ".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.INFO
    )
    assert "@" not in info_text, f"an address reached INFO: {info_text[:200]}"
    assert "Enquiry from Acme" not in info_text, "subjects stay out of INFO"
    assert "Linh Tran" not in info_text, "names stay out of INFO"


def test_the_sender_token_is_stable_and_opaque():
    from companies_research.agents.triage import _who

    a, b = _who(_email()), _who(_email())
    assert a == b, "'this sender again' must survive hashing"
    assert "linh" not in a and "@" not in a


# --- owner-only settings -----------------------------------------------------


def test_only_the_first_account_is_the_owner(monkeypatch):
    from companies_research.webapp import auth

    monkeypatch.setenv("SIGNUP_OPEN", "true")
    from companies_research.config import reload_settings

    reload_settings()
    first = auth.create_user(email="owner@example.com", password="a-strong-one-1")
    second = auth.create_user(email="visitor@example.com", password="a-strong-one-2")

    assert auth.is_owner(first) is True
    assert auth.is_owner(second) is False


def test_settings_endpoints_refuse_non_owner_sessions(monkeypatch):
    """The DEPLOYMENT.md claim, enforced: a visitor cannot widen the gates."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from companies_research.webapp import auth, server

    monkeypatch.setenv("SIGNUP_OPEN", "true")
    from companies_research.config import reload_settings

    reload_settings()
    auth.create_user(email="owner@example.com", password="a-strong-one-1")
    visitor = auth.create_user(email="visitor@example.com", password="a-strong-one-2")

    request = SimpleNamespace(state=SimpleNamespace(user=visitor))
    with pytest.raises(HTTPException) as caught:
        server._require_owner(request)
    assert caught.value.status_code == 403

    owner_request = SimpleNamespace(state=SimpleNamespace(user=auth.authenticate(
        email="owner@example.com", password="a-strong-one-1")))
    server._require_owner(owner_request)   # must not raise
