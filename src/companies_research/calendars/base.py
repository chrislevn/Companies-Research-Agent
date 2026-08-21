"""What a calendar provider has to do, and what it hands back.

Step 3 answers one question: is a meeting with this company already in the
diary? That changes a brief from background reading into preparation, so the
answer has to be trustworthy — which mostly means being honest about how it was
reached, and saying "none" when there are none.

Same protocol-and-factory shape as mail providers and research providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import MeetingRef

# Sharing a mail domain with an attendee is all but conclusive; the organiser is
# equally strong. A company name appearing in a title is a guess — "Northwind
# sync" might be about them, or about a project named after them.
CONFIDENCE = {
    "attendee_domain": 0.95,
    "organizer_domain": 0.95,
    "title_mention": 0.55,
}


@dataclass
class CalendarOutcome:
    """One lookup for one company.

    ``checked`` is what separates "no meetings" from "could not look". Both come
    back with an empty ``meetings`` list, and a brief that confuses them would
    tell someone there is nothing scheduled when in truth nobody looked.
    """

    meetings: list[MeetingRef] = field(default_factory=list)
    checked: bool = False
    reason: str = ""
    events_scanned: int = 0
    lookahead_days: int = 0

    @property
    def ok(self) -> bool:
        return self.checked and not self.reason

    @property
    def next_meeting(self) -> MeetingRef | None:
        """Soonest match, most confident first among simultaneous ones."""
        if not self.meetings:
            return None
        return sorted(self.meetings, key=lambda m: (m.starts_at, -m.confidence))[0]

    def summary(self) -> str:
        if self.reason:
            return f"calendar not checked — {self.reason}"
        if not self.meetings:
            return f"no meetings in the next {self.lookahead_days} days"
        soonest = self.next_meeting
        assert soonest is not None
        return (
            f"{len(self.meetings)} upcoming meeting(s); next {soonest.starts_at:%Y-%m-%d %H:%M} "
            f"({soonest.matched_on.replace('_', ' ')})"
        )


class CalendarProvider(Protocol):
    name: str

    def describe(self) -> str:
        """Human-readable identity, for logs and the UI."""

    def upcoming(
        self, *, domain: str, company: str = "", lookahead_days: int = 30
    ) -> CalendarOutcome:
        """Events in the window that look like they involve this company."""
        ...


class CalendarError(RuntimeError):
    """Configuration is wrong — as opposed to one lookup failing."""
