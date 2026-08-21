"""Company research via Claude's server-side web search and fetch.

Anthropic runs the searching and the page fetching, so there is no scraper to
maintain here and nothing to keep working when a site changes its markup. What
this module owns is the part that is actually ours: the brief we ask for, the
schema it has to come back in, and refusing to let one bad lookup stop a run.

Unlike triage, the input is a company name and a public domain rather than
anybody's mail, so sending it to a hosted service gives away nothing the company
has not already published.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import prompts
from .. import tools as harness
from ..config import SETTINGS
from ..models import CompanyProfile
from ..schema_utils import json_schema_for
from .base import ResearchOutcome, usage_counts

log = logging.getLogger(__name__)

# The dynamic-filtering tool versions. Claude runs code behind these to filter
# results before they reach the context window, so `code_execution` must NOT be
# declared alongside them — a second execution environment confuses the model.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch"}

# Server tools loop server-side and can stop with `pause_turn` before finishing.
# Each resume is another billable request, so the cap is low and deliberate.
MAX_RESUMES = 3

DEFAULT_SYSTEM_PROMPT = """\
You research companies for an executive who is about to meet or reply to them.

Work from the company's own website first, then recent news. Prefer primary
sources — the company's about/product pages, its own announcements — over
directories and aggregators, which are frequently stale or wrong.

Rules:
- Every factual claim must come from a page you actually retrieved. If you could
  not find something, leave the field empty. An empty field is useful; an
  invented one destroys trust in the whole brief.
- Put every URL you relied on in `sources`.
- `news` is for genuinely recent and relevant items — funding, launches,
  leadership changes, expansion, notable customers. Skip press-release filler
  and anything older than about a year unless it is still what defines them.
- `meeting_prep` is the point of this brief. Write things worth saying out loud:
  what they are likely to want, what to ask them, what has changed recently.
  Not generic advice — specifics grounded in what you found.
- Several companies may share a name. Trust the domain over the name, and say so
  in `notes` if you are not certain you found the right one.
- Set `confidence` low when the evidence is thin. Say what is missing in `notes`.
- Write in English unless the company operates primarily in Vietnamese, in which
  case write `description`, `one_liner` and `meeting_prep` in Vietnamese.
"""


class ClaudeWebResearch:
    """Researches a company with Claude plus the hosted web tools."""

    name = "claude_web"

    def __init__(self, client: Any | None = None) -> None:
        import anthropic

        if client is None:
            client = (
                anthropic.Anthropic(api_key=SETTINGS.anthropic_api_key)
                if SETTINGS.anthropic_api_key
                else anthropic.Anthropic()
            )
        self.client = client
        self.model = SETTINGS.research_model
        self.effort = SETTINGS.research_effort
        self.max_searches = SETTINGS.research_max_searches

    def describe(self) -> str:
        return f"Claude web search ({self.model})"

    # ------------------------------------------------------------------

    def research(self, *, company: str, domain: str, context: str = "") -> ResearchOutcome:
        if not (company or domain):
            return ResearchOutcome(error="no company name or domain to research")

        # Loaded per lookup, not at import, so editing prompts/research.md takes
        # effect on the next company without restarting a running watcher.
        prompt = prompts.load("research", DEFAULT_SYSTEM_PROMPT)
        if prompt.customised:
            log.info("  using custom research prompt (%s)", prompt.source)

        # Gate before declaring. A revoked research:read scope means the tool is
        # never offered to the model, so no prompt can talk it into searching —
        # the capability is absent rather than merely discouraged.
        api_tools: list[dict[str, Any]] = []
        try:
            harness.web_search(company=company, domain=domain, max_uses=self.max_searches)
            api_tools.append({**WEB_SEARCH_TOOL, "max_uses": self.max_searches})
        except harness.ToolDenied as exc:
            log.warning("web_search denied at %s: %s", exc.gate, exc.reason)
        try:
            harness.web_fetch(domain=domain, max_uses=self.max_searches)
            api_tools.append({**WEB_FETCH_TOOL, "max_uses": self.max_searches})
        except harness.ToolDenied as exc:
            log.warning("web_fetch denied at %s: %s", exc.gate, exc.reason)

        if not api_tools:
            return ResearchOutcome(
                error="research tools are not enabled (missing scope research:read)"
            )
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": _render_request(company, domain, context)}
        ]

        searches = fetches = 0
        try:
            for attempt in range(MAX_RESUMES + 1):
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    system=prompt.text,
                    tools=api_tools,
                    output_config={
                        "format": {"type": "json_schema", "schema": json_schema_for(CompanyProfile)},
                        "effort": self.effort,
                    },
                    messages=messages,
                )
                used_searches, used_fetches = usage_counts(response.content)
                searches += used_searches
                fetches += used_fetches

                if response.stop_reason == "refusal":
                    category = getattr(response.stop_details, "category", None)
                    return ResearchOutcome(
                        error=f"model declined to research this company ({category})",
                        searches=searches,
                        fetches=fetches,
                    )

                # The server-side tool loop hit its iteration cap mid-task. Send
                # the turn back unchanged and it picks up where it stopped; do
                # not add a "continue" message, which only confuses it.
                if response.stop_reason == "pause_turn":
                    if attempt == MAX_RESUMES:
                        return ResearchOutcome(
                            error="research did not finish within the allowed rounds",
                            searches=searches,
                            fetches=fetches,
                        )
                    log.info(
                        "  research paused after %d search(es); resuming (%d/%d)",
                        searches,
                        attempt + 1,
                        MAX_RESUMES,
                    )
                    messages = messages[:1] + [
                        {"role": "assistant", "content": response.content}
                    ]
                    continue

                if response.stop_reason == "max_tokens":
                    log.warning("Research hit max_tokens for %s; profile may be short", domain)

                text = next(
                    (b.text for b in response.content if getattr(b, "type", "") == "text"), ""
                )
                if not text:
                    return ResearchOutcome(
                        error="empty model response", searches=searches, fetches=fetches
                    )
                try:
                    profile = CompanyProfile.model_validate_json(text)
                except Exception:
                    log.exception("Could not parse research output: %s", text[:500])
                    return ResearchOutcome(
                        error="unparseable model response",
                        searches=searches,
                        fetches=fetches,
                    )
                # The model is told to trust the domain; make sure the record
                # agrees with the key it will be cached under.
                if domain and not profile.domain:
                    profile.domain = domain
                return ResearchOutcome(
                    profile=profile, searches=searches, fetches=fetches
                )
        except Exception as exc:  # network, auth, rate limit
            log.exception("Research call failed for %s", domain or company)
            return ResearchOutcome(
                error=f"{type(exc).__name__}: {exc}", searches=searches, fetches=fetches
            )

        return ResearchOutcome(error="research did not produce a result")


def _render_request(company: str, domain: str, context: str) -> str:
    lines = ["Research this company and return the brief."]
    if company:
        lines.append(f"Company name: {company}")
    if domain:
        lines.append(f"Website domain: {domain}")
    if context:
        lines.append(
            "\nWhat they contacted us about (a hint for finding the right company, "
            f"not a fact to repeat back):\n{context}"
        )
    return "\n".join(lines)
