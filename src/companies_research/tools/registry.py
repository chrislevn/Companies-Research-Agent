"""One chokepoint every tool call passes through.

Six gates run in a fixed order — schema, auth, scopes, rate_limit, audit,
execute — and each records a named boolean. The order is the design: cheap
structural checks refuse before expensive ones, and the audit row is written
*before* the tool runs so that a crash still leaves evidence of the attempt.

The point of putting this between the model and every capability is that a
prompt cannot argue with it. An injected instruction may well convince the
model to ask for ``deliver_brief``; it cannot give the process a scope that
``.env`` did not grant, because the scope set is never in the model's context.
That is why the boundary lives here and not in a prompt filter — a filter is a
model-level control, and model-level controls lose to model-level attacks.

Denials raise :class:`ToolDenied`, which callers turn into a structured refusal
the model can read. In keeping with the rest of this codebase, a refusal
degrades the result; it never ends a scan.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from ..config import SETTINGS

log = logging.getLogger(__name__)

GATES = ("schema", "auth", "scopes", "rate_limit", "audit", "execute")

# Who is calling. A contextvar rather than a parameter so that adding the
# harness to an existing call site does not mean threading `caller` through
# every function between the pipeline and the tool.
_caller: contextvars.ContextVar[str] = contextvars.ContextVar("tool_caller", default="unknown")
_trace: contextvars.ContextVar[str] = contextvars.ContextVar("tool_trace", default="")


def set_caller(name: str, *, trace_id: str = "") -> None:
    """Name the calling agent for the rest of this context.

    Scans run one per worker thread and the CLI is single-shot, so setting the
    variable without unsetting it is safe here. Use :class:`caller` instead when
    a block needs to restore the previous name afterwards.
    """
    _caller.set(name)
    if trace_id:
        _trace.set(trace_id)


class caller:
    """Name the calling agent for everything inside the block."""

    def __init__(self, name: str, *, trace_id: str = "") -> None:
        self.name, self.trace_id = name, trace_id
        self._tokens: list[Any] = []

    def __enter__(self) -> "caller":
        self._tokens = [_caller.set(self.name)]
        if self.trace_id:
            self._tokens.append(_trace.set(self.trace_id))
        return self

    def __exit__(self, *exc: Any) -> None:
        for token in reversed(self._tokens):
            token.var.reset(token)


class ToolDenied(Exception):
    """A gate refused the call. Carries which one, so the audit is specific."""

    def __init__(self, gate: str, reason: str) -> None:
        super().__init__(f"{gate}: {reason}")
        self.gate = gate
        self.reason = reason

    def as_refusal(self) -> dict[str, Any]:
        """A structured refusal safe to hand back to a model.

        Names the gate and the reason and nothing else — a refusal that echoed
        the arguments back would be a way to read them.
        """
        return {
            "status": "denied",
            "gate": self.gate,
            "reason": self.reason,
            "hint": "This capability is not enabled. Continue without it.",
        }


class ToolCallRecord(BaseModel):
    id: str
    ts: datetime
    tool: str
    caller: str
    args_hash: str
    gate_results: dict[str, bool]
    denied_at: str | None = None
    duration_ms: int = 0
    ok: bool = False
    error: str | None = None
    trace_id: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    args_model: type[BaseModel]
    requires_auth: bool
    scopes: frozenset[str]
    rate_limit_per_min: int
    side_effect: bool          # True = writes or sends
    description: str = ""


# --- rate limiting ---------------------------------------------------------


class _SlidingWindow:
    """Per-tool sliding window, in process.

    In process is the honest scope: this bounds one agent run, not a fleet. It
    is a runaway-loop guard, not a quota system.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, name: str, per_minute: int) -> bool:
        if per_minute <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            window = self._hits[name]
            while window and now - window[0] >= 60.0:
                window.popleft()
            if len(window) >= per_minute:
                return False
            window.append(now)
            return True

    def reset(self, name: str | None = None) -> None:
        with self._lock:
            if name is None:
                self._hits.clear()
            else:
                self._hits.pop(name, None)


RATE = _SlidingWindow()


# --- registry --------------------------------------------------------------

REGISTRY: dict[str, ToolSpec] = {}

# Extra checks a later work order can bolt onto the scopes gate without
# reopening this module — WO-03 registers the recipient allow-list here.
ScopeCheck = Callable[[ToolSpec, dict[str, Any]], "str | None"]
_scope_checks: list[ScopeCheck] = []


def add_scope_check(check: ScopeCheck) -> None:
    """Register an extra scopes-gate predicate. Return a reason to deny."""
    _scope_checks.append(check)


def canonical_args_hash(args: dict[str, Any]) -> str:
    """sha256 over canonical JSON. Never store or log the arguments themselves."""
    try:
        blob = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        blob = repr(sorted(args))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _store():
    from ..store import Store

    return Store()


def tool(spec: ToolSpec) -> Callable:
    """Route a function through the six gates.

    Keyword arguments whose name starts with ``_`` are dependencies — a live
    provider, an open client — and are neither validated nor hashed. Everything
    else must satisfy ``spec.args_model``.
    """
    REGISTRY[spec.name] = spec

    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(**kwargs: Any) -> Any:
            deps = {k: v for k, v in kwargs.items() if k.startswith("_")}
            args = {k: v for k, v in kwargs.items() if not k.startswith("_")}

            gates: dict[str, bool] = {}
            call_id = uuid.uuid4().hex[:16]
            who = _caller.get()
            trace_id = _trace.get()
            args_hash = canonical_args_hash(args)
            started = time.monotonic()
            denied_at: str | None = None
            opened = False

            def finish(ok: bool, error: str | None) -> None:
                if not opened:
                    return
                try:
                    _store().close_tool_call(
                        call_id,
                        gate_results=gates,
                        denied_at=denied_at,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        ok=ok,
                        error=error,
                    )
                except Exception:  # audit must never break the caller
                    log.exception("Could not close audit row for %s", spec.name)

            def deny(gate: str, reason: str) -> ToolDenied:
                nonlocal denied_at
                gates[gate] = False
                denied_at = gate
                log.warning("tool %s denied at %s: %s", spec.name, gate, reason)
                if opened:
                    finish(False, f"{gate}: {reason}")
                else:
                    _record_denial(call_id, spec, who, args_hash, gates, gate,
                                   int((time.monotonic() - started) * 1000), trace_id)
                return ToolDenied(gate, reason)

            # 1 ── schema
            try:
                spec.args_model(**args)
                gates["schema"] = True
            except ValidationError as exc:
                raise deny("schema", f"arguments do not match {spec.args_model.__name__} "
                                     f"({exc.error_count()} error(s))") from None

            # 2 ── auth
            if spec.requires_auth and not _has_credentials(spec):
                raise deny("auth", "no usable credentials for this tool")
            gates["auth"] = True

            # 3 ── scopes
            missing = spec.scopes - SETTINGS.tool_scopes
            if missing:
                raise deny("scopes", f"missing scope(s): {', '.join(sorted(missing))}")
            for check in _scope_checks:
                reason = check(spec, args)
                if reason:
                    raise deny("scopes", reason)
            gates["scopes"] = True

            # 4 ── rate limit
            if not RATE.allow(spec.name, spec.rate_limit_per_min):
                raise deny("rate_limit",
                           f"more than {spec.rate_limit_per_min} call(s)/min for {spec.name}")
            gates["rate_limit"] = True

            # 5 ── audit, written before execute so an attempt always leaves a trace
            if SETTINGS.tool_audit_enabled:
                try:
                    _store().open_tool_call(
                        call_id=call_id, tool=spec.name, caller=who,
                        args_hash=args_hash, gate_results=gates, trace_id=trace_id,
                    )
                    opened = True
                    gates["audit"] = True
                except Exception as exc:
                    # An unwritable audit log is a refusal, not a warning: an
                    # unaudited side effect is exactly what this exists to stop.
                    if spec.side_effect:
                        raise deny("audit", f"cannot record audit row: {exc}") from None
                    gates["audit"] = False
                    log.warning("audit row failed for read-only %s: %s", spec.name, exc)
            else:
                gates["audit"] = True

            # 6 ── execute
            try:
                result = fn(**args, **deps)
            except ToolDenied:
                raise
            except Exception as exc:
                gates["execute"] = False
                finish(False, f"{type(exc).__name__}: {exc}")
                raise
            gates["execute"] = True
            finish(True, None)
            return result

        wrapper.spec = spec  # type: ignore[attr-defined]
        return wrapper

    return decorate


def _record_denial(call_id, spec, who, args_hash, gates, gate, duration_ms, trace_id) -> None:
    """Audit a call refused before the audit gate was reached."""
    if not SETTINGS.tool_audit_enabled:
        return
    try:
        store = _store()
        store.open_tool_call(call_id=call_id, tool=spec.name, caller=who,
                             args_hash=args_hash, gate_results=gates, trace_id=trace_id)
        store.close_tool_call(call_id, gate_results=gates, denied_at=gate,
                              duration_ms=duration_ms, ok=False,
                              error=f"denied at {gate}")
    except Exception:
        log.exception("Could not audit denial of %s", spec.name)


def _has_credentials(spec: ToolSpec) -> bool:
    """Cheap pre-flight: is there any credential this tool could use?

    Deliberately shallow — proving a credential works means spending a network
    round trip, and the tool itself will fail loudly enough if it is stale.
    """
    if spec.name in ("web_search", "web_fetch"):
        import os

        return bool(
            SETTINGS.anthropic_api_key
            or os.getenv("ANTHROPIC_AUTH_TOKEN")
            or (SETTINGS.google_token_file.parent / "..").exists()
        )
    if spec.name in ("gmail_read", "calendar_read"):
        return SETTINGS.google_credentials_file.exists() or bool(
            list(SETTINGS.google_token_file.parent.glob("token*.json"))
        )
    return True


# --- recipient allow-list --------------------------------------------------

# Argument names that carry somewhere content could be sent. A tool that has
# none of these has no recipient to check, which is why a database write is
# audited as a side effect without being measured against an email address.
RECIPIENT_FIELDS = ("recipient", "recipients", "to", "email", "address", "webhook_url")


def recipient_check(spec: ToolSpec, args: dict[str, Any]) -> str | None:
    """Refuse a side-effecting call aimed anywhere but the allow-list.

    This is the control that makes "forward everything to attacker@evil.com"
    fail. It runs in the scopes gate, on arguments, after the model has already
    decided what it wants — so it does not matter how persuasive the email was.
    An empty allow-list denies everything rather than allowing everything.
    """
    if not spec.side_effect:
        return None

    wanted: list[str] = []
    for field_name in RECIPIENT_FIELDS:
        value = args.get(field_name)
        if isinstance(value, str) and value.strip():
            wanted.append(value.strip())
        elif isinstance(value, (list, tuple, set)):
            wanted.extend(str(v).strip() for v in value if str(v).strip())

    if not wanted:
        return None  # nothing addressed anywhere; not this gate's business

    allowed = SETTINGS.recipient_allowlist
    if not allowed:
        return ("no recipient is allow-listed — set ALLOWED_RECIPIENTS "
                "or USER_EMAILS before anything can be sent")

    refused = [w for w in wanted if _address_of(w).lower() not in allowed]
    if refused:
        # Name the count, not the addresses: an error string is somewhere an
        # attacker-supplied address could be read back out.
        return (f"{len(refused)} recipient(s) not in ALLOWED_RECIPIENTS "
                f"({len(allowed)} address(es) allowed)")
    return None


def _address_of(value: str) -> str:
    """Bare address out of `Name <a@b.com>`, or the value unchanged."""
    if "<" in value and ">" in value:
        return value[value.rindex("<") + 1: value.rindex(">")].strip()
    return value.strip()


add_scope_check(recipient_check)


def granted(scope: str) -> bool:
    """Is this scope enabled? For deciding what to offer, not for enforcement."""
    return scope in SETTINGS.tool_scopes


def describe_registry() -> list[dict[str, Any]]:
    return [
        {
            "name": s.name,
            "scopes": sorted(s.scopes),
            "side_effect": s.side_effect,
            "rate_limit_per_min": s.rate_limit_per_min,
            "granted": s.scopes <= SETTINGS.tool_scopes,
        }
        for s in sorted(REGISTRY.values(), key=lambda s: s.name)
    ]
