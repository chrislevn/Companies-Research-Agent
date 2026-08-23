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


class OrgProfile(BaseModel):
    """Who *you* are — the missing half of every relevance judgement.

    Without this the agent can only ask "is this a real person from a company".
    With it, it can ask the question that actually matters: "is this company
    worth *our* time". A logistics firm and a design studio get identical
    inbound mail and should reach opposite conclusions about most of it.

    Deliberately small. This is a page of text that goes into the prompt whole —
    there is no retrieval step, because there is nothing to retrieve from. A
    vector store for six paragraphs would add moving parts and answer no
    question that including the text does not already answer.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", description="Your company name.")
    domain: str = Field(default="", description="Your website domain.")
    what_we_do: str = Field(
        default="", description="What you sell, in a sentence or two."
    )
    ideal_customer: str = Field(
        default="", description="Who you want to hear from — the profile of a good lead."
    )
    target_industries: list[str] = Field(default_factory=list)
    target_regions: list[str] = Field(default_factory=list)
    target_company_sizes: list[str] = Field(
        default_factory=list, description="e.g. '50-200', 'enterprise'."
    )
    not_interested_in: list[str] = Field(
        default_factory=list,
        description="Kinds of approach that are never worth a brief, in your words.",
    )
    research_criteria: str = Field(
        default="",
        description="What to dig into for every company: the questions you always ask.",
    )

    @property
    def configured(self) -> bool:
        return bool(self.name or self.what_we_do or self.ideal_customer)

    @property
    def has_research_criteria(self) -> bool:
        return bool(self.research_criteria.strip())


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


class FieldSource(BaseModel):
    """The one page that supports one finding.

    A list rather than a mapping because structured outputs cannot express an
    open-ended object: a schema with `additionalProperties` typed as anything
    other than false is rejected outright.
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(
        description=(
            "Which finding this supports: one_liner, description, industry, "
            "hq_location, size_estimate, founded, products, or meeting_prep."
        )
    )
    url: str = Field(description="The page it was read on.")


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
    field_sources: list[FieldSource] = Field(
        default_factory=list,
        description=(
            "Which page each finding came from, one entry per attributable field. "
            "A brief shows these next to each claim."
        ),
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="How well-evidenced this profile is."
    )
    notes: str = Field(
        default="", description="Anything ambiguous, e.g. two companies share this name."
    )

    def source_for(self, field: str) -> str | None:
        """The page backing one finding, or None if it was never attributed."""
        for entry in self.field_sources:
            if entry.field == field:
                return entry.url or None
        return None


class BriefClaim(BaseModel):
    """One assertion in a brief, and what backs it.

    A claim without a ``source_url`` is not suppressed — it is rendered as
    unverified and named in the brief's ``unknowns``. Hiding it would let the
    reader assume everything shown is evidenced.
    """

    model_config = ConfigDict(extra="forbid")

    field: str
    value: str | None = None
    source_url: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def verified(self) -> bool:
        return bool(self.value) and bool(self.source_url)


class Brief(BaseModel):
    """Everything known about one lead, assembled for a person to read.

    Assembled in code rather than written by a model. Every value here has
    already been through triage, research or the calendar; asking a model to
    restate them would be one more chance to invent something.
    """

    model_config = ConfigDict(extra="forbid")

    lead_id: str
    company: str
    domain: str
    generated_at: datetime
    claims: list[BriefClaim] = Field(default_factory=list)
    upcoming_meeting: MeetingRef | None = None
    talking_points: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    status: Literal["draft", "approved", "rejected", "delivered"] = "draft"
    approved_by: str = ""
    approved_at: datetime | None = None

    @property
    def verified_claims(self) -> list[BriefClaim]:
        return [c for c in self.claims if c.verified]

    @property
    def unverified_claims(self) -> list[BriefClaim]:
        return [c for c in self.claims if c.value and not c.source_url]
