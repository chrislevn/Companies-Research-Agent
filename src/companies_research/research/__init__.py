"""Company research — step 2 of the pipeline."""

from __future__ import annotations

from ..config import SETTINGS
from .base import ResearchError, ResearchOutcome, ResearchProvider

__all__ = [
    "ResearchError",
    "ResearchOutcome",
    "ResearchProvider",
    "PROVIDERS",
    "build_researcher",
]

# Imported lazily inside the factory so that a missing optional dependency for
# one provider never stops the others — or the rest of the app — from loading.
PROVIDERS = ("claude_web",)


def build_researcher(name: str | None = None) -> ResearchProvider:
    key = (name or SETTINGS.research_provider or "claude_web").strip().lower()
    if key == "claude_web":
        from .claude_web import ClaudeWebResearch

        return ClaudeWebResearch()
    raise ResearchError(
        f"Unknown RESEARCH_PROVIDER {key!r}. Choose one of: {', '.join(PROVIDERS)}."
    )
