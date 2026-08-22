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
