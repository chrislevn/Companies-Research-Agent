"""Approval and delivery — step 5.

Two properties are load-bearing here, and both are about what the agent
*cannot* do:

1. The mailbox it reads is never the mailbox it sends from. An agent that reads
   untrusted email and can send from the same box is a mail relay with a
   language model choosing the recipient.
2. Approval is a record that a human read something. It is not authority to
   send: the gate is consulted on every delivery regardless of who clicked.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from companies_research.briefs import build_brief, deliver
from companies_research.delivery import DeliveryError, build_delivery
from companies_research.delivery.file import FileDelivery
from companies_research.models import Relationship, TriageResult


def _triage(**over) -> TriageResult:
    base = dict(
        message_id="m1", is_business_contact=True, relationship=Relationship.CUSTOMER,
        company_name="Acme", company_domain="acme.com", should_research=True, confidence=0.9,
    )
    base.update(over)
    return TriageResult(**base)


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.setenv("DELIVERY_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ALLOWED_RECIPIENTS", "owner@example.com")
    from companies_research.config import reload_settings
    from companies_research.store import Store

    reload_settings()
    return Store()


def _approved(store, **over):
    brief = build_brief(triage=_triage(**over), lead_id="lead-1")
    brief_id = store.save_brief(brief)
    store.set_brief_status(brief_id, "approved", approved_by="chris")
    return brief_id


# --- the default is local ---------------------------------------------------


def test_default_provider_does_not_leave_the_machine():
    """Sending is a different kind of act; the safe option is the default."""
    provider = build_delivery()
    assert provider.name == "file"
    assert provider.leaves_machine is False


def test_file_delivery_writes_the_brief(store, monkeypatch):
    monkeypatch.setenv("TOOL_SCOPES", "brief:deliver")
    from companies_research.config import reload_settings

    reload_settings()
    brief_id = _approved(store)
    out = deliver(brief_id=brief_id, recipient="owner@example.com",
                  note="Worth a call.", store=store)
    assert out.ok, out.error
    written = open(out.destination, encoding="utf-8").read()
    assert "Acme" in written
    assert "prepared for: owner@example.com" in written
    assert "approved by: chris" in written
    assert "Worth a call." in written
    assert store.get_brief(brief_id)["status"] == "delivered"


# --- approval is not authority to send --------------------------------------


def test_a_draft_cannot_be_delivered(store, monkeypatch):
    monkeypatch.setenv("TOOL_SCOPES", "brief:deliver")
    from companies_research.config import reload_settings

    reload_settings()
    brief_id = store.save_brief(build_brief(triage=_triage(), lead_id="lead-2"))
    out = deliver(brief_id=brief_id, recipient="owner@example.com", store=store)
    assert not out.ok
    assert "not approved" in out.error


def test_scope_off_blocks_delivery_but_keeps_the_approval(store, monkeypatch):
    """The normal case: brief:deliver is off, so approving records and stops."""
    monkeypatch.setenv("TOOL_SCOPES", "mail:read")
    from companies_research.config import reload_settings

    reload_settings()
    brief_id = _approved(store)
    out = deliver(brief_id=brief_id, recipient="owner@example.com", store=store)
    assert not out.ok
    assert "denied at scopes" in out.error
    assert store.get_brief(brief_id)["status"] == "approved", "the approval still stands"


@pytest.mark.parametrize("recipient", [
    "exfil@attacker.example",
    "Finance Team <exfil@attacker.example>",
    "OWNER@EXAMPLE.COM.attacker.example",
])
def test_allowlist_blocks_delivery_even_with_the_scope_granted(store, monkeypatch, recipient):
    monkeypatch.setenv("TOOL_SCOPES", "brief:deliver")
    from companies_research.config import reload_settings

    reload_settings()
    brief_id = _approved(store)
    out = deliver(brief_id=brief_id, recipient=recipient, store=store)
    assert not out.ok
    assert "denied at scopes" in out.error
    assert store.get_brief(brief_id)["status"] == "approved"


def test_allow_listed_recipient_is_matched_case_insensitively(store, monkeypatch):
    monkeypatch.setenv("TOOL_SCOPES", "brief:deliver")
    from companies_research.config import reload_settings

    reload_settings()
    brief_id = _approved(store)
    assert deliver(brief_id=brief_id, recipient="Owner@Example.COM", store=store).ok


# --- the reading mailbox is never a sending mailbox -------------------------


def test_gmail_send_refuses_without_a_separate_account(monkeypatch):
    monkeypatch.setenv("DELIVERY_PROVIDER", "gmail_send")
    monkeypatch.setenv("DELIVERY_ACCOUNT", "")
    from companies_research.config import reload_settings

    reload_settings()
    with pytest.raises(DeliveryError) as exc:
        build_delivery()
    assert "DELIVERY_ACCOUNT" in str(exc.value)


def test_gmail_send_refuses_to_use_a_mailbox_the_agent_reads(monkeypatch):
    """The guard that keeps a compromised triage from becoming outbound mail."""
    from companies_research.providers.base import Account

    monkeypatch.setenv("DELIVERY_PROVIDER", "gmail_send")
    monkeypatch.setenv("DELIVERY_ACCOUNT", "reading-box")
    monkeypatch.setattr(
        "companies_research.accounts.load_accounts",
        lambda: [Account(account_id="reading-box", provider="gmail", email="me@example.com")],
    )
    from companies_research.config import reload_settings

    reload_settings()
    with pytest.raises(DeliveryError) as exc:
        build_delivery()
    assert "mailbox this agent reads" in str(exc.value)


def test_a_separate_account_is_accepted(monkeypatch):
    from companies_research.providers.base import Account

    monkeypatch.setenv("DELIVERY_PROVIDER", "gmail_send")
    monkeypatch.setenv("DELIVERY_ACCOUNT", "sending-box")
    monkeypatch.setattr(
        "companies_research.accounts.load_accounts",
        lambda: [Account(account_id="reading-box", provider="gmail", email="me@example.com")],
    )
    from companies_research.config import reload_settings

    reload_settings()
    provider = build_delivery()
    assert provider.leaves_machine is True


def test_send_scope_is_not_added_to_the_shared_google_scopes():
    """The reading token must never acquire send permission."""
    from companies_research.config import GOOGLE_SCOPES

    assert not any("gmail.send" in s for s in GOOGLE_SCOPES)
    assert not any("gmail.compose" in s for s in GOOGLE_SCOPES)
    assert not any("gmail.modify" in s for s in GOOGLE_SCOPES)


# --- state machine ----------------------------------------------------------


def test_a_delivered_brief_cannot_be_approved_again(store, monkeypatch):
    """Re-approving would quietly erase the record that it was sent."""
    monkeypatch.setenv("TOOL_SCOPES", "brief:deliver")
    from companies_research.config import reload_settings

    reload_settings()
    brief_id = _approved(store)
    assert deliver(brief_id=brief_id, recipient="owner@example.com", store=store).ok
    assert store.get_brief(brief_id)["status"] == "delivered"

    assert store.set_brief_status(brief_id, "approved", approved_by="someone") is False
    assert store.get_brief(brief_id)["status"] == "delivered"


def test_a_rejected_brief_cannot_be_approved(store):
    brief_id = store.save_brief(build_brief(triage=_triage(), lead_id="lead-3"))
    assert store.set_brief_status(brief_id, "rejected", approved_by="chris")
    assert store.set_brief_status(brief_id, "approved", approved_by="chris") is False
    assert store.get_brief(brief_id)["status"] == "rejected"


def test_approval_provenance_is_recorded(store):
    brief_id = store.save_brief(build_brief(triage=_triage(), lead_id="lead-4"))
    store.set_brief_status(brief_id, "approved", approved_by="chris")
    record = store.get_brief(brief_id)
    assert record["approved_by"] == "chris"
    assert record["approved_at"]


def test_approval_does_not_promote_claims_into_the_research_cache(store, monkeypatch):
    """A human clicking approve is not evidence that a claim is true."""
    monkeypatch.setenv("TOOL_SCOPES", "brief:deliver")
    from companies_research.config import reload_settings

    reload_settings()
    before = store.research_count()
    brief_id = _approved(store)
    deliver(brief_id=brief_id, recipient="owner@example.com", store=store)
    assert store.research_count() == before, "approval must not write research"


# --- delivery failures degrade ---------------------------------------------


def test_delivery_failure_is_reported_not_raised(store, monkeypatch):
    monkeypatch.setenv("TOOL_SCOPES", "brief:deliver")
    monkeypatch.setenv("DELIVERY_DIR", "/proc/nonexistent/nope")
    from companies_research.config import reload_settings

    reload_settings()
    brief_id = _approved(store)
    out = deliver(brief_id=brief_id, recipient="owner@example.com", store=store)
    assert not out.ok and out.error
    assert store.get_brief(brief_id)["status"] == "approved", "a failure is not a delivery"


def test_missing_brief_is_reported(store):
    out = deliver(brief_id="nope", recipient="owner@example.com", store=store)
    assert not out.ok and "no brief" in out.error
