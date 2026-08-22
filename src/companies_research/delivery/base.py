"""Where an approved brief goes.

Two implementations, and the default writes to a file. That is a deliberate
asymmetry: reading mail is what this agent is for, sending it is a different
kind of act, and the safe option should be the one you get without asking.

The reading mailbox is never a sending mailbox. Its OAuth token carries
``gmail.readonly`` and nothing else, and step 5 does not change that — sending
requires a separate account, separately consented, named explicitly in
``.env``. An agent that could send from the mailbox it reads is one prompt
injection away from being a mail relay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class DeliveryOutcome:
    """What happened when a brief was handed over."""

    delivered: bool = False
    destination: str = ""     # a path, or an address — where it actually went
    error: str = ""
    provider: str = ""

    @property
    def ok(self) -> bool:
        return self.delivered and not self.error


class DeliveryProvider(Protocol):
    name: str
    # True when the brief leaves this machine. The UI says so out loud, because
    # "saved a file" and "emailed a third party" deserve different hesitation.
    leaves_machine: bool

    def describe(self) -> str:
        """Human-readable identity, for the UI and the audit trail."""

    def deliver(self, *, brief, recipient: str, note: str = "") -> DeliveryOutcome:
        """Hand the brief over. Never raises; failures come back as an outcome."""
        ...


class DeliveryError(RuntimeError):
    """Configuration is wrong — as opposed to one delivery failing."""
