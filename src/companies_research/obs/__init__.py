"""Observability: metrics, traces and cost accounting.

Every part of this degrades to a no-op when its library is missing or its
backend is unreachable. Telemetry that can stop the thing it measures is worse
than no telemetry.
"""

from __future__ import annotations

from . import langfuse, metrics, tracing
from .cost import LEDGER, CostLedger, Usage, price, usage_from_response

__all__ = [
    "LEDGER", "CostLedger", "Usage", "langfuse", "metrics", "price", "tracing",
    "usage_from_response", "start", "shutdown",
]


def start() -> None:
    """Bring up whatever is configured. Called from the CLI and the web app."""
    from ..config import SETTINGS

    if SETTINGS.metrics_enabled:
        metrics.serve()
    if SETTINGS.tracing_enabled:
        tracing.setup()
    if SETTINGS.langfuse_enabled:
        langfuse.setup()


def shutdown() -> None:
    """Flush buffered telemetry. Losing the last batch is a poor way to end."""
    langfuse.flush()
