"""Two renderings of one brief: Markdown is canonical, HTML is for the webapp.

Both obey the same rule — an unverified claim is *marked*, never hidden, and
never silently dropped. A reader who cannot tell which lines are evidenced will
assume all of them are, which is exactly the failure a sourced brief exists to
prevent.
"""

from __future__ import annotations

from html import escape

from ..models import Brief, BriefClaim
from .build import PROFILE_FIELDS

LABELS = dict(PROFILE_FIELDS) | {
    "contact_name": "Contact",
    "contact_title": "Title",
    "intent": "What they want",
    "relationship": "Relationship",
}


def _label(field: str) -> str:
    if field.startswith("news["):
        return "News"
    if field.startswith("meeting_prep["):
        return "Prep"
    return LABELS.get(field, field.replace("_", " ").capitalize())


def _grouped(brief: Brief) -> tuple[list[BriefClaim], list[BriefClaim], list[BriefClaim]]:
    """Facts, news and prep — the three things a reader wants separately."""
    facts, news, prep = [], [], []
    for claim in brief.claims:
        if claim.field.startswith("news["):
            news.append(claim)
        elif claim.field.startswith("meeting_prep["):
            prep.append(claim)
        else:
            facts.append(claim)
    return facts, news, prep


# --- markdown (canonical) --------------------------------------------------


def to_markdown(brief: Brief) -> str:
    facts, news, prep = _grouped(brief)
    out: list[str] = [f"# {brief.company}", ""]
    if brief.domain:
        out.append(f"**{brief.domain}** · generated {brief.generated_at:%Y-%m-%d %H:%M} UTC")
        out.append("")

    if brief.upcoming_meeting:
        meeting = brief.upcoming_meeting
        out += [
            "## Upcoming meeting",
            "",
            f"**{meeting.starts_at:%A %d %B %Y, %H:%M}** — {meeting.title or '(untitled)'}",
            "",
            f"Matched on {meeting.matched_on.replace('_', ' ')} "
            f"(confidence {meeting.confidence:.2f}).",
            "",
        ]
        if meeting.attendees:
            out += ["Attendees: " + ", ".join(meeting.attendees), ""]

    if facts:
        out += ["## The company", ""]
        for claim in facts:
            out.append(f"- **{_label(claim.field)}:** {claim.value}{_md_source(claim)}")
        out.append("")

    if brief.talking_points:
        out += ["## Talking points", ""]
        out += [f"- {point}" for point in brief.talking_points]
        out += ["", "*Only points traceable to a source appear here.*", ""]

    unsourced_prep = [c for c in prep if not c.verified]
    if unsourced_prep:
        out += ["## Suggested prep (unverified)", ""]
        for claim in unsourced_prep:
            out.append(f"- {claim.value} *(no source — treat as a prompt, not a fact)*")
        out.append("")

    if news:
        out += ["## Recent news", ""]
        for claim in news:
            out.append(f"- {claim.value}{_md_source(claim)}")
        out.append("")

    if brief.unknowns:
        out += ["## What we could not establish", ""]
        out += [f"- {gap}" for gap in brief.unknowns]
        out += ["", "*Listed rather than left blank: a gap you cannot see is a gap "
                "you will fill in yourself.*", ""]

    if brief.sources:
        out += ["## Sources", ""]
        out += [f"{i}. <{url}>" for i, url in enumerate(brief.sources, start=1)]
        out.append("")

    out += ["---", "", f"Status: **{brief.status}** · lead `{brief.lead_id}`"]
    if brief.approved_by:
        out.append(f" · approved by {brief.approved_by}")
    return "\n".join(out).rstrip() + "\n"


def _md_source(claim: BriefClaim) -> str:
    if not claim.source_url:
        return "  *(unverified)*"
    if claim.source_url.startswith("email:"):
        return "  *(from the email)*"
    return f"  ([source]({claim.source_url}))"


# --- html (webapp) ---------------------------------------------------------


def to_html(brief: Brief) -> str:
    facts, news, prep = _grouped(brief)
    parts: list[str] = [
        f'<article class="brief" data-status="{escape(brief.status)}">',
        f"<header><h1>{escape(brief.company)}</h1>",
    ]
    if brief.domain:
        parts.append(
            f'<p class="muted small">{escape(brief.domain)} · generated '
            f"{brief.generated_at:%Y-%m-%d %H:%M} UTC</p>"
        )
    parts.append("</header>")

    if brief.upcoming_meeting:
        meeting = brief.upcoming_meeting
        parts += [
            '<section class="meeting"><h2>Upcoming meeting</h2>',
            f"<p><strong>{meeting.starts_at:%A %d %B %Y, %H:%M}</strong> — "
            f"{escape(meeting.title or '(untitled)')}</p>",
            f'<p class="muted small">Matched on '
            f"{escape(meeting.matched_on.replace('_', ' '))} "
            f"(confidence {meeting.confidence:.2f})</p>",
        ]
        if meeting.attendees:
            parts.append(
                '<p class="muted small">Attendees: '
                + escape(", ".join(meeting.attendees)) + "</p>"
            )
        parts.append("</section>")

    if facts:
        parts.append("<section><h2>The company</h2><dl>")
        for claim in facts:
            parts.append(
                f"<dt>{escape(_label(claim.field))}</dt>"
                f"<dd>{escape(claim.value or '')}{_html_source(claim)}</dd>"
            )
        parts.append("</dl></section>")

    if brief.talking_points:
        parts.append("<section><h2>Talking points</h2><ul>")
        parts += [f"<li>{escape(point)}</li>" for point in brief.talking_points]
        parts.append('</ul><p class="muted small">Only points traceable to a source '
                     "appear here.</p></section>")

    unsourced_prep = [c for c in prep if not c.verified]
    if unsourced_prep:
        parts.append('<section class="unverified"><h2>Suggested prep (unverified)</h2><ul>')
        for claim in unsourced_prep:
            parts.append(
                f"<li>{escape(claim.value or '')} "
                '<span class="flag">no source — treat as a prompt, not a fact</span></li>'
            )
        parts.append("</ul></section>")

    if news:
        parts.append("<section><h2>Recent news</h2><ul>")
        for claim in news:
            parts.append(f"<li>{escape(claim.value or '')}{_html_source(claim)}</li>")
        parts.append("</ul></section>")

    if brief.unknowns:
        parts.append('<section class="unknowns"><h2>What we could not establish</h2><ul>')
        parts += [f"<li>{escape(gap)}</li>" for gap in brief.unknowns]
        parts.append('</ul><p class="muted small">Listed rather than left blank: a gap '
                     "you cannot see is a gap you will fill in yourself.</p></section>")

    if brief.sources:
        parts.append("<section><h2>Sources</h2><ol>")
        for url in brief.sources:
            safe = escape(url, quote=True)
            parts.append(
                f'<li><a href="{safe}" target="_blank" rel="noopener">{escape(url)}</a></li>'
            )
        parts.append("</ol></section>")

    parts.append(
        f'<footer class="muted small">Status: <strong>{escape(brief.status)}</strong> · '
        f"lead <code>{escape(brief.lead_id)}</code></footer></article>"
    )
    return "\n".join(parts)


def _html_source(claim: BriefClaim) -> str:
    if not claim.source_url:
        return ' <span class="flag">unverified</span>'
    if claim.source_url.startswith("email:"):
        return ' <span class="muted small">from the email</span>'
    safe = escape(claim.source_url, quote=True)
    return f' <a class="src" href="{safe}" target="_blank" rel="noopener">source</a>'
