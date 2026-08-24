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
    # And never a real Langfuse: a test that traces into the developer's
    # project is polluting the same store the eval lane is graded from.
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setenv("LANGFUSE_PROMPTS_ENABLED", "false")
    # Guardrails are on by default in production, but a test that builds a
    # real NeMo engine is a test that downloads models and talks to Ollama.
    # tests/test_guardrails.py opts back in with stubs.
    monkeypatch.setenv("GUARDRAILS_ENABLED", "false")
    # The login wall is on by default in every test; the AUTH_DISABLED bypass is
    # local-dev only, so a test that wants it opts back in explicitly. Without
    # this, a developer's own .env=true would silently open every gated route.
    monkeypatch.setenv("AUTH_DISABLED", "false")

    from companies_research import config as config_module
    from companies_research.config import reload_settings
    from companies_research.tools.registry import RATE

    # reload_settings() re-reads .env with override=True, which would clobber
    # every monkeypatched value above with whatever the developer's .env says.
    # For the duration of a test, "the .env file" is an empty temp path: a test
    # sees process env plus its own monkeypatches, never the developer's state.
    original_env_file = config_module.ENV_FILE
    config_module.ENV_FILE = tmp_path / "test.env"

    reload_settings()
    RATE.reset()
    yield
    RATE.reset()
    config_module.ENV_FILE = original_env_file
    reload_settings()
