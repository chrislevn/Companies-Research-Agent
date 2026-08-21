"""Brief generation — step 4 of the pipeline."""

from __future__ import annotations

from .build import build_brief
from .render import to_html, to_markdown

__all__ = ["build_brief", "to_html", "to_markdown", "generate"]


def generate(*, domain: str, store=None, lead_id: str = "", refresh_calendar: bool = True):
    """Build a brief for one company from whatever is already known.

    Reads triage and research out of the store rather than re-running them: a
    brief is an assembly job, and re-researching on every render would make an
    expensive step happen every time somebody refreshed a page.
    """
    from ..calendars import look_up
    from ..models import CompanyProfile, TriageResult
    from ..store import Store

    store = store or Store()
    key = (domain or "").strip().lower()

    triage = None
    for lead in store.recent_leads(limit=500, only_research=False):
        if (lead["triage"].get("company_domain") or "").strip().lower() == key:
            triage = TriageResult.model_validate(lead["triage"])
            lead_id = lead_id or lead["uid"]
            break
    if triage is None:
        return None

    cached = store.get_research(key)
    profile = (
        CompanyProfile.model_validate(cached["profile"])
        if cached and cached.get("profile") else None
    )
    calendar = look_up(domain=key, company=triage.company_name) if refresh_calendar else None

    return build_brief(triage=triage, profile=profile, calendar=calendar, lead_id=lead_id)
