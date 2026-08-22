"""OpenTelemetry spans: one per scan, children per stage and per tool call.

The trace id is written into every ``tool_calls`` row, so a span in a trace
viewer and a row in the audit table are two views of the same event. That link
is the point: metrics tell you denials rose, the audit table tells you which
tool and gate, and the trace tells you what the agent was doing at the time.

Off by default. Tracing needs a collector, and an agent that fails to start
because nothing is listening on 4318 would be a poor trade for a demo.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger(__name__)

_tracer: Any = None
_configured = False

try:  # pragma: no cover - import guard
    from opentelemetry import trace as _otel_trace

    AVAILABLE = True
except Exception:  # pragma: no cover
    AVAILABLE = False


def setup() -> bool:
    """Wire up the exporter once. Safe to call repeatedly."""
    global _tracer, _configured
    if _configured:
        return _tracer is not None
    _configured = True

    from ..config import SETTINGS

    if not (AVAILABLE and SETTINGS.tracing_enabled):
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": "companies-research-agent"})
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=SETTINGS.otlp_endpoint))
        )
        _otel_trace.set_tracer_provider(provider)
        _tracer = _otel_trace.get_tracer("companies_research")
        log.info("Tracing to %s", SETTINGS.otlp_endpoint)
        return True
    except Exception as exc:
        log.warning("Tracing unavailable (%s); continuing without it", exc)
        _tracer = None
        return False


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """A span, or nothing at all if tracing is off."""
    setup()
    if _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(name) as active:
        for key, value in attributes.items():
            if value is not None:
                active.set_attribute(key, value)
        yield


def current_trace_id() -> str:
    """Hex trace id for the audit row, or '' when nothing is being traced."""
    if not AVAILABLE or _tracer is None:
        return ""
    try:
        context = _otel_trace.get_current_span().get_span_context()
        return f"{context.trace_id:032x}" if context and context.trace_id else ""
    except Exception:
        return ""
