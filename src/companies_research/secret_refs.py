"""Indirect secret references, so account config files carry no literal secrets.

Supported forms:

    env:MS_CLIENT_SECRET     read from the environment
    file:/run/secrets/token  read from a file (Docker/Kubernetes secret mounts)
    <anything else>          treated as a literal value (dev only)

Vault / Secret Manager backends slot in as extra schemes here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class SecretNotFound(RuntimeError):
    pass


def resolve_secret(ref: str | None, *, what: str = "secret") -> str | None:
    if not ref:
        return None

    if ref.startswith("env:"):
        name = ref[4:]
        value = os.getenv(name)
        if not value:
            raise SecretNotFound(f"{what}: environment variable {name} is not set")
        return value

    if ref.startswith("file:"):
        path = Path(ref[5:]).expanduser()
        if not path.exists():
            raise SecretNotFound(f"{what}: secret file {path} does not exist")
        return path.read_text(encoding="utf-8").strip()

    log.warning(
        "%s is configured as a literal value; use env: or file: outside development", what
    )
    return ref
