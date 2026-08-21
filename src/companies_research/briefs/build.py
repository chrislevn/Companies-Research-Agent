"""Assemble a brief from what the earlier steps already established.

No model runs here. Triage, research and the calendar have each done their
work and been validated; asking a model to restate them would be one more
opportunity to invent something, for no gain.

The rule this module exists to enforce is that **a brief never asserts more
than it can show**. Every value becomes a :class:`BriefClaim` carrying the URL
that supports it, or no URL at all — in which case it is rendered as unverified
and named in ``unknowns``. Nothing is quietly dropped and nothing is quietly
upgraded.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..calendars.base import CalendarOutcome
from ..models import Brief, BriefClaim, CompanyProfile, MeetingRef, TriageResult

log = logging.getLogger(__name__)

# Profile fields worth putting in front of a person, in reading order, with the
# label a human sees. Anything not listed here is deliberately not rendered.
PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("one_liner", "What they do"),
    ("description", "Detail"),
    ("industry", "Industry"),
    ("products", "Products"),
    ("hq_location", "Headquarters"),
    ("size_estimate", "Size"),
    ("founded", "Founded"),
)

# Fields a reader will look for. If research could not establish one, the brief
# says so by name rather than leaving a silent gap the reader fills themselves.
EXPECTED_FIELDS: tuple[str, ...] = (
    "one_liner", "industry", "hq_location", "size_estimate", "founded",
)


def build_brief(
    *,
    triage: TriageResult,
    profile: CompanyProfile | None = None,
    calendar: CalendarOutcome | None = None,
    lead_id: str = "",
) -> Brief:
    """Turn the three earlier results into one reviewable document."""
    domain = (triage.company_domain or (profile.domain if profile else "")).strip().lower()
    company = (profile.name if profile and profile.name else triage.company_name) or domain

    claims: list[BriefClaim] = []
    unknowns: list[str] = []

    # -- what the email itself told us -----------------------------------
    # Sourced to the message, which is evidence we genuinely hold, as opposed
    # to a web page we are guessing at.
    for field, value in (
        ("contact_name", triage.contact_name),
        ("contact_title", triage.contact_title),
        ("intent", triage.intent_summary),
        ("relationship", triage.relationship.value),
    ):
        if value:
            claims.append(BriefClaim(
                field=field, value=value,
                source_url=f"email:{triage.message_id}" if triage.message_id else None,
                confidence=triage.confidence,
            ))

    # -- what research established ---------------------------------------
    if profile is None:
        unknowns.append("company research has not run for this lead")
    else:
        for field, _label in PROFILE_FIELDS:
            raw = getattr(profile, field, None)
            value = ", ".join(raw) if isinstance(raw, list) else (raw or "")
            if not value:
                if field in EXPECTED_FIELDS:
                    unknowns.append(f"{field} could not be established")
                continue
            claims.append(BriefClaim(
                field=field, value=value,
                source_url=profile.source_for(field),
                confidence=profile.confidence,
            ))

        # News items carry their own URL, so they are the best-evidenced
        # material in the whole brief.
        for index, item in enumerate(profile.news):
            label = item.title + (f" ({item.published})" if item.published else "")
            claims.append(BriefClaim(
                field=f"news[{index}]",
                value=f"{label} — {item.summary}" if item.summary else label,
                source_url=item.url or None,
                confidence=profile.confidence,
            ))

        # Meeting prep is a claim like any other. Where research attributed it
        # to a page it becomes a talking point; where it did not, it is shown
        # as unverified rather than presented as fact.
        prep_source = profile.source_for("meeting_prep")
        for index, point in enumerate(profile.meeting_prep):
            claims.append(BriefClaim(
                field=f"meeting_prep[{index}]", value=point,
                source_url=prep_source, confidence=profile.confidence,
            ))

        if profile.notes:
            unknowns.append(f"research noted: {profile.notes}")

    # -- what the calendar said ------------------------------------------
    meeting: MeetingRef | None = None
    if calendar is None:
        unknowns.append("calendar was not checked for this lead")
    elif not calendar.checked:
        # "Could not look" is not "nothing scheduled", and a brief that blurs
        # the two tells someone their diary is clear when nobody looked.
        unknowns.append(f"calendar not checked — {calendar.reason}")
    else:
        meeting = calendar.next_meeting
        if meeting is None and triage.mentions_meeting:
            # The email talks about meeting; the diary does not know about it.
            # That gap is the single most useful thing a brief can point out.
            unknowns.append(
                "the email mentions a meeting but nothing matching is in the calendar"
            )

    unverified = [c for c in claims if c.value and not c.source_url]
    if unverified:
        unknowns.append(
            f"{len(unverified)} claim(s) could not be traced to a source page"
        )

    return Brief(
        lead_id=lead_id or f"{domain}:{triage.message_id}",
        company=company,
        domain=domain,
        generated_at=datetime.now(timezone.utc),
        claims=claims,
        upcoming_meeting=meeting,
        talking_points=_talking_points(claims),
        unknowns=unknowns,
        sources=_sources(claims, profile),
        status="draft",
    )


def _talking_points(claims: list[BriefClaim]) -> list[str]:
    """Only from claims that carry a source.

    A talking point is what someone repeats out loud in a meeting, which makes
    it the worst possible place for an unsourced assertion. Prep and news that
    could not be traced to a page stay in the brief as unverified claims; they
    do not get promoted to something worth saying.
    """
    points: list[str] = []
    for claim in claims:
        if not claim.verified:
            continue
        if claim.field.startswith("meeting_prep["):
            points.append(claim.value or "")
        elif claim.field.startswith("news["):
            points.append(f"Recent: {claim.value}")
    return [p for p in points if p]


def _sources(claims: list[BriefClaim], profile: CompanyProfile | None) -> list[str]:
    """Every distinct web source behind the brief, in first-seen order.

    ``email:`` references are excluded — they are provenance for the reader,
    not links they can follow.
    """
    seen: list[str] = []
    for claim in claims:
        url = claim.source_url or ""
        if url and not url.startswith("email:") and url not in seen:
            seen.append(url)
    for url in (profile.sources if profile else []):
        if url not in seen:
            seen.append(url)
    return seen
