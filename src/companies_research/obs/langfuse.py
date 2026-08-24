"""Langfuse: what the model was actually asked, and what it answered.

Metrics say a triage batch took 4 seconds and cost $0.01. Traces say it
happened inside a scan. Neither tells you *why* the model called a supplier
invoice a new customer — for that you need the prompt and the completion side
by side, which is the question Langfuse exists to answer.

The reason this file is longer than a client initialisation is the tension in
that sentence. This project's rule is that raw bodies and contact details never
leave the database, and the thing Langfuse is good at is showing exactly those.
So content capture is off by default: what gets sent is the shape of the call —
model, token counts, cost, latency, batch size, which prompt version, how many
results came back and how confident they were. That answers "is triage getting
worse" and "where is the money going" without shipping anyone's mail to a
second system.

Turning ``LANGFUSE_CAPTURE_CONTENT=true`` on sends prompts and completions too,
with addresses hashed on the way out. It is genuinely more useful for debugging
a bad verdict. It is also a copy of your mailbox in a Postgres container, so it
warns when it starts and it is never the default.

Like everything in obs/, this degrades. No package, no keys, no server, bad
response — all of it becomes a debug line and a no-op. Telemetry that can stop
a scan is worse than no telemetry.
"""

from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger(__name__)

_client: Any = None
_configured = False
_warned_content = False

try:  # pragma: no cover - import guard
    from langfuse import get_client as _get_client

    AVAILABLE = True
except Exception:  # pragma: no cover
    AVAILABLE = False


# --- redaction --------------------------------------------------------------

# Deliberately greedy. A false positive costs a hashed word in a debug view; a
# false negative puts a real address in a second datastore.
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().\-]{7,}\d)(?!\d)")


def pseudonym(value: str) -> str:
    """Stable short hash. The same address is the same token every time.

    Stable on purpose: "this sender again" is a question worth being able to
    ask in a trace view, and it does not require knowing who they are.
    """
    digest = hashlib.sha256((value or "").strip().lower().encode("utf-8")).hexdigest()
    return f"<{digest[:12]}>"


def redact(text: str, *, limit: int = 2000) -> str:
    """Replace addresses and phone numbers, then truncate."""
    if not text:
        return ""
    cleaned = _EMAIL.sub(lambda m: pseudonym(m.group(0)), text)
    cleaned = _PHONE.sub("<phone>", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + f"… [+{len(cleaned) - limit} chars]"
    return cleaned


def _payload(value: Any) -> Any:
    """What is safe to send, given the current setting.

    Returns a marker rather than ``None`` when capture is off, so a trace view
    shows "withheld by policy" instead of an empty field that looks like a bug.
    """
    from ..config import SETTINGS

    if not SETTINGS.langfuse_capture_content:
        return {"withheld": "LANGFUSE_CAPTURE_CONTENT is off"}
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_payload(v) for v in value]
    return value


# --- lifecycle --------------------------------------------------------------


def setup() -> bool:
    """Build the client once. Safe to call repeatedly; never raises."""
    global _client, _configured, _warned_content
    if _configured:
        return _client is not None
    _configured = True

    from ..config import SETTINGS

    if not SETTINGS.langfuse_enabled:
        return False
    if not AVAILABLE:
        log.info("LANGFUSE_ENABLED is set but the langfuse package is not installed")
        return False
    if not (SETTINGS.langfuse_public_key and SETTINGS.langfuse_secret_key):
        log.warning("Langfuse is enabled but LANGFUSE_PUBLIC_KEY/SECRET_KEY are unset")
        return False

    try:
        import os

        # The SDK reads its own environment. Setting it here rather than asking
        # the operator to set both means one source of truth in .env.
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", SETTINGS.langfuse_public_key)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", SETTINGS.langfuse_secret_key)
        os.environ.setdefault("LANGFUSE_HOST", SETTINGS.langfuse_host)
        _client = _get_client()
        log.info("Langfuse at %s (content capture %s)", SETTINGS.langfuse_host,
                 "ON" if SETTINGS.langfuse_capture_content else "off — metadata only")
        if SETTINGS.langfuse_capture_content and not _warned_content:
            _warned_content = True
            log.warning(
                "LANGFUSE_CAPTURE_CONTENT is on: prompts and completions are being "
                "sent to %s. Addresses are hashed, but message text is not. Leave "
                "this off unless you are actively debugging a verdict.",
                SETTINGS.langfuse_host,
            )
        return True
    except Exception as exc:
        log.warning("Langfuse unavailable (%s); continuing without it", exc)
        _client = None
        return False


def flush() -> None:
    """Push anything buffered. Called before the process exits."""
    if _client is None:
        return
    try:
        _client.flush()
    except Exception:  # pragma: no cover
        log.debug("Langfuse flush failed", exc_info=True)


# --- recording --------------------------------------------------------------


@contextmanager
def generation(name: str, *, model: str, stage: str, prompt: Any = None,
               **metadata: Any) -> Iterator[Any]:
    """One model call. Yields a handle whose ``.finish()`` records the result.

    Yields a no-op handle when Langfuse is off, so call sites read the same
    either way and never need to check first.
    """
    setup()
    if _client is None:
        yield _NullGeneration()
        return

    try:
        ctx = _client.start_as_current_observation(
            as_type="generation", name=name, model=model,
            input=_payload(prompt),
            metadata={"stage": stage, **metadata},
        )
    except Exception:
        log.debug("Langfuse generation could not be opened", exc_info=True)
        yield _NullGeneration()
        return

    try:
        # __enter__ returns the observation; `ctx` is only the context manager.
        # Handing the manager to _Generation instead was a silent no-op: every
        # .update() raised AttributeError straight into the debug log, so the
        # trace showed up with its input and nothing else.
        observation = ctx.__enter__()
    except Exception:
        log.debug("Langfuse generation could not be entered", exc_info=True)
        yield _NullGeneration()
        return

    # Enter and exit are driven by hand rather than with `with`, because the
    # two failure modes must not be confused. A failure *inside* the caller's
    # block is the caller's business and has to propagate untouched — swallow
    # an Anthropic error here and a dead API key looks like an empty batch.
    # A failure in Langfuse's own bookkeeping is ours, and is only ever a log
    # line. Wrapping both in one `try` would hide the first behind the second.
    handle = _Generation(observation)
    try:
        yield handle
    except BaseException as exc:
        try:
            ctx.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            log.debug("Langfuse generation could not be closed", exc_info=True)
        raise
    try:
        ctx.__exit__(None, None, None)
    except Exception:
        log.debug("Langfuse generation could not be closed", exc_info=True)


class _NullGeneration:
    def finish(self, **_kw: Any) -> None:
        pass


class _Generation:
    def __init__(self, span: Any) -> None:
        self._span = span

    def finish(self, *, output: Any = None, usage: Any = None,
               error: str | None = None, **metadata: Any) -> None:
        """Record the outcome. Swallows its own failures by design."""
        try:
            fields: dict[str, Any] = {}
            if output is not None:
                fields["output"] = _payload(output)
            if usage is not None:
                fields["usage_details"] = {
                    "input": int(getattr(usage, "input_tokens", 0) or 0),
                    "output": int(getattr(usage, "output_tokens", 0) or 0),
                    "cache_read_input_tokens":
                        int(getattr(usage, "cache_read_tokens", 0) or 0),
                }
            if error:
                fields["level"] = "ERROR"
                fields["status_message"] = error[:500]
            if metadata:
                fields["metadata"] = metadata
            if fields:
                self._span.update(**fields)
        except Exception:
            log.debug("Langfuse update failed", exc_info=True)
