"""The tools this agent may use, and the gates each one sits behind.

Argument models exist to be *checked*, not to be stored: the registry hashes
them and throws the values away. So they carry shapes and identifiers, never
message bodies — a schema that accepted a body would put one in every audit row.

``web_search`` and ``web_fetch`` are unusual: Anthropic runs them server-side
inside a single Messages request, so there is no per-search function for us to
wrap. They are gated at the point where research decides whether to *declare*
them. A revoked scope means the tool is never offered, so the model cannot call
it — enforcement at the boundary rather than filtering afterwards.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .registry import ToolSpec, tool


class WebSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(default="", description="Company name being researched.")
    domain: str = Field(default="", description="Public company domain.")
    max_uses: int = Field(default=1, ge=0)


class WebFetchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str = ""
    max_uses: int = Field(default=1, ge=0)


class GmailReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str
    folder: str = "inbox"
    max_results: int = Field(default=100, ge=1)
    query: str = Field(default="", description="Provider-native query. No message content.")


class StoreWriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(description="processed_message | known_sender | company_research")
    key: str = Field(description="Row key — a uid or domain, never a body.")
    user_id: str = "default"


WEB_SEARCH = ToolSpec(
    name="web_search",
    args_model=WebSearchArgs,
    requires_auth=True,
    scopes=frozenset({"research:read"}),
    rate_limit_per_min=30,
    side_effect=False,
    description="Search the public web for information about a company.",
)

WEB_FETCH = ToolSpec(
    name="web_fetch",
    args_model=WebFetchArgs,
    requires_auth=True,
    scopes=frozenset({"research:read"}),
    rate_limit_per_min=30,
    side_effect=False,
    description="Retrieve a public web page already referenced in the conversation.",
)

GMAIL_READ = ToolSpec(
    name="gmail_read",
    args_model=GmailReadArgs,
    requires_auth=True,
    scopes=frozenset({"mail:read"}),
    rate_limit_per_min=60,
    side_effect=False,
    description="Read messages from a configured mailbox.",
)

# side_effect=True because it writes. The recipient allow-list WO-03 adds to the
# scopes gate applies only to tools that carry a recipient, so a database write
# is audited as a side effect without being checked against an address it does
# not have.
STORE_WRITE = ToolSpec(
    name="store_write",
    args_model=StoreWriteArgs,
    requires_auth=False,
    scopes=frozenset({"memory:write"}),
    rate_limit_per_min=600,
    side_effect=True,
    description="Persist a row to the local SQLite store.",
)


class CalendarReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str = Field(default="", description="Company domain to match attendees against.")
    company: str = Field(default="", description="Company name, for the weaker title match.")
    lookahead_days: int = Field(default=30, ge=1, le=365)


class DeliverBriefArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brief_id: str
    recipient: str = Field(description="Where the brief is sent. Checked against ALLOWED_RECIPIENTS.")
    note: str = Field(default="", max_length=2000)


# `brief:deliver` is off by default, so a call is refused at the scopes gate
# whatever the arguments say, and the recipient allow-list is checked on top of
# that. Both run before the provider is ever reached.
DELIVER_BRIEF = ToolSpec(
    name="deliver_brief",
    args_model=DeliverBriefArgs,
    requires_auth=False,
    scopes=frozenset({"brief:deliver"}),
    rate_limit_per_min=10,
    side_effect=True,
    description="Send an approved brief to a recipient.",
)


CALENDAR_READ = ToolSpec(
    name="calendar_read",
    args_model=CalendarReadArgs,
    requires_auth=True,
    scopes=frozenset({"calendar:read"}),
    rate_limit_per_min=30,
    side_effect=False,
    description="List upcoming events that involve a company.",
)


@tool(CALENDAR_READ)
def calendar_read(*, domain: str = "", company: str = "", lookahead_days: int = 30,
                  _look: Any = None) -> Any:
    if _look is None:
        raise ValueError("calendar_read requires a _look dependency")
    return _look()


@tool(DELIVER_BRIEF)
def deliver_brief(*, brief_id: str, recipient: str, note: str = "",
                  _deliver: Any = None) -> Any:
    if _deliver is None:
        raise ValueError("deliver_brief requires a _deliver dependency")
    return _deliver()


@tool(WEB_SEARCH)
def web_search(*, company: str = "", domain: str = "", max_uses: int = 1) -> bool:
    """Gate-only: permission to declare the hosted search tool for this lookup."""
    return True


@tool(WEB_FETCH)
def web_fetch(*, domain: str = "", max_uses: int = 1) -> bool:
    """Gate-only: permission to declare the hosted fetch tool for this lookup."""
    return True


@tool(GMAIL_READ)
def gmail_read(*, account_id: str, folder: str = "inbox", max_results: int = 100,
               query: str = "", _fetch: Any = None) -> Any:
    """Run the provider's fetch behind the gate. ``_fetch`` is the bound call."""
    if _fetch is None:
        raise ValueError("gmail_read requires a _fetch dependency")
    return _fetch()


@tool(STORE_WRITE)
def store_write(*, kind: str, key: str, user_id: str = "default", _write: Any = None) -> Any:
    if _write is None:
        raise ValueError("store_write requires a _write dependency")
    return _write()


# --- chat demo tools: Drive + long-term memory ------------------------------
# These four exist for the interactive `chat` command. save_memory is the one
# schema here that carries content, because content is the thing being saved —
# the registry hashes arguments before auditing, so the body still never lands
# in a tool_calls row. The memories table is its intended destination.


class DriveListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder_id: str = Field(default="", description="Drive folder to list; empty = default folder.")
    page_size: int = Field(default=50, ge=1, le=200)


class DriveReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str = Field(min_length=1, description="Drive file ID, from list_drive_files.")


class MemorySaveArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=100_000)
    category: str = Field(default="general", max_length=64)
    source: str = Field(default="", max_length=256, description="Where this came from, e.g. a file name.")


class MemorySearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


LIST_DRIVE_FILES = ToolSpec(
    name="list_drive_files",
    args_model=DriveListArgs,
    requires_auth=True,
    scopes=frozenset({"drive:read"}),
    rate_limit_per_min=30,
    side_effect=False,
    description="List files in Google Drive: names, IDs, types, sizes, modification times.",
)

READ_DRIVE_FILE = ToolSpec(
    name="read_drive_file",
    args_model=DriveReadArgs,
    requires_auth=True,
    scopes=frozenset({"drive:read"}),
    rate_limit_per_min=30,
    side_effect=False,
    description=(
        "Read one Drive file as Markdown (PDF, DOCX, XLSX, PPTX, Google Docs/"
        "Sheets/Slides, text). Use list_drive_files first to get the file ID."
    ),
)

SAVE_MEMORY = ToolSpec(
    name="save_memory",
    args_model=MemorySaveArgs,
    requires_auth=False,
    scopes=frozenset({"memory:write"}),
    rate_limit_per_min=60,
    side_effect=True,
    description=(
        "Save information to long-term memory (survives restarts). Use for user "
        "preferences, key facts, and file contents the user asks to keep."
    ),
)

SEARCH_MEMORY = ToolSpec(
    name="search_memory",
    args_model=MemorySearchArgs,
    requires_auth=False,
    scopes=frozenset({"memory:read"}),
    rate_limit_per_min=60,
    side_effect=False,
    description=(
        "Semantic search over long-term memory. Use before answering anything "
        "about the user's preferences or past conversations."
    ),
)


# Unlike the provider-bound tools above, these default to the real module-level
# service when no dependency is injected: there is exactly one Drive client and
# one memory store per process, so the injection point exists for tests, not
# for wiring.


@tool(LIST_DRIVE_FILES)
def list_drive_files(*, folder_id: str = "", page_size: int = 50,
                     _list: Any = None) -> Any:
    if _list is None:
        from .. import drive

        _list = drive.list_files
    return _list(folder_id=folder_id, page_size=page_size)


@tool(READ_DRIVE_FILE)
def read_drive_file(*, file_id: str, _read: Any = None) -> Any:
    if _read is None:
        from .. import drive

        _read = drive.read_file_markdown
    result = dict(_read(file_id))

    # A Drive file is stranger-writable the same way an email body is, and it
    # gets the same two-layer treatment triage gives mail: the NeMo input rail
    # advises (and degrades to nothing where NeMo cannot run), then the content
    # is fenced with a random-tag untrusted block the payload cannot forge its
    # way out of. The gates stay the real boundary either way.
    from ..config import SETTINGS
    from ..prompts import render_untrusted

    if SETTINGS.guardrails_enabled:
        from ..agents import rails

        rail = rails.get_input_rail()
        if rail is not None and rail.screen(result.get("content", "")[:4000]):
            result["screening"] = ("guardrails flagged this file as a possible "
                                   "prompt-injection attempt (advisory)")
    result["content"] = render_untrusted(result.get("content", ""), kind="drive-file")
    return result


@tool(SAVE_MEMORY)
def save_memory(*, content: str, category: str = "general", source: str = "",
                _save: Any = None) -> Any:
    if _save is None:
        from .. import memory

        _save = memory.remember
    return _save(content, category=category, source=source)


@tool(SEARCH_MEMORY)
def search_memory(*, query: str, top_k: int = 5, _search: Any = None) -> Any:
    if _search is None:
        from .. import memory

        _search = memory.recall
    return _search(query, top_k=top_k)


# --- chat views of the agent's own state ------------------------------------
# What the dashboard and MCP server already show, offered to the chat model —
# reads of the local store, gated so every look still leaves an audit row.


class LeadsListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, ge=1, le=50)
    only_research: bool = Field(default=False, description="Only leads worth researching.")


class BriefsListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(default="", description="draft | approved | delivered; empty = all.")
    limit: int = Field(default=10, ge=1, le=50)


class ResearchGetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str = Field(min_length=1, description="Company domain, e.g. acme.com.")


LIST_LEADS = ToolSpec(
    name="list_leads",
    args_model=LeadsListArgs,
    requires_auth=False,
    scopes=frozenset({"mail:read"}),
    rate_limit_per_min=60,
    side_effect=False,
    description="Leads found in scanned mail: sender, company, relationship, intent.",
)

LIST_BRIEFS = ToolSpec(
    name="list_briefs",
    args_model=BriefsListArgs,
    requires_auth=False,
    scopes=frozenset({"research:read"}),
    rate_limit_per_min=60,
    side_effect=False,
    description="Generated company briefs and their approval status.",
)

GET_RESEARCH = ToolSpec(
    name="get_research",
    args_model=ResearchGetArgs,
    requires_auth=False,
    scopes=frozenset({"research:read"}),
    rate_limit_per_min=60,
    side_effect=False,
    description="The cached research profile for one company domain, if any.",
)


@tool(LIST_LEADS)
def list_leads(*, limit: int = 10, only_research: bool = False,
               _leads: Any = None) -> Any:
    if _leads is None:
        from ..store import Store

        _leads = lambda: Store().recent_leads(limit=limit, only_research=only_research)  # noqa: E731
    rows = _leads()
    leads = [
        {
            "from": row.get("sender_email", ""),
            "subject": row.get("subject", ""),
            "received_at": row.get("received_at"),
            "company": (row.get("triage") or {}).get("company_name", ""),
            "domain": (row.get("triage") or {}).get("company_domain", ""),
            "relationship": (row.get("triage") or {}).get("relationship", ""),
            "intent": (row.get("triage") or {}).get("intent_summary", ""),
            "researched": row.get("research") is not None,
        }
        for row in rows[:limit]
    ]
    return {"total": len(leads), "leads": leads}


@tool(LIST_BRIEFS)
def list_briefs(*, status: str = "", limit: int = 10, _briefs: Any = None) -> Any:
    if _briefs is None:
        from ..store import Store

        _briefs = lambda: Store().list_briefs(status=status or None, limit=limit)  # noqa: E731
    rows = _briefs()
    briefs = [
        {
            "brief_id": row["id"],
            "company": row.get("company", ""),
            "domain": row.get("domain", ""),
            "status": row.get("status", ""),
            "generated_at": row.get("generated_at"),
        }
        for row in rows
    ]
    return {"total": len(briefs), "briefs": briefs}


@tool(GET_RESEARCH)
def get_research(*, domain: str, _research: Any = None) -> Any:
    if _research is None:
        from ..store import Store

        _research = lambda: Store().get_research(domain)  # noqa: E731
    record = _research()
    if not record:
        return {"found": False, "domain": domain,
                "hint": "No cached research. A scan or `research` run creates it."}
    if not record.get("ok", True) or not record.get("profile"):
        return {"found": False, "domain": domain,
                "error": record.get("error") or "last research attempt failed"}
    profile = record.get("profile") or {}
    if hasattr(profile, "model_dump"):
        profile = profile.model_dump(mode="json")
    profile = dict(profile)
    profile.pop("field_sources", None)  # per-claim provenance is UI detail
    return {"found": True, "domain": domain,
            "researched_at": record.get("researched_at"), "profile": profile}
