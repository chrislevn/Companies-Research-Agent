"""Observability: metrics, traces and cost accounting.

Every part of this degrades to a no-op when its library is missing or its
backend is unreachable. Telemetry that can stop the thing it measures is worse
than no telemetry.
"""

from __future__ import annotations

from . import metrics, tracing
from .cost import LEDGER, CostLedger, Usage, price, usage_from_response

__all__ = [
    "LEDGER", "CostLedger", "Usage", "metrics", "price", "tracing", "usage_from_response",
    "start",
]


def start() -> None:
    """Bring up whatever is configured. Called from the CLI and the web app."""
    from ..config import SETTINGS

    if SETTINGS.metrics_enabled:
        metrics.serve()
    if SETTINGS.tracing_enabled:
        tracing.setup()
