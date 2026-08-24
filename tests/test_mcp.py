"""The MCP surface — same pipeline, same gates, different door.

Two properties are load-bearing:

1. Every MCP tool goes through the existing pipeline, so the six-gate harness
   still decides what happens. `deliver_brief` over MCP with the scope off must
   come back as a structured refusal, not a delivery.
2. The HTTP guard is the same wall as the web UI's: unknown Host names are
   refused before the protocol is even spoken, and a configured bearer token is
   required when set.

Everything here is offline — no API key, no mailbox, no network.
"""

from __future__ import annotations

import json

import anyio
import pytest

pytest.importorskip("mcp", reason="the MCP SDK is not installed")

from companies_research.briefs import build_brief
from companies_research.models import EmailAddress, EmailMessage, Relationship, TriageResult
from companies_research.store import Store


def _run(coro):
    return anyio.run(lambda: coro)


def _triage(**over) -> TriageResult:
    base = dict(
        message_id="m1", is_business_contact=True, relationship=Relationship.CUSTOMER,
        company_name="Acme", company_domain="acme.com", should_research=True,
        confidence=0.9, intent_summary="wants a demo",
    )
    base.update(over)
    return TriageResult(**base)


def _message(**over) -> EmailMessage:
    base = dict(
        message_id="m1", provider="imap", account_id="default",
        subject="Partnership with Acme",
        sender=EmailAddress(name="Ann", email="ann@acme.com"),
    )
    base.update(over)
    return EmailMessage(**base)


def _payload(result) -> dict:
    """The JSON a client model actually reads out of a tool result."""
    assert result.content, "tool returned no content"
    return json.loads(result.content[0].text)


@pytest.fixture
def server():
    from companies_research.mcp_server import build_server

    return build_server()


# ---------------------------------------------------------------------------
# surface
# ---------------------------------------------------------------------------


def test_every_pipeline_step_has_a_tool(server):
    tools = {t.name for t in _run(server.list_tools())}
    expected = {
        "get_status", "get_audit_log", "scan_inbox", "triage_messages",
        "list_leads", "seed_known_senders", "research_company", "get_research",
        "lookup_calendar", "generate_brief", "list_briefs", "get_brief",
        "approve_brief", "reject_brief", "deliver_brief",
        "search", "fetch",  # the ChatGPT connector contract
    }
    assert expected <= tools


def test_status_reports_scopes_without_leaking_values(server):
    status = _payload(_run(server.call_tool("get_status", {})))
    assert status["delivery_enabled"] is False  # off by default
    # counts, never addresses: the allow-list itself must not cross the wire
    assert isinstance(status["allowed_recipients"], int)


def test_cli_knows_the_subcommand():
    from companies_research.cli import build_parser

    args = build_parser().parse_args(["mcp", "--http", "--port", "9999"])
    assert args.http and args.port == 9999


# ---------------------------------------------------------------------------
# the store-backed tools, end to end against a seeded database
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_store():
    store = Store()
    store.mark_processed(_message(), _triage())
    brief_id = store.save_brief(build_brief(triage=_triage(), lead_id="lead-1"))
    return store, brief_id


def test_search_and_fetch_roundtrip(server, seeded_store):
    _, brief_id = seeded_store

    results = _payload(_run(server.call_tool("search", {"query": "acme"})))["results"]
    ids = {r["id"] for r in results}
    assert any(i.startswith("lead:") for i in ids)
    assert f"brief:{brief_id}" in ids

    fetched = _payload(_run(server.call_tool("fetch", {"id": f"brief:{brief_id}"})))
    assert "Acme" in fetched["text"]
    assert fetched["metadata"]["status"] == "draft"


def test_fetch_unknown_id_explains_rather_than_errors(server):
    fetched = _payload(_run(server.call_tool("fetch", {"id": "brief:nope"})))
    assert fetched["title"] == "not found"


def test_brief_review_flow(server, seeded_store):
    _, brief_id = seeded_store

    briefs = _payload(_run(server.call_tool("list_briefs", {})))["briefs"]
    assert briefs[0]["brief_id"] == brief_id and briefs[0]["status"] == "draft"

    approved = _payload(_run(server.call_tool(
        "approve_brief", {"brief_id": brief_id, "approved_by": "chris"})))
    assert approved["ok"] is True

    shown = _payload(_run(server.call_tool("get_brief", {"brief_id": brief_id})))
    assert shown["status"] == "approved" and shown["approved_by"] == "chris"


def test_delivered_brief_cannot_be_reopened_over_mcp(server, seeded_store):
    store, brief_id = seeded_store
    store.set_brief_status(brief_id, "approved", approved_by="chris")
    store.set_brief_status(brief_id, "delivered")

    rejected = _payload(_run(server.call_tool(
        "reject_brief", {"brief_id": brief_id, "status": "draft"})))
    assert rejected["ok"] is False


def test_deliver_is_refused_at_the_gate_not_the_prompt(server, seeded_store, monkeypatch):
    """The scope is off by default; an approved brief still must not leave."""
    store, brief_id = seeded_store
    store.set_brief_status(brief_id, "approved", approved_by="chris")

    outcome = _payload(_run(server.call_tool(
        "deliver_brief",
        {"brief_id": brief_id, "recipient": "owner@example.com"})))
    assert outcome["ok"] is False
    assert "denied at scopes" in outcome["error"]

    # and the refusal is observable over MCP itself: the audit row names the
    # gate and the code path (briefs.deliver names itself, as it does for the
    # web UI too), and carries no argument content
    audit = _payload(_run(server.call_tool("get_audit_log", {"denied_only": True})))
    denial = audit["calls"][0]
    assert denial["tool"] == "deliver_brief"
    assert denial["denied_at"] == "scopes"
    assert denial["caller"] == "briefs.deliver"
    assert "owner@example.com" not in str(audit)


# ---------------------------------------------------------------------------
# triage_messages — mail fetched by the client (a Gmail connector, a skill)
# runs through the same triage brain and lands in the same lead store
# ---------------------------------------------------------------------------


class _StubTriageAgent:
    """Answers like the real agent without a model behind it."""

    def __init__(self, backend=None) -> None:
        pass

    def triage(self, messages, progress=None):
        return [
            _triage(
                message_id=m.message_id,
                company_name="Acme", company_domain="acme.com",
            )
            for m in messages
        ]


@pytest.fixture
def stub_triage(monkeypatch):
    monkeypatch.setattr(
        "companies_research.agents.triage.TriageAgent", _StubTriageAgent
    )


def _inbound(**over) -> dict:
    base = dict(sender="ann@acme.com", subject="Partnership",
                body="We'd like a demo next week.")
    base.update(over)
    return base


def test_triage_messages_feeds_the_same_lead_store(server, stub_triage):
    report = _payload(_run(server.call_tool(
        "triage_messages", {"messages": [_inbound()]})))
    assert report["recorded"] is True
    assert report["triaged"][0]["company_domain"] == "acme.com"

    # the bridge is a conversion, not a second pipeline: the lead is now
    # visible to every store-backed tool
    leads = _payload(_run(server.call_tool("list_leads", {})))["leads"]
    assert leads and leads[0]["domain"] == "acme.com"


def test_triage_messages_skips_what_the_store_already_knows(server, stub_triage):
    first = _payload(_run(server.call_tool(
        "triage_messages", {"messages": [_inbound()]})))
    assert first["recorded"] is True

    # same message again → deduped; a colleague of a now-known sender → known
    again = _payload(_run(server.call_tool(
        "triage_messages",
        {"messages": [_inbound(), _inbound(sender="bob@acme.com", subject="Hi")]})))
    assert again["triaged"] == []
    assert again["skipped"] == {"already triaged": 1, "known sender": 1}

    forced = _payload(_run(server.call_tool(
        "triage_messages",
        {"messages": [_inbound(sender="bob@acme.com", subject="Hi")],
         "include_known": True})))
    assert len(forced["triaged"]) == 1


def test_triage_messages_dry_run_records_nothing(server, stub_triage):
    report = _payload(_run(server.call_tool(
        "triage_messages", {"messages": [_inbound()], "dry_run": True})))
    assert report["triaged"] and report["recorded"] is False
    assert _payload(_run(server.call_tool("list_leads", {})))["leads"] == []


# ---------------------------------------------------------------------------
# the HTTP guard
# ---------------------------------------------------------------------------


class _Recorder:
    """Collects what an ASGI app sends, and notes if the inner app ran."""

    def __init__(self):
        self.status = None
        self.inner_ran = False

    async def inner_app(self, scope, receive, send):
        self.inner_ran = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]


def _hit(guard_kwargs: dict, headers: list[tuple[bytes, bytes]]) -> _Recorder:
    from companies_research.mcp_server import _Guard

    recorder = _Recorder()
    guard = _Guard(recorder.inner_app, **guard_kwargs)
    scope = {"type": "http", "headers": headers}

    async def receive():  # pragma: no cover - never pulled by the guard
        return {"type": "http.request"}

    _run(guard(scope, receive, recorder.send))
    return recorder


def test_guard_refuses_unknown_hosts_like_the_web_ui(monkeypatch):
    from companies_research.config import reload_settings

    monkeypatch.setenv("PUBLIC_HOSTS", "*.trycloudflare.com,demo.example.com")
    reload_settings()

    for host, allowed in [
        (b"127.0.0.1:8766", True),
        (b"random-words.trycloudflare.com", True),
        (b"demo.example.com", True),
        (b"evil.example.net", False),
    ]:
        recorder = _hit({"auth_token": ""}, [(b"host", host)])
        assert recorder.inner_ran is allowed, host
        if not allowed:
            assert recorder.status == 421


def test_guard_requires_the_bearer_token_when_set():
    headers = [(b"host", b"127.0.0.1:8766")]
    denied = _hit({"auth_token": "s3cret"}, headers)
    assert denied.status == 401 and not denied.inner_ran

    granted = _hit(
        {"auth_token": "s3cret"},
        headers + [(b"authorization", b"Bearer s3cret")],
    )
    assert granted.inner_ran


def test_guard_lets_non_http_scopes_straight_through():
    """The lifespan scope must reach the app or uvicorn never finishes startup."""
    from companies_research.mcp_server import _Guard

    ran = False

    async def inner(scope, receive, send):
        nonlocal ran
        ran = True

    _run(_Guard(inner, auth_token="s3cret")({"type": "lifespan"}, None, None))
    assert ran


# ---------------------------------------------------------------------------
# drive + memory over MCP: same gates, structured degradation
# ---------------------------------------------------------------------------


def test_memory_write_over_mcp_is_refused_at_the_gate(server, monkeypatch):
    monkeypatch.setenv("TOOL_SCOPES", "memory:read")
    from companies_research.config import reload_settings

    reload_settings()
    outcome = _payload(_run(server.call_tool(
        "save_memory", {"content": "remember this"})))
    assert outcome["ok"] is False
    assert outcome["gate"] == "scopes"


def test_memory_unavailable_degrades_into_an_answer(server, monkeypatch):
    """A dead Ollama must come back as words, not a raw exception."""
    from companies_research import memory

    # With nothing stored, recall answers before embedding — seed one row so
    # the query actually needs the embedder.
    Store().add_memory(text="the user likes tea", embedding=[1.0, 0.0])

    def broken(_texts):
        raise memory.MemoryUnavailable("no Ollama at http://localhost:11434")

    monkeypatch.setattr(memory, "embed_texts", broken)
    outcome = _payload(_run(server.call_tool(
        "search_memory", {"query": "anything"})))
    assert outcome["ok"] is False
    assert "Ollama" in outcome["error"]


def test_drive_over_mcp_reports_missing_credentials(server, monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "no-sa.json"))
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", str(tmp_path / "no-oauth.json"))
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "creds" / "token.json"))
    from companies_research.config import reload_settings

    reload_settings()
    outcome = _payload(_run(server.call_tool("list_drive_files", {})))
    # With no credential anywhere the auth gate refuses before Drive is reached.
    assert outcome["ok"] is False
    assert outcome.get("gate") == "auth" or "credential" in outcome.get("error", "")
