"""Write the brief to a file. The default, and deliberately so.

Nothing leaves the machine. The "recipient" is recorded in the document as
provenance — who this was prepared for — rather than used to address anything,
which is why this provider is still checked against ``ALLOWED_RECIPIENTS``:
the same gate should apply whether or not this particular implementation would
have acted on the address.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import SETTINGS
from .base import DeliveryOutcome

log = logging.getLogger(__name__)


class FileDelivery:
    name = "file"
    leaves_machine = False

    def describe(self) -> str:
        return f"file ({SETTINGS.delivery_dir})"

    def deliver(self, *, brief, recipient: str, note: str = "") -> DeliveryOutcome:
        from ..briefs import to_markdown

        try:
            directory = SETTINGS.delivery_dir
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            path = directory / f"{stamp}-{_slug(brief.domain or brief.company)}.md"

            header = [
                f"<!-- prepared for: {recipient} -->",
                f"<!-- approved by: {brief.approved_by or 'unknown'} -->",
                "",
            ]
            body = to_markdown(brief)
            if note:
                body += f"\n\n---\n\n## Note from the approver\n\n{note}\n"
            path.write_text("\n".join(header) + body, encoding="utf-8")
            log.info("Brief written to %s", path)
            return DeliveryOutcome(
                delivered=True, destination=str(path), provider=self.name
            )
        except OSError as exc:
            return DeliveryOutcome(
                error=f"could not write the brief: {exc}", provider=self.name
            )


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "brief").lower()).strip("-")
    return cleaned[:60] or "brief"
