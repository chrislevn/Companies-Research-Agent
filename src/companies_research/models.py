"""Shared data models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EmailAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    email: str = ""

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1].lower() if "@" in self.email else ""


class EmailMessage(BaseModel):
    """One message from any provider, normalised to what the agent needs.

    ``message_id`` is only unique within a single mailbox, so anything that
    persists or dedupes messages must key on :attr:`uid`.
    """

    model_config = ConfigDict(extra="forbid")

    message_id: str
    thread_id: str = ""
    provider: str = ""
    account_id: str = ""
    account_email: str = ""
    subject: str = ""
    sender: EmailAddress
    to: list[EmailAddress] = Field(default_factory=list)
    cc: list[EmailAddress] = Field(default_factory=list)
    received_at: datetime | None = None
    snippet: str = ""
    body_text: str = ""
    labels: list[str] = Field(default_factory=list)
    is_automated: bool = False
    signature_block: str = ""

    @property
    def uid(self) -> str:
        """Globally unique key: provider + account + provider-local id."""
        return f"{self.provider}:{self.account_id}:{self.message_id}"

    def short_body(self, limit: int = 4000) -> str:
        body = self.body_text.strip() or self.snippet
        return body[:limit]


class Relationship(str, Enum):
    CUSTOMER = "customer"          # khách hàng / prospect
    PARTNER = "partner"            # đối tác
    VENDOR = "vendor"              # nhà cung cấp
    INTERNAL = "internal"          # đồng nghiệp, nội bộ
    RECRUITER = "recruiter"
    NEWSLETTER = "newsletter"
    AUTOMATED = "automated"        # notification, no-reply, system mail
    PERSONAL = "personal"
    UNKNOWN = "unknown"


class TriageResult(BaseModel):
    """What the triage agent decides about one email."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    is_business_contact: bool = Field(
        description="True if a real person is reaching out on behalf of a company."
    )
    relationship: Relationship
    company_name: str = Field(default="", description="Company name, empty if unknown.")
    company_domain: str = Field(
        default="", description="Best-guess website domain, e.g. acme.com. Empty if unknown."
    )
    contact_name: str = ""
    contact_title: str = ""
    contact_phone: str = ""
    intent_summary: str = Field(
        default="", description="One sentence: what does this person want?"
    )
    mentions_meeting: bool = Field(
        default=False, description="True if the email proposes, confirms or references a meeting."
    )
    should_research: bool = Field(
        description="True if this company is worth an automated research brief."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(default="", description="Short justification, max 2 sentences.")


class TriageBatch(BaseModel):
    """Envelope so the model can classify several emails in one call."""

    model_config = ConfigDict(extra="forbid")

    results: list[TriageResult]


class MeetingRef(BaseModel):
    """An upcoming calendar event that looks like it involves this company.

    ``matched_on`` is kept because *why* an event matched decides how much to
    trust it: sharing an attendee domain is near-certain, a company name in a
    title is a guess that a brief should present as one.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    title: str = ""
    starts_at: datetime
    attendees: list[str] = Field(default_factory=list)
    matched_on: Literal["attendee_domain", "organizer_domain", "title_mention"]
    confidence: float = Field(ge=0.0, le=1.0)


class NewsItem(BaseModel):
    """One recent story about the company."""

    model_config = ConfigDict(extra="forbid")

    title: str
    url: str = Field(default="", description="Link to the story. Empty if not known.")
    published: str = Field(
        default="", description="Publication date as written on the page, e.g. 2026-05-14."
    )
    summary: str = Field(default="", description="One sentence on why it matters.")


class CompanyProfile(BaseModel):
    """What research finds out about one company.

    Deliberately carries no timestamps or cache bookkeeping: those belong to the
    store, and a field the model has to fill is a field the model can invent.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Company name as it presents itself.")
    domain: str = Field(default="", description="Primary website domain.")
    one_liner: str = Field(default="", description="What the company does, in one sentence.")
    description: str = Field(default="", description="Two or three sentences of detail.")
    products: list[str] = Field(
        default_factory=list, description="Named products or services."
    )
    industry: str = ""
    hq_location: str = Field(default="", description="City and country of headquarters.")
    size_estimate: str = Field(
        default="", description="Employee count or band, e.g. '50-200'. Empty if unknown."
    )
    founded: str = Field(default="", description="Year founded. Empty if unknown.")
    news: list[NewsItem] = Field(
        default_factory=list, description="Recent and relevant stories, newest first."
    )
    meeting_prep: list[str] = Field(
        default_factory=list,
        description="Talking points and questions worth raising in a meeting with them.",
    )
    sources: list[str] = Field(
        default_factory=list, description="URLs the findings came from."
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="How well-evidenced this profile is."
    )
    notes: str = Field(
        default="", description="Anything ambiguous, e.g. two companies share this name."
    )
