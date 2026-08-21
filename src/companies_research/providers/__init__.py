"""Email provider registry."""

from __future__ import annotations

from typing import Type

from .base import (
    Account,
    EmailProvider,
    Folder,
    MessageQuery,
    ProviderError,
    ProviderProfile,
)

_REGISTRY: dict[str, Type[EmailProvider]] = {}


def register(cls: Type[EmailProvider]) -> Type[EmailProvider]:
    _REGISTRY[cls.provider_id] = cls
    return cls


def available_providers() -> list[str]:
    _load_builtins()
    return sorted(_REGISTRY)


def build_provider(account: Account) -> EmailProvider:
    _load_builtins()
    try:
        cls = _REGISTRY[account.provider]
    except KeyError:
        raise ProviderError(
            f"{account.account_id}: unknown provider {account.provider!r}. "
            f"Available: {', '.join(sorted(_REGISTRY))}"
        ) from None
    return cls(account)


def _load_builtins() -> None:
    if _REGISTRY:
        return
    from .gmail import GmailProvider
    from .imap import ImapProvider
    from .microsoft import MicrosoftGraphProvider

    for cls in (GmailProvider, MicrosoftGraphProvider, ImapProvider):
        register(cls)


__all__ = [
    "Account",
    "EmailProvider",
    "Folder",
    "MessageQuery",
    "ProviderError",
    "ProviderProfile",
    "available_providers",
    "build_provider",
    "register",
]
