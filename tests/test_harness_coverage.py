"""Does every gated capability actually go *through* the gate?

The six gates are only worth as much as their coverage. A registry with a
perfect rate limiter and a complete audit trail protects nothing if one code
path calls the provider directly, and that is exactly the bug this file exists
to catch: `seed_known_senders` read months of inbox *and sent* mail straight
off the provider, unscoped and unaudited, while the scan beside it went
through `gmail_read`.

Two kinds of test here. The behavioural one proves the gate is consulted. The
shape guard proves no *new* bypass gets added later, which is the failure mode
that matters — a bypass is invisible in review because the code that skips the
gate looks like ordinary code.
"""

from __future__ import annotations

import pathlib
import re
from datetime import timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "companies_research"


# --- shape guard ------------------------------------------------------------


def test_no_capability_is_called_outside_its_tool():
    """Every provider fetch must be a `_fetch=` dependency handed to the gate."""
    offenders = []
    for path in SRC.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if not re.search(r"\bprovider\.fetch\(", line):
                continue
            if "_fetch=" in line:
                continue                      # bound and handed to gmail_read
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "mail is read without passing the tool gate:\n  " + "\n  ".join(offenders)
    )


# --- behavioural ------------------------------------------------------------


class _FakeProvider:
    """Stands in for a mailbox. Records whether it was ever actually read."""

    def __init__(self) -> None:
        self.reads = 0

    def __enter__(self) -> "_FakeProvider":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def fetch(self, _query: object) -> list:
        self.reads += 1
        return []


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    """A seed run against a fake mailbox, with the store pointed at tmp."""
    from companies_research.providers.base import Account

    monkeypatch.setenv("DB_PATH", str(tmp_path / "seed.db"))
    provider = _FakeProvider()
    monkeypatch.setattr(
        "companies_research.pipeline.load_accounts",
        lambda: [Account(account_id="box", provider="gmail", email="me@example.com")],
    )
    monkeypatch.setattr(
        "companies_research.pipeline.build_provider", lambda _a: provider
    )
    return provider


def _run_seed(tmp_path):
    from companies_research.config import reload_settings
    from companies_research.pipeline import seed_known_senders
    from companies_research.store import Store

    reload_settings()
    seed_known_senders(since=timedelta(days=30), store=Store())
    return Store()


def test_seeding_is_refused_when_mail_read_is_revoked(seeded, monkeypatch, tmp_path):
    """Revoking mail:read must stop the largest read in the system."""
    monkeypatch.setenv("TOOL_SCOPES", "memory:write")
    _run_seed(tmp_path)
    assert seeded.reads == 0, "the mailbox was read despite mail:read being revoked"


def test_a_refused_seed_leaves_an_audit_row_naming_the_gate(seeded, monkeypatch, tmp_path):
    monkeypatch.setenv("TOOL_SCOPES", "memory:write")
    monkeypatch.setenv("TOOL_AUDIT_ENABLED", "1")
    store = _run_seed(tmp_path)
    rows = store.recent_tool_calls(limit=50, tool="gmail_read")
    assert rows, "a denied bulk mail read left no trace at all"
    assert all(r["denied_at"] == "scopes" for r in rows)
    assert all(r["caller"] == "pipeline.seed" for r in rows)


def test_a_permitted_seed_reads_and_is_audited(seeded, monkeypatch, tmp_path):
    monkeypatch.setenv("TOOL_SCOPES", "mail:read,memory:write")
    monkeypatch.setenv("TOOL_AUDIT_ENABLED", "1")
    store = _run_seed(tmp_path)
    assert seeded.reads == 2, "seeding should read both inbox and sent"
    rows = store.recent_tool_calls(limit=50, tool="gmail_read")
    assert len(rows) == 2
    assert all(r["ok"] and not r["denied_at"] for r in rows)


def test_the_audit_row_records_no_message_content(seeded, monkeypatch, tmp_path):
    """A bulk read must be provable without copying the mailbox into the log."""
    monkeypatch.setenv("TOOL_SCOPES", "mail:read,memory:write")
    monkeypatch.setenv("TOOL_AUDIT_ENABLED", "1")
    store = _run_seed(tmp_path)
    for row in store.recent_tool_calls(limit=50):
        assert "@" not in (row["args_hash"] or ""), "an address reached the audit log"
