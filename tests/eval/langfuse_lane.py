"""Shared plumbing for the Langfuse eval lane.

The eval harnesses next door print tables and write JSON files. This lane puts
the same experiments where they can be compared, annotated and kept: prompts,
datasets, experiment runs, scores and an annotation queue in Langfuse.

Two clients on purpose. The SDK client covers what it is good at — prompts,
datasets, experiments, scores attached to live traces. The raw API client
covers what the SDK does not wrap (score configs, annotation queues) and is
what `verify` uses for everything, because verification through the same SDK
that did the writing would test the SDK's cache as much as the server.

Unlike obs/langfuse.py this module raises. That file sits in the path of every
scan and must degrade to a no-op; this one is invoked by name (`./start.sh
langfuse …`), and the honest answer to "sync my prompts" with no server running
is an error that says so, not a success that did nothing.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx


class LaneError(RuntimeError):
    """Configuration or connectivity problem the operator has to fix."""


def _settings():
    from companies_research.config import SETTINGS

    return SETTINGS


def require_config():
    """The settings this lane needs, or an error that says how to get them."""
    s = _settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        raise LaneError(
            "Langfuse keys are not configured. Set LANGFUSE_PUBLIC_KEY and "
            "LANGFUSE_SECRET_KEY in .env (the defaults in .env.example match "
            "what docker-compose.langfuse.yml provisions on first boot)."
        )
    return s


def sdk_client() -> Any:
    """The Langfuse SDK client, configured from SETTINGS.

    Goes through obs.langfuse.setup() so the keys land in the environment the
    SDK reads, and so the app and the eval lane can never disagree about which
    server they talk to.
    """
    s = require_config()
    if not s.langfuse_enabled:
        raise LaneError(
            "LANGFUSE_ENABLED is false. Set it to true in .env and start the "
            "stack:  docker compose -f docker-compose.langfuse.yml up -d"
        )
    from companies_research.obs import langfuse as obs_lf

    if not obs_lf.setup():
        raise LaneError(
            f"Could not reach Langfuse at {s.langfuse_host}. Is the stack up? "
            "  docker compose -f docker-compose.langfuse.yml up -d"
        )
    from langfuse import get_client

    return get_client()


class Api:
    """Thin public-API client: basic auth, JSON in, JSON out, errors named."""

    def __init__(self, host: str | None = None, public_key: str | None = None,
                 secret_key: str | None = None, transport: Any = None) -> None:
        s = _settings()
        self.host = (host or s.langfuse_host).rstrip("/")
        token = base64.b64encode(
            f"{public_key or s.langfuse_public_key}:"
            f"{secret_key or s.langfuse_secret_key}".encode()
        ).decode()
        self._client = httpx.Client(
            base_url=self.host,
            headers={"Authorization": f"Basic {token}"},
            timeout=30.0,
            transport=transport,
        )

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise LaneError(
                f"No Langfuse at {self.host} ({exc}). Start it with:\n"
                "  docker compose -f docker-compose.langfuse.yml up -d"
            ) from exc
        if response.status_code == 401:
            raise LaneError(
                "Langfuse rejected the API keys — check LANGFUSE_PUBLIC_KEY "
                "and LANGFUSE_SECRET_KEY in .env"
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise LaneError(
                f"{method} {path} failed: HTTP {response.status_code} "
                f"{response.text[:300]}"
            )
        if not response.content:
            return {}
        return response.json()

    def get(self, path: str, **params: Any) -> Any:
        return self._call("GET", path, params={
            k: v for k, v in params.items() if v is not None
        })

    def post(self, path: str, body: dict) -> Any:
        return self._call("POST", path, json=body)

    def close(self) -> None:
        self._client.close()
