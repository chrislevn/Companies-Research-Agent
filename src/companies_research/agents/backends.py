"""Where triage actually runs — a hosted API, or a model on this machine.

Triage is a classification task with a fixed JSON shape, so a backend only has
to do one thing: take a system prompt, a user prompt and a JSON Schema, and come
back with text that validates against that schema. Both backends below constrain
decoding to the schema rather than asking for JSON politely, so the caller can
keep parsing with the same Pydantic model either way.

The split exists because email is sensitive. The Anthropic backend is stronger,
especially on Vietnamese and on thin evidence; the Ollama backend never sends a
message body off this machine. Which of those matters more is a deployment
decision, not a code one — hence the switch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ..config import SETTINGS

log = logging.getLogger(__name__)


@dataclass
class Completion:
    """One backend response.

    ``error`` set means the whole batch could not be classified and the caller
    should fall back rather than parse; ``truncated`` means output ran out of
    room, so results may be short. Backends report these instead of raising so
    that one bad batch degrades to low-confidence unknowns instead of killing
    the scan.
    """

    text: str = ""
    error: str | None = None
    truncated: bool = False
    usage: Any = None      # obs.Usage when the backend can report one


class TriageBackend(Protocol):
    name: str    # which backend: "anthropic" | "ollama"
    model: str   # which model it will actually run — worth repeating in logs,
                 # because it is the thing that explains a surprising verdict

    def describe(self) -> str:
        """Human-readable identity, for logs and the UI."""

    def complete(self, *, system: str, user: str, schema: dict[str, Any]) -> Completion:
        ...


# --- hosted ----------------------------------------------------------------

# `output_config.effort` is only accepted by models that support it — Haiku 4.5
# and Sonnet 4.5 reject the request outright. Settings offers Haiku as the cheap
# option, so send the parameter only where it is understood.
EFFORT_MODELS = (
    "claude-fable-", "claude-mythos-",
    "claude-opus-5", "claude-opus-4-5", "claude-opus-4-6",
    "claude-opus-4-7", "claude-opus-4-8",
    "claude-sonnet-5", "claude-sonnet-4-6",
)

# Thinking counts against max_tokens on models that think by default, so this
# has to cover the reasoning as well as ten JSON results. Too low and the batch
# comes back truncated, which costs a full round of tokens for nothing.
MAX_TOKENS = 16000


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, client: Any | None = None) -> None:
        import anthropic

        if client is None:
            # A missing ANTHROPIC_API_KEY is not fatal: the SDK also resolves
            # ANTHROPIC_AUTH_TOKEN and `ant auth login` profiles on its own.
            client = (
                anthropic.Anthropic(api_key=SETTINGS.anthropic_api_key)
                if SETTINGS.anthropic_api_key
                else anthropic.Anthropic()
            )
        self.client = client
        self.model = SETTINGS.triage_model
        self.effort = SETTINGS.triage_effort

    def describe(self) -> str:
        return f"Anthropic API ({self.model})"

    def complete(self, *, system: str, user: str, schema: dict[str, Any]) -> Completion:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.model.startswith(EFFORT_MODELS):
            output_config["effort"] = self.effort

        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            output_config=output_config,
            messages=[{"role": "user", "content": user}],
        )

        from ..obs import usage_from_response

        usage = usage_from_response(response, model=self.model)
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            log.warning("Triage refused: %s", category)
            return Completion(error="model refused to classify", usage=usage)

        text = next((b.text for b in response.content if b.type == "text"), "")
        return Completion(
            text=text, truncated=response.stop_reason == "max_tokens", usage=usage
        )


# --- local -----------------------------------------------------------------


class OllamaBackend:
    """Talks to an Ollama daemon, normally on this machine.

    Ollama constrains decoding to a JSON Schema when one is passed as ``format``,
    which is what makes it a drop-in for structured outputs: the same schema that
    the hosted API enforces server-side is enforced here by the sampler.
    """

    name = "ollama"

    def __init__(self) -> None:
        self.host = SETTINGS.ollama_host.rstrip("/")
        self.model = SETTINGS.ollama_model
        self.num_ctx = SETTINGS.ollama_num_ctx
        self.timeout = SETTINGS.ollama_timeout

    def describe(self) -> str:
        return f"local Ollama ({self.model} @ {self.host})"

    def complete(self, *, system: str, user: str, schema: dict[str, Any]) -> Completion:
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema,
            # Classification wants the same answer every run, and a short batch
            # of emails can otherwise flip label between scans.
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            response = httpx.post(
                f"{self.host}/api/chat", json=payload, timeout=self.timeout
            )
            response.raise_for_status()
            body = response.json()
        except httpx.ConnectError:
            return Completion(
                error=f"no Ollama at {self.host} — start it with `ollama serve`"
            )
        except httpx.TimeoutException:
            return Completion(
                error=f"Ollama did not answer within {self.timeout:.0f}s "
                "(try a smaller model or a smaller TRIAGE_BATCH_SIZE)"
            )
        except httpx.HTTPStatusError as exc:
            detail = _ollama_error(exc.response)
            return Completion(error=f"Ollama rejected the request: {detail}")
        except Exception as exc:  # network stack, bad JSON, anything else
            log.exception("Ollama call failed")
            return Completion(error=f"Ollama call failed: {exc}")

        text = (body.get("message") or {}).get("content", "") or ""
        # Ollama reports why generation stopped; "length" means the context or
        # prediction limit cut the JSON off mid-object.
        truncated = body.get("done_reason") == "length"
        if truncated:
            log.warning(
                "Ollama hit its length limit (num_ctx=%d) — raise OLLAMA_NUM_CTX "
                "or lower TRIAGE_BATCH_SIZE",
                self.num_ctx,
            )
        return Completion(text=text, truncated=truncated)


def _ollama_error(response: httpx.Response) -> str:
    """Ollama puts a useful message in the body; fall back to the status line."""
    try:
        return str(response.json().get("error") or response.text)[:300]
    except Exception:
        return f"HTTP {response.status_code}"


# --- selection -------------------------------------------------------------

BACKENDS = {"anthropic": AnthropicBackend, "ollama": OllamaBackend}


def build_backend(name: str | None = None) -> TriageBackend:
    """Pick a backend by name, defaulting to whatever ``.env`` selected."""
    key = (name or SETTINGS.triage_backend or "anthropic").strip().lower()
    try:
        factory = BACKENDS[key]
    except KeyError:
        raise ValueError(
            f"Unknown TRIAGE_BACKEND {key!r}. Choose one of: {', '.join(sorted(BACKENDS))}."
        ) from None
    return factory()  # type: ignore[abstract]
