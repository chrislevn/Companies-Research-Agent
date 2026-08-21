"""Calendar matching, offline.

The matching rules decide whether a brief says "you are meeting them on
Thursday". A false positive there is worse than silence, so every rule is
exercised against a fake events payload rather than a live calendar.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from companies_research.calendars.base import CalendarOutcome
from companies_research.calendars.google import GoogleCalendar, _match, _mentions

SOON = datetime.now(timezone.utc) + timedelta(days=3)


def _event(**over) -> dict:
    base = {
        "id": "evt1",
        "summary": "Intro call",
        "start": {"dateTime": SOON.isoformat()},
        "attendees": [{"email": "owner@example.com"}],
        "organizer": {"email": "owner@example.com"},
        "status": "confirmed",
    }
    base.update(over)
    return base


class _FakeService:
    """Stands in for the Google client — one page, no network."""

    def __init__(self, items): self._items = items
    def events(self): return self
    def list(self, **kw): self._kw = kw; return self
    def execute(self): return {"items": self._items}


# --- matching rules --------------------------------------------------------


def test_attendee_domain_is_the_strongest_signal():
    ev = _event(attendees=[{"email": "owner@example.com"}, {"email": "sarah@acme.com"}])
    m = _match(ev, domain="acme.com", company="Acme")
    assert m is not None and m.matched_on == "attendee_domain"
    assert m.confidence >= 0.9


def test_organizer_domain_matches_when_no_attendee_does():
    ev = _event(attendees=[], organizer={"email": "sarah@acme.com"})
    m = _match(ev, domain="acme.com", company="Acme")
    assert m is not None and m.matched_on == "organizer_domain"


def test_title_mention_is_weaker_than_a_domain_match():
    ev = _event(summary="Acme quarterly sync", attendees=[{"email": "owner@example.com"}])
    m = _match(ev, domain="acme.com", company="Acme")
    assert m is not None and m.matched_on == "title_mention"
    assert m.confidence < 0.9, "a name in a title is a guess, not a fact"


def test_domain_evidence_wins_over_a_title_mention():
    """Order of evidence must not depend on the order events arrive in."""
    ev = _event(summary="Acme sync", attendees=[{"email": "sarah@acme.com"}])
    m = _match(ev, domain="acme.com", company="Acme")
    assert m.matched_on == "attendee_domain"


def test_unrelated_event_does_not_match():
    assert _match(_event(), domain="acme.com", company="Acme") is None


def test_cancelled_events_are_ignored():
    ev = _event(status="cancelled", attendees=[{"email": "sarah@acme.com"}])
    assert _match(ev, domain="acme.com", company="Acme") is None


def test_event_without_a_start_is_ignored():
    ev = _event(start={})
    ev["attendees"] = [{"email": "sarah@acme.com"}]
    assert _match(ev, domain="acme.com", company="Acme") is None


def test_all_day_event_still_matches():
    ev = _event(start={"date": "2026-09-01"}, attendees=[{"email": "s@acme.com"}])
    m = _match(ev, domain="acme.com", company="Acme")
    assert m is not None and m.starts_at.tzinfo is not None


@pytest.mark.parametrize(
    "title,company,expected",
    [
        ("Acme sync", "Acme", True),
        ("acme SYNC", "Acme", True),
        ("Call with Acme Corp", "Acme", True),
        ("Vietnam AIrlines review", "AI", False),   # substring, not a word
        ("Acmetronics demo", "Acme", False),        # prefix, not a word
        ("weekly standup", "Acme", False),
        ("planning", "AI", False),                  # too short to be meaningful
    ],
)
def test_title_mention_requires_a_whole_word(title, company, expected):
    assert _mentions(title, company) is expected


# --- the lookup as a whole -------------------------------------------------


def test_no_meetings_is_a_result_not_an_error():
    """'Nothing scheduled' must be reportable, and distinct from 'not checked'."""
    outcome = GoogleCalendar(service=_FakeService([_event()])).upcoming(
        domain="acme.com", company="Acme", lookahead_days=30
    )
    assert outcome.checked is True
    assert outcome.ok is True
    assert outcome.meetings == []
    assert outcome.reason == ""
    assert "no meetings" in outcome.summary()


def test_failure_is_distinguishable_from_no_meetings():
    class Broken(_FakeService):
        def execute(self): raise RuntimeError("quota exceeded")

    outcome = GoogleCalendar(service=Broken([])).upcoming(domain="acme.com")
    assert outcome.checked is False
    assert outcome.meetings == []
    assert "quota exceeded" in outcome.reason
    assert "not checked" in outcome.summary()


def test_lookup_never_raises_on_a_broken_service():
    class Exploding:
        def events(self): raise RuntimeError("boom")

    outcome = GoogleCalendar(service=Exploding()).upcoming(domain="acme.com")
    assert isinstance(outcome, CalendarOutcome) and not outcome.checked


def test_consumer_domains_are_refused_before_any_call():
    """Matching every gmail.com attendee would return the entire diary."""
    outcome = GoogleCalendar(service=_FakeService([])).upcoming(domain="gmail.com")
    assert outcome.checked is False
    assert "consumer mail host" in outcome.reason


def test_nothing_to_match_on_is_reported():
    outcome = GoogleCalendar(service=_FakeService([])).upcoming(domain="", company="")
    assert not outcome.checked and "no domain or company" in outcome.reason


def test_meetings_are_sorted_soonest_first():
    later = datetime.now(timezone.utc) + timedelta(days=10)
    events = [
        _event(id="b", start={"dateTime": later.isoformat()},
               attendees=[{"email": "x@acme.com"}]),
        _event(id="a", attendees=[{"email": "y@acme.com"}]),
    ]
    outcome = GoogleCalendar(service=_FakeService(events)).upcoming(domain="acme.com")
    assert [m.event_id for m in outcome.meetings] == ["a", "b"]
    assert outcome.next_meeting.event_id == "a"


def test_recurrence_is_expanded_to_instances():
    """singleEvents=True is what turns a weekly invite into dated instances."""
    service = _FakeService([])
    GoogleCalendar(service=service).upcoming(domain="acme.com")
    assert service._kw["singleEvents"] is True
    assert service._kw["orderBy"] == "startTime"


# --- the gate --------------------------------------------------------------


def test_revoked_scope_degrades_instead_of_raising(monkeypatch):
    monkeypatch.setenv("TOOL_SCOPES", "mail:read")
    from companies_research.config import reload_settings
    from companies_research.calendars import look_up

    reload_settings()
    outcome = look_up(domain="acme.com", company="Acme")
    assert isinstance(outcome, CalendarOutcome)
    assert not outcome.checked
    assert "denied at scopes" in outcome.reason


def test_disabled_calendar_is_reported_not_silent(monkeypatch):
    monkeypatch.setenv("CALENDAR_ENABLED", "false")
    from companies_research.config import reload_settings
    from companies_research.calendars import look_up

    reload_settings()
    outcome = look_up(domain="acme.com")
    assert not outcome.checked and "disabled" in outcome.reason
