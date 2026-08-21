"""Google Calendar lookup over the already-consented ``calendar.readonly`` scope.

The scope has been in :data:`GOOGLE_SCOPES` since the first sign-in, so this
needs no new consent and no token to re-issue — it reads a permission that was
granted and then never used.

Matching is deliberately boring. Domain equality is checked in Python rather
than handed to Calendar's free-text search, because ``q=acme.com`` also matches
an event whose description happens to mention the domain, and a brief that
claims a meeting exists when it does not is worse than one that says nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import SETTINGS
from ..models import MeetingRef
from .base import CONFIDENCE, CalendarOutcome

log = logging.getLogger(__name__)

# Consumer mail hosts. A meeting is not "with Acme" because someone in it has a
# gmail.com address, and on a personal calendar most attendees do.
GENERIC_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
        "yahoo.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
        "aol.com", "qq.com", "163.com", "resource.calendar.google.com",
        "group.calendar.google.com", "group.v.calendar.google.com",
    }
)


class GoogleCalendar:
    name = "google"

    def __init__(self, service: Any | None = None) -> None:
        self._service = service

    def describe(self) -> str:
        return "Google Calendar (calendar.readonly)"

    # ------------------------------------------------------------------

    def upcoming(
        self, *, domain: str, company: str = "", lookahead_days: int = 30
    ) -> CalendarOutcome:
        domain = (domain or "").strip().lower().lstrip("@")
        company = (company or "").strip()
        outcome = CalendarOutcome(lookahead_days=lookahead_days)

        if not domain and not company:
            outcome.reason = "no domain or company name to match on"
            return outcome
        if domain and domain in GENERIC_DOMAINS:
            # Matching every gmail.com attendee would return the whole diary.
            outcome.reason = f"{domain} is a consumer mail host, not a company domain"
            return outcome

        try:
            service = self._service or self._build_service()
        except Exception as exc:
            log.warning("Calendar unavailable: %s", exc)
            outcome.reason = f"calendar unavailable ({type(exc).__name__})"
            return outcome

        now = datetime.now(timezone.utc)
        try:
            events = self._list_events(service, now, now + timedelta(days=lookahead_days))
        except Exception as exc:
            log.warning("Calendar lookup failed for %s: %s", domain or company, exc)
            outcome.reason = f"lookup failed ({type(exc).__name__}: {exc})"
            return outcome

        # Reached the API and got an answer. Whatever the answer is, it counts
        # as having looked — including an answer of "nothing".
        outcome.checked = True
        outcome.events_scanned = len(events)

        for event in events:
            match = _match(event, domain=domain, company=company)
            if match is not None:
                outcome.meetings.append(match)

        outcome.meetings.sort(key=lambda m: (m.starts_at, -m.confidence))
        return outcome

    # ------------------------------------------------------------------

    def _build_service(self) -> Any:
        """Build the Calendar client from the *mailbox's* token.

        The token the user actually consented with is written per account
        (``credentials/token-<account_id>.json``), not to the single default
        path. Reading the default path finds nothing, and ``get_credentials``
        responds to nothing by opening a browser and waiting — which in a cron
        job or a CLI run means hanging forever. So: locate the real token, and
        never sit in a consent flow that no one is watching.
        """
        from googleapiclient.discovery import build

        from ..google_auth import get_credentials

        token_file = _google_token_file()
        if token_file is None or not token_file.exists():
            raise FileNotFoundError(
                "no Google token found — run `auth`, or connect the mailbox in the "
                "web interface, before reading the calendar"
            )

        creds = get_credentials(
            token_file=token_file,
            # A cached token needs no consent. If one is somehow required, fail
            # in a minute with a message rather than blocking indefinitely.
            consent_timeout=60,
        )
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def _list_events(self, service: Any, start: datetime, end: datetime) -> list[dict]:
        """Every event in the window, expanding recurrences to real instances."""
        events: list[dict] = []
        page: str | None = None
        while True:
            response = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,        # expand recurrence into instances
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=page,
                )
                .execute()
            )
            events.extend(response.get("items", []))
            page = response.get("nextPageToken")
            if not page:
                break
        return events


def _match(event: dict, *, domain: str, company: str) -> MeetingRef | None:
    """Decide whether one event involves this company, and how sure we are.

    Domain evidence is checked first and wins: a title mention is only consulted
    when nothing stronger is available, so a confident match is never downgraded
    by the order events happen to arrive in.
    """
    if event.get("status") == "cancelled":
        return None
    starts_at = _start_of(event)
    if starts_at is None:
        return None

    attendees = [
        a.get("email", "") for a in event.get("attendees", []) or [] if a.get("email")
    ]
    organizer = (event.get("organizer") or {}).get("email", "")
    title = event.get("summary", "") or ""

    matched_on: str | None = None
    if domain and any(_domain_of(a) == domain for a in attendees):
        matched_on = "attendee_domain"
    elif domain and _domain_of(organizer) == domain:
        matched_on = "organizer_domain"
    elif company and _mentions(title, company):
        matched_on = "title_mention"

    if matched_on is None:
        return None

    return MeetingRef(
        event_id=event.get("id", ""),
        title=title,
        starts_at=starts_at,
        attendees=sorted(set(attendees)),
        matched_on=matched_on,  # type: ignore[arg-type]
        confidence=CONFIDENCE[matched_on],
    )


def _google_token_file():
    """The token a configured Google mailbox signed in with.

    Calendar and mail share one consent, so this reuses whatever the mailbox
    already holds rather than starting a second sign-in for the same account.
    """
    from pathlib import Path

    try:
        from ..accounts import load_accounts

        for account in load_accounts():
            if account.provider != "gmail":
                continue
            configured = account.auth.get("token_file")
            if configured:
                return Path(configured)
    except Exception:  # a broken accounts.json must not mask the real error
        log.debug("Could not read accounts while locating a Google token", exc_info=True)

    default = SETTINGS.google_token_file
    if default.exists():
        return default
    # Last resort: the per-account naming the web UI writes.
    candidates = sorted(default.parent.glob("token*.json"))
    return candidates[0] if candidates else None


def _domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower() if "@" in (address or "") else ""


def _mentions(title: str, company: str) -> bool:
    """Whole-word, case-insensitive company mention in a title.

    Substring matching would make "AI" match "Vietnam AIrlines"; requiring word
    boundaries keeps the weakest signal from also being the noisiest.
    """
    import re

    needle = company.strip()
    if len(needle) < 3:      # two-letter names match far too much
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", title or "", re.IGNORECASE) is not None


def _start_of(event: dict) -> datetime | None:
    """Event start as an aware datetime; all-day events start at local midnight."""
    start = event.get("start") or {}
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
