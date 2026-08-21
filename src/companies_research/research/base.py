"""What a research provider has to do, and what it hands back.

Step 2 of the pipeline turns a company name and domain into a profile worth
putting in front of a person. How that happens — hosted search, a scraper, a
firmographics API — is an implementation detail, so it lives behind this
protocol the same way mail providers and triage backends do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..models import CompanyProfile


@dataclass
class ResearchOutcome:
    """One attempt at researching one company.

    Carries an ``error`` instead of raising for the same reason triage does: a
    company that cannot be researched should leave a note in the brief, not
    abort the run for the other companies behind it.
    """

    profile: CompanyProfile | None = None
    error: str = ""
    searches: int = 0
    fetches: int = 0

    @property
    def ok(self) -> bool:
        return self.profile is not None


class ResearchProvider(Protocol):
    name: str
    model: str

    def describe(self) -> str:
        """Human-readable identity, for logs and the UI."""

    def research(self, *, company: str, domain: str, context: str = "") -> ResearchOutcome:
        """Look up one company.

        ``context`` is what the email told us — an intent line, a product the
        sender mentioned. It is a hint for disambiguation, not a fact to repeat.
        """
        ...


class ResearchError(RuntimeError):
    """Configuration is wrong — as opposed to one lookup failing."""


def usage_counts(content: Any) -> tuple[int, int]:
    """Count server-tool calls in a response, for logging what a lookup cost."""
    searches = fetches = 0
    for block in content or []:
        if getattr(block, "type", "") != "server_tool_use":
            continue
        name = getattr(block, "name", "")
        if name == "web_search":
            searches += 1
        elif name == "web_fetch":
            fetches += 1
    return searches, fetches
