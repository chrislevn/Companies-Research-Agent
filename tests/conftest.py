"""Isolation every test gets whether it asks for it or not.

A test that writes to the developer's real database is a bug in the test, and
relying on each test to remember `monkeypatch.setenv("DB_PATH", ...)` is relying
on nobody ever forgetting. This was not hypothetical: before this file existed,
392 of the 500 rows in the live audit table had been written by the test suite,
which both corrupts the operator's own audit trail and makes the demo data
meaningless.

The same reasoning covers delivery: a test must never be able to write a brief
into the real out/briefs, and must never be one misconfiguration away from
sending one.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Point every stateful path at a temp directory for the duration."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DELIVERY_DIR", str(tmp_path / "briefs"))
    monkeypatch.setenv("PROMPTS_DIR", str(tmp_path / "prompts"))
    # Never let a test reach a real mailbox, calendar or delivery account.
    monkeypatch.setenv("ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("DELIVERY_PROVIDER", "file")
    monkeypatch.setenv("DELIVERY_ACCOUNT", "")
    # Metrics bind a port; a suite that starts servers is a suite that flakes.
    monkeypatch.setenv("METRICS_ENABLED", "false")
    monkeypatch.setenv("TRACING_ENABLED", "false")

    from companies_research.config import reload_settings
    from companies_research.tools.registry import RATE

    reload_settings()
    RATE.reset()
    yield
    RATE.reset()
    reload_settings()
