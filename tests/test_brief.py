"""Brief assembly and rendering.

The rule under test throughout: **a brief never asserts more than it can show.**
An unsourced claim is marked and named, never hidden and never promoted. These
are the tests that stop a well-meaning refactor from tidying an inconvenient
"(unverified)" out of the output.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from companies_research.briefs.build import build_brief
from companies_research.briefs.render import to_html, to_markdown
from companies_research.calendars.base import CalendarOutcome
from companies_research.models import (
    CompanyProfile,
    FieldSource,
    MeetingRef,
    NewsItem,
    Relationship,
    TriageResult,
)

SOON = datetime.now(timezone.utc) + timedelta(days=2)


def _triage(**over) -> TriageResult:
    base = dict(
        message_id="m1", is_business_contact=True, relationship=Relationship.CUSTOMER,
        company_name="Acme", company_domain="acme.com", contact_name="Sarah Chen",
        contact_title="Head of Ops", intent_summary="Wants a demo",
        mentions_meeting=False, should_research=True, confidence=0.9,
    )
    base.update(over)
    return TriageResult(**base)


def _profile(**over) -> CompanyProfile:
    base = dict(
        name="Acme", domain="acme.com", one_liner="Makes widgets.",
        industry="Manufacturing", confidence=0.8,
    )
    base.update(over)
    return CompanyProfile(**base)


def _calendar(meetings=(), checked=True, reason="") -> CalendarOutcome:
    return CalendarOutcome(
        meetings=list(meetings), checked=checked, reason=reason, lookahead_days=30
    )


# --- sourcing is the whole point -------------------------------------------


def test_claim_with_a_source_is_verified():
    brief = build_brief(
        triage=_triage(),
        profile=_profile(field_sources=[FieldSource(field="one_liner", url="https://acme.com/about")]),
        calendar=_calendar(),
    )
    claim = next(c for c in brief.claims if c.field == "one_liner")
    assert claim.verified and claim.source_url == "https://acme.com/about"


def test_unsourced_claim_is_kept_but_marked_and_named():
    """It must survive into the brief, be flagged, and be counted in unknowns."""
    brief = build_brief(triage=_triage(), profile=_profile(), calendar=_calendar())
    claim = next(c for c in brief.claims if c.field == "one_liner")
    assert claim.value, "an unsourced claim must not be dropped"
    assert not claim.verified
    assert any("could not be traced" in gap for gap in brief.unknowns)
    assert "(unverified)" in to_markdown(brief)


def test_talking_points_come_only_from_sourced_claims():
    profile = _profile(
        meeting_prep=["Ask about pricing", "Ask about SLAs"],
        news=[NewsItem(title="Raised $10M", url="https://news.example/a", summary="Series A")],
    )
    brief = build_brief(triage=_triage(), profile=profile, calendar=_calendar())
    # meeting_prep has no attribution, so it must not be promoted
    assert not any("pricing" in p for p in brief.talking_points)
    # the news item carries its own url, so it may be
    assert any("Raised $10M" in p for p in brief.talking_points)


def test_attributed_prep_is_promoted_to_a_talking_point():
    profile = _profile(
        meeting_prep=["Ask about pricing"],
        field_sources=[FieldSource(field="meeting_prep", url="https://news.example/call")],
    )
    brief = build_brief(triage=_triage(), profile=profile, calendar=_calendar())
    assert brief.talking_points == ["Ask about pricing"]


def test_unsourced_prep_is_rendered_separately_and_labelled():
    profile = _profile(meeting_prep=["Ask about pricing"])
    md = to_markdown(build_brief(triage=_triage(), profile=profile, calendar=_calendar()))
    assert "Suggested prep (unverified)" in md
    assert "treat as a prompt, not a fact" in md


# --- never invent, never hide ----------------------------------------------


def test_missing_fields_are_named_not_filled():
    brief = build_brief(triage=_triage(), profile=_profile(), calendar=_calendar())
    assert any("size_estimate" in gap for gap in brief.unknowns)
    assert any("founded" in gap for gap in brief.unknowns)
    assert not any(c.field == "founded" for c in brief.claims), "must not be invented"


def test_no_research_is_stated_plainly():
    brief = build_brief(triage=_triage(), profile=None, calendar=_calendar())
    assert any("research has not run" in gap for gap in brief.unknowns)


def test_unchecked_calendar_is_not_reported_as_no_meetings():
    """The distinction a reader's diary depends on."""
    brief = build_brief(
        triage=_triage(), profile=_profile(),
        calendar=_calendar(checked=False, reason="token expired"),
    )
    assert brief.upcoming_meeting is None
    assert any("calendar not checked" in gap for gap in brief.unknowns)


def test_absent_calendar_is_also_reported():
    brief = build_brief(triage=_triage(), profile=_profile(), calendar=None)
    assert any("calendar was not checked" in gap for gap in brief.unknowns)


def test_email_mentions_a_meeting_the_diary_does_not_know_about():
    """The most useful single line a brief can produce."""
    brief = build_brief(
        triage=_triage(mentions_meeting=True), profile=_profile(), calendar=_calendar()
    )
    assert any("mentions a meeting but nothing matching" in g for g in brief.unknowns)


def test_a_real_meeting_is_surfaced():
    meeting = MeetingRef(
        event_id="e1", title="Acme intro", starts_at=SOON,
        attendees=["sarah@acme.com"], matched_on="attendee_domain", confidence=0.95,
    )
    brief = build_brief(
        triage=_triage(mentions_meeting=True), profile=_profile(),
        calendar=_calendar(meetings=[meeting]),
    )
    assert brief.upcoming_meeting is not None
    assert "Upcoming meeting" in to_markdown(brief)


# --- rendering --------------------------------------------------------------


def test_both_renderers_mark_unverified_claims():
    brief = build_brief(triage=_triage(), profile=_profile(), calendar=_calendar())
    assert "unverified" in to_markdown(brief)
    assert "unverified" in to_html(brief)


def test_sources_are_rendered_as_links():
    profile = _profile(field_sources=[FieldSource(field="one_liner", url="https://acme.com/about")])
    brief = build_brief(triage=_triage(), profile=profile, calendar=_calendar())
    assert "(https://acme.com/about)" in to_markdown(brief)
    assert 'href="https://acme.com/about"' in to_html(brief)


def test_html_escapes_attacker_controlled_content():
    """Company names and prep come from email and the web. Both are untrusted."""
    profile = _profile(
        name='Acme <script>alert(1)</script>',
        meeting_prep=['<img src=x onerror=alert(1)>'],
    )
    html = to_html(build_brief(triage=_triage(), profile=profile, calendar=_calendar()))
    # The property that matters is that no *tag* is formed. `onerror=` surviving
    # as inert text inside an escaped element is harmless; an unescaped `<img`
    # is not, so assert on the angle brackets rather than the attribute name.
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img" in html


def test_html_escapes_a_source_url_attribute():
    profile = _profile(field_sources=[FieldSource(field="one_liner", url='https://x/"><script>')])
    html = to_html(build_brief(triage=_triage(), profile=profile, calendar=_calendar()))
    assert '"><script>' not in html


def test_markdown_is_stable_and_ends_with_a_newline():
    md = to_markdown(build_brief(triage=_triage(), profile=_profile(), calendar=_calendar()))
    assert md.startswith("# Acme")
    assert md.endswith("\n")


# --- persistence ------------------------------------------------------------


def test_approved_brief_is_not_replaced_by_a_regenerated_draft(monkeypatch, tmp_path):
    """What somebody approved has to stay what they approved."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "b.db"))
    from companies_research.config import reload_settings
    from companies_research.store import Store

    reload_settings()
    store = Store()

    brief = build_brief(triage=_triage(), profile=_profile(), calendar=_calendar(),
                        lead_id="lead-1")
    brief_id = store.save_brief(brief)
    store.set_brief_status(brief_id, "approved", approved_by="chris")

    regenerated = build_brief(triage=_triage(company_name="Changed"), profile=_profile(),
                              calendar=_calendar(), lead_id="lead-1")
    again = store.save_brief(regenerated)

    assert again == brief_id
    assert store.get_brief(brief_id)["status"] == "approved"
    assert store.get_brief(brief_id)["approved_by"] == "chris"


def test_draft_is_replaced_on_regeneration(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "b2.db"))
    from companies_research.config import reload_settings
    from companies_research.store import Store

    reload_settings()
    store = Store()
    first = store.save_brief(build_brief(triage=_triage(), profile=_profile(),
                                         calendar=_calendar(), lead_id="lead-2"))
    second = store.save_brief(build_brief(triage=_triage(), profile=_profile(),
                                          calendar=_calendar(), lead_id="lead-2"))
    assert first == second
    assert len(store.list_briefs()) == 1


def test_failed_research_does_not_destroy_a_good_profile(monkeypatch, tmp_path):
    """A rate limit or a bad schema must not cost you a working brief."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "b3.db"))
    from companies_research.config import reload_settings
    from companies_research.store import Store

    reload_settings()
    store = Store()

    store.save_research("acme.com", _profile(), provider="test", model="test")
    store.save_research("acme.com", None, error="429 rate limited", provider="test", model="test")

    kept = store.get_research("acme.com")
    assert kept["profile"] is not None, "a transient failure must not lose the profile"
    assert kept["profile"]["one_liner"] == "Makes widgets."
    assert "rate limited" in kept["error"]


# --- schema guard -----------------------------------------------------------


def test_open_ended_dicts_are_rejected_before_the_api_sees_them():
    """The bug that produced a 400: dict[str, str] is not expressible."""
    from pydantic import BaseModel

    from companies_research.schema_utils import UnsupportedSchema, json_schema_for

    class Bad(BaseModel):
        mapping: dict[str, str]

    with pytest.raises(UnsupportedSchema):
        json_schema_for(Bad)


def test_company_profile_schema_is_expressible():
    from companies_research.schema_utils import json_schema_for

    schema = json_schema_for(CompanyProfile)
    assert "additionalProperties\": {" not in str(schema)
