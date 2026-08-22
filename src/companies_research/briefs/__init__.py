"""Brief generation — step 4 of the pipeline."""

from __future__ import annotations

from .build import build_brief
from .render import to_html, to_markdown

__all__ = ["build_brief", "to_html", "to_markdown", "generate", "deliver"]


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


def deliver(*, brief_id: str, recipient: str, note: str = "", store=None):
    """Hand an approved brief to the configured provider, through the gate.

    Approval and delivery are separate acts. A human approving a brief is a
    record that they read it; whether it may then be sent, and to whom, is the
    gate's decision — checked here on every attempt rather than inherited from
    the click.
    """
    from .. import tools
    from ..delivery import DeliveryError, DeliveryOutcome, build_delivery
    from ..models import Brief
    from ..store import Store

    store = store or Store()
    record = store.get_brief(brief_id)
    if record is None or not record.get("brief"):
        return DeliveryOutcome(error=f"no brief {brief_id}")
    if record["status"] != "approved":
        # Delivering a draft would make the approval step decorative.
        return DeliveryOutcome(
            error=f"brief is {record['status']}, not approved — approve it first"
        )

    brief = Brief.model_validate(record["brief"])
    try:
        provider = build_delivery()
    except DeliveryError as exc:
        return DeliveryOutcome(error=str(exc))

    tools.set_caller("briefs.deliver")
    try:
        outcome = tools.deliver_brief(
            brief_id=brief_id, recipient=recipient, note=note,
            _deliver=lambda: provider.deliver(brief=brief, recipient=recipient, note=note),
        )
    except tools.ToolDenied as exc:
        return DeliveryOutcome(error=f"denied at {exc.gate}: {exc.reason}",
                               provider=provider.name)

    if outcome.ok:
        store.set_brief_status(brief_id, "delivered")
    return outcome
