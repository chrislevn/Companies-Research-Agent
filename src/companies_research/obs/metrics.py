"""Prometheus metrics.

Almost all of this emits from the tool gate, which is already the one place
every capability passes through. That is the payoff of WO-02: instrumentation
went in at a single site rather than being sprinkled through the pipeline.

Everything here degrades to a no-op if ``prometheus_client`` is missing. An
agent that cannot start because its telemetry library is absent has its
priorities backwards — and the demo machine is not the place to discover that.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    from prometheus_client import (CollectorRegistry, Counter, Gauge, Histogram,
                                   start_http_server)

    AVAILABLE = True
except Exception:  # pragma: no cover
    AVAILABLE = False


class _Noop:
    """Stands in for a metric when prometheus_client is not installed."""

    def labels(self, *_a: Any, **_k: Any) -> "_Noop":
        return self

    def inc(self, *_a: Any, **_k: Any) -> None:
        pass

    def observe(self, *_a: Any, **_k: Any) -> None:
        pass

    def set(self, *_a: Any, **_k: Any) -> None:
        pass


REGISTRY = CollectorRegistry() if AVAILABLE else None
_server_started = False
_lock = threading.Lock()


def _counter(name: str, doc: str, labels: list[str]):
    return Counter(name, doc, labels, registry=REGISTRY) if AVAILABLE else _Noop()


def _gauge(name: str, doc: str, labels: list[str]):
    return Gauge(name, doc, labels, registry=REGISTRY) if AVAILABLE else _Noop()


def _histogram(name: str, doc: str, labels: list[str], buckets=None):
    if not AVAILABLE:
        return _Noop()
    kwargs = {"registry": REGISTRY}
    if buckets:
        kwargs["buckets"] = buckets
    return Histogram(name, doc, labels, **kwargs)


# Latency buckets span three orders of magnitude on purpose: a gate denial is
# sub-millisecond, a mail fetch is seconds, and a research lookup is minutes.
# Default buckets top out at 10s and would put most research in +Inf.
LATENCY_BUCKETS = (0.005, 0.05, 0.25, 1, 5, 15, 30, 60, 120, 300, 600)
COST_BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

TOOL_CALLS = _counter(
    "agent_tool_calls_total", "Tool calls by outcome", ["tool", "caller", "outcome"]
)
TOOL_DENIED = _counter(
    "agent_tool_denied_total", "Tool calls refused, by the gate that refused them",
    ["tool", "gate"],
)
TOOL_DURATION = _histogram(
    "agent_tool_duration_seconds", "Tool call duration", ["tool"], LATENCY_BUCKETS
)
LLM_TOKENS = _counter(
    "agent_llm_tokens_total", "Model tokens consumed", ["model", "kind"]
)
LLM_COST = _counter(
    "agent_llm_cost_usd_total", "Model spend in USD at list prices", ["model", "stage"]
)
STAGE_DURATION = _histogram(
    "agent_stage_duration_seconds", "Pipeline stage duration", ["stage"], LATENCY_BUCKETS
)
BRIEF_COST = _histogram(
    "agent_brief_cost_usd", "Total model spend per generated brief", [], COST_BUCKETS
)
SCAN_LEADS = _counter(
    "agent_scan_leads_total", "Messages by what the scan decided about them", ["outcome"]
)

# Health and uptime. Prometheus already synthesises `up` for a target it can
# reach, which answers "is it listening" — these answer the two questions that
# follow it: how long has this process been alive, and *what* is it running.
# A restart loop and a healthy process look identical on `up` alone, because
# `up` is 1 again by the time anyone looks.
START_TIME = _gauge(
    "agent_start_time_seconds", "Unix time this agent process began exporting", []
)
BUILD_INFO = _gauge(
    "agent_build_info",
    "Always 1. The labels carry which configuration is actually running.",
    ["version", "triage_backend", "triage_model", "research_provider"],
)


def mark_started() -> None:
    """Stamp process start and the running configuration.

    Called from ``serve()``. The build labels matter more than they look: the
    most expensive class of demo bug is an agent running a different backend
    than the operator believes, and this is the one place that is visible
    without reading a log.
    """
    if not AVAILABLE:
        return
    import time as _time

    from ..config import SETTINGS

    START_TIME.set(_time.time())
    BUILD_INFO.labels(
        version=_version(),
        triage_backend=SETTINGS.triage_backend or "anthropic",
        triage_model=(SETTINGS.ollama_model if SETTINGS.triage_backend == "ollama"
                      else SETTINGS.triage_model),
        research_provider=SETTINGS.research_provider or "claude_web",
    ).set(1)


def _version() -> str:
    """Short git sha when there is one, else 'dev'. Never raises."""
    import subprocess

    try:
        from ..config import ROOT

        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() or "dev"
    except Exception:
        return "dev"


# --- recording helpers -----------------------------------------------------


def record_tool_call(*, tool: str, caller: str, ok: bool, denied_at: str | None,
                     duration_ms: int) -> None:
    """One call through the gate. Called from the gate itself."""
    outcome = "denied" if denied_at else ("ok" if ok else "error")
    TOOL_CALLS.labels(tool=tool, caller=caller or "unknown", outcome=outcome).inc()
    if denied_at:
        TOOL_DENIED.labels(tool=tool, gate=denied_at).inc()
    TOOL_DURATION.labels(tool=tool).observe(duration_ms / 1000.0)


def record_stage(stage: str, seconds: float) -> None:
    STAGE_DURATION.labels(stage=stage).observe(seconds)


def record_usage(*, model: str, stage: str, input_tokens: int, output_tokens: int,
                 cost_usd: float) -> None:
    LLM_TOKENS.labels(model=model, kind="in").inc(max(input_tokens, 0))
    LLM_TOKENS.labels(model=model, kind="out").inc(max(output_tokens, 0))
    if cost_usd > 0:
        LLM_COST.labels(model=model, stage=stage).inc(cost_usd)


def record_brief_cost(cost_usd: float) -> None:
    BRIEF_COST.observe(max(cost_usd, 0.0))


def record_scan_outcome(outcome: str, count: int = 1) -> None:
    if count > 0:
        SCAN_LEADS.labels(outcome=outcome).inc(count)


# --- server ----------------------------------------------------------------


def serve(port: int | None = None, host: str | None = None) -> bool:
    """Expose /metrics on its own port.

    Deliberately separate from the web interface: the UI binds to 127.0.0.1 and
    is gated by a per-run token, and Prometheus can present neither. Putting
    metrics on the same port would mean either exposing the token or exposing
    the API.

    Binds to 127.0.0.1 by default. ``prometheus_client`` would otherwise listen
    on every interface, which quietly contradicts the rest of this app. The
    metrics carry no message content, addresses or credentials — tool names,
    counts, durations, model names and costs — but "low sensitivity" is not a
    reason to publish them to the LAN without being asked. Scraping from a
    container needs ``METRICS_HOST=0.0.0.0``; see the README.
    """
    global _server_started
    if not AVAILABLE:
        log.info("prometheus_client is not installed; metrics are not exported")
        return False
    from ..config import SETTINGS

    with _lock:
        if _server_started:
            return True
        try:
            bind_host = host or SETTINGS.metrics_host
            bind_port = port or SETTINGS.metrics_port
            start_http_server(bind_port, addr=bind_host, registry=REGISTRY)
            _server_started = True
            mark_started()
            log.info("Metrics on http://%s:%d/metrics", bind_host, bind_port)
            if bind_host not in ("127.0.0.1", "localhost", "::1"):
                log.warning(
                    "Metrics are listening on %s — reachable from the network. "
                    "That is what container scraping needs; set METRICS_HOST="
                    "127.0.0.1 if you did not intend it.", bind_host,
                )
            return True
        except OSError as exc:
            log.warning("Could not start the metrics server: %s", exc)
            return False


def snapshot() -> str:
    """Current metrics in Prometheus text format — for tests and the CLI."""
    if not AVAILABLE:
        return ""
    from prometheus_client import generate_latest

    return generate_latest(REGISTRY).decode()
