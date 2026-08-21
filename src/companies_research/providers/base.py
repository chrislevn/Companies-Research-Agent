"""The contract every email backend implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, ClassVar

from ..models import EmailMessage


class Folder(str, Enum):
    INBOX = "inbox"
    SENT = "sent"


@dataclass(frozen=True)
class MessageQuery:
    """Provider-neutral search.

    Each backend translates this into its own dialect — Gmail search operators,
    OData ``$filter``, or IMAP ``SEARCH``. ``raw`` is an escape hatch for
    provider-native syntax and skips translation entirely.
    """

    since: datetime | None = None
    until: datetime | None = None
    folder: Folder = Folder.INBOX
    max_results: int = 100
    raw: str | None = None

    @classmethod
    def recent(cls, age: timedelta, **kwargs: Any) -> "MessageQuery":
        return cls(since=datetime.now(timezone.utc) - age, **kwargs)

    def matches(self, message: EmailMessage) -> bool:
        """Client-side re-check — IMAP SEARCH only has date granularity."""
        received = message.received_at
        if received is None:
            return True
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        if self.since and received < self.since:
            return False
        if self.until and received > self.until:
            return False
        return True


@dataclass(frozen=True)
class ProviderProfile:
    email: str
    display_name: str = ""
    total_messages: int | None = None


@dataclass(frozen=True)
class Account:
    """A single mailbox the agent watches.

    ``auth`` and ``options`` are provider-specific. Secret values are stored as
    references (``env:NAME`` / ``file:/path``) and resolved at use — see
    :mod:`companies_research.secret_refs`.
    """

    account_id: str
    provider: str
    email: str
    user_id: str = "default"
    label: str = ""
    enabled: bool = True
    auth: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1].lower() if "@" in self.email else ""

    def describe(self) -> str:
        return f"{self.account_id} [{self.provider}] {self.email}"


class ProviderError(RuntimeError):
    """Raised for auth or transport failures a provider can't recover from."""


class EmailProvider(ABC):
    """Read access to one mailbox.

    Implementations must be safe to construct without doing network I/O, so a
    misconfigured account fails on ``verify()`` / ``fetch()`` rather than at
    import or startup.
    """

    provider_id: ClassVar[str]

    def __init__(self, account: Account) -> None:
        self.account = account

    @abstractmethod
    def verify(self) -> ProviderProfile:
        """Confirm credentials work. Called by `accounts check`."""

    @abstractmethod
    def fetch(self, query: MessageQuery) -> list[EmailMessage]:
        """Return messages matching ``query``, newest first."""

    def close(self) -> None:  # pragma: no cover - most providers are stateless
        return None

    def __enter__(self) -> "EmailProvider":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- helpers shared by implementations -------------------------------

    def _stamp(self, message: EmailMessage) -> EmailMessage:
        """Attach provider/account identity so `uid` is globally unique."""
        return message.model_copy(
            update={
                "provider": self.provider_id,
                "account_id": self.account.account_id,
                "account_email": self.account.email,
            }
        )
