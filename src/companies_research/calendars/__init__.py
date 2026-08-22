"""Calendar lookup — step 3 of the pipeline.

Named ``calendars`` rather than ``calendar`` so that nothing in this package, or
any library it imports, can accidentally shadow the standard library module of
that name.
"""

from __future__ import annotations

from ..config import SETTINGS
from .base import CONFIDENCE, CalendarError, CalendarOutcome, CalendarProvider

__all__ = [
    "CONFIDENCE",
    "CalendarError",
    "CalendarOutcome",
    "CalendarProvider",
    "PROVIDERS",
    "build_calendar",
    "look_up",
]

PROVIDERS = ("google",)


def build_calendar(name: str | None = None) -> CalendarProvider:
    key = (name or SETTINGS.calendar_provider or "google").strip().lower()
    if key == "google":
        from .google import GoogleCalendar

        return GoogleCalendar()
    raise CalendarError(
        f"Unknown CALENDAR_PROVIDER {key!r}. Choose one of: {', '.join(PROVIDERS)}."
    )


def look_up(
    *,
    domain: str,
    company: str = "",
    lookahead_days: int | None = None,
    provider: CalendarProvider | None = None,
) -> CalendarOutcome:
    """Look for meetings with one company, through the tool gate.

    Never raises. A revoked ``calendar:read`` scope, a missing token or a failing
    API all come back as an unchecked outcome carrying the reason, because step 4
    has to render *something* for every lead and "we could not look" is a
    perfectly good thing to render.
    """
    from .. import tools

    days = lookahead_days if lookahead_days is not None else SETTINGS.calendar_lookahead_days
    if not SETTINGS.calendar_enabled:
        return CalendarOutcome(reason="calendar lookup is disabled", lookahead_days=days)

    import time as _time

    from ..obs import metrics as _metrics
    from ..obs import tracing as _tracing

    tools.set_caller("calendar.lookup")
    _started = _time.monotonic()
    try:
        agent = provider or build_calendar()
    except CalendarError as exc:
        return CalendarOutcome(reason=str(exc), lookahead_days=days)

    try:
        with _tracing.span("stage.calendar", **{"calendar.domain": domain}):
            return tools.calendar_read(
                domain=domain,
                company=company,
                lookahead_days=days,
                _look=lambda: agent.upcoming(
                    domain=domain, company=company, lookahead_days=days
                ),
            )
    except tools.ToolDenied as exc:
        return CalendarOutcome(
            reason=f"denied at {exc.gate}: {exc.reason}", lookahead_days=days
        )
    finally:
        _metrics.record_stage("calendar", _time.monotonic() - _started)
