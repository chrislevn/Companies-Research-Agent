"""What a run cost, in dollars, at published list prices.

List prices, not your prices: any negotiated rate is unknowable from here, and
a figure that claims to be your invoice and is not would be worse than one that
is honestly a ceiling. Every number this produces is an upper bound.

Usage comes off the API response rather than being estimated from token counts
we computed ourselves — the server's count is the one being billed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import PRICING, SEARCH_COST_USD, SETTINGS

log = logging.getLogger(__name__)


@dataclass
class Usage:
    """Token and tool consumption for one model call."""

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    searches: int = 0

    @property
    def cost_usd(self) -> float:
        return price(self)


def price(usage: Usage) -> float:
    """Cost in USD at list rates. Unknown models price at zero, not a guess."""
    rates = _rates_for(usage.model)
    if rates is None:
        return round(usage.searches * SEARCH_COST_USD, 6)

    per_in, per_out = rates
    # Cache reads bill at ~0.1x input and writes at ~1.25x. Folding them in at
    # full input price would overstate a cached agent's cost several-fold.
    billable_in = (
        usage.input_tokens
        + usage.cache_read_tokens * 0.1
        + usage.cache_write_tokens * 1.25
    )
    total = (billable_in / 1_000_000) * per_in
    total += (usage.output_tokens / 1_000_000) * per_out
    total += usage.searches * SEARCH_COST_USD
    return round(total, 6)


def _rates_for(model: str) -> tuple[float, float] | None:
    """Longest-prefix match, so a dated or suffixed id still prices."""
    name = (model or "").strip().lower()
    if not name:
        return None
    best: tuple[int, tuple[float, float]] | None = None
    for known, rates in PRICING.items():
        if name.startswith(known) and (best is None or len(known) > best[0]):
            best = (len(known), rates)
    if best is None:
        log.debug("No list price known for %r; counting tokens but not cost", model)
        return None
    return best[1]


def usage_from_response(response: Any, *, model: str = "", searches: int = 0) -> Usage:
    """Read usage off an Anthropic response, tolerating fields that may be absent."""
    raw = getattr(response, "usage", None)

    def _get(name: str) -> int:
        value = getattr(raw, name, 0) if raw is not None else 0
        return int(value) if isinstance(value, (int, float)) else 0

    return Usage(
        model=model or getattr(response, "model", "") or "",
        input_tokens=_get("input_tokens"),
        output_tokens=_get("output_tokens"),
        cache_read_tokens=_get("cache_read_input_tokens"),
        cache_write_tokens=_get("cache_creation_input_tokens"),
        searches=searches,
    )


@dataclass
class CostLedger:
    """Accumulates spend across the stages that produce one brief."""

    by_stage: dict[str, float] = field(default_factory=dict)
    by_model: dict[str, float] = field(default_factory=dict)
    total_usd: float = 0.0

    def add(self, usage: Usage, *, stage: str) -> float:
        from . import metrics

        amount = usage.cost_usd
        self.by_stage[stage] = round(self.by_stage.get(stage, 0.0) + amount, 6)
        if usage.model:
            self.by_model[usage.model] = round(
                self.by_model.get(usage.model, 0.0) + amount, 6
            )
        self.total_usd = round(self.total_usd + amount, 6)
        metrics.record_usage(
            model=usage.model or "unknown", stage=stage,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cost_usd=amount,
        )
        return amount

    def summary(self) -> str:
        if not self.total_usd:
            return "no measurable model spend"
        parts = ", ".join(f"{k} ${v:.4f}" for k, v in sorted(self.by_stage.items()))
        return f"${self.total_usd:.4f} at list prices ({parts})"


# One ledger per process run. Briefs are assembled from cached research as often
# as not, so per-brief cost is recorded when a brief is generated rather than
# being derived by dividing a total by a count.
LEDGER = CostLedger()
