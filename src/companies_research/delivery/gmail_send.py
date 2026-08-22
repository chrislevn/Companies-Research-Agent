"""Send a brief by email, from an account that is not the one being read.

The guard in this module is the point of it. The reading mailbox holds a token
scoped to ``gmail.readonly``; sending needs a different scope, which means a
different consent, which means a different account. Rather than let that happen
by accident, this refuses to start if ``DELIVERY_ACCOUNT`` resolves to a
mailbox the agent reads.

The reasoning is not tidiness. An agent that reads untrusted email and can send
from the same box is a mail relay with a language model deciding the recipient.
Keeping the two apart means a compromised triage cannot become outbound mail
even if every other control failed.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage as MimeMessage

from ..config import SETTINGS
from .base import DeliveryError, DeliveryOutcome

log = logging.getLogger(__name__)

SEND_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class GmailSendDelivery:
    name = "gmail_send"
    leaves_machine = True

    def __init__(self) -> None:
        self.account_id = (SETTINGS.delivery_account or "").strip()
        if not self.account_id:
            raise DeliveryError(
                "DELIVERY_PROVIDER=gmail_send needs DELIVERY_ACCOUNT — the id of a "
                "SEPARATE mailbox to send from. The mailbox the agent reads is "
                "consented read-only and must stay that way."
            )
        self._assert_not_a_reading_mailbox()

    def describe(self) -> str:
        return f"gmail send (from {self.account_id})"

    # ------------------------------------------------------------------

    def _assert_not_a_reading_mailbox(self) -> None:
        """Refuse to send from any mailbox this agent reads."""
        try:
            from ..accounts import load_accounts

            reading = {a.account_id for a in load_accounts() if a.enabled}
        except Exception:
            reading = set()

        if self.account_id in reading:
            raise DeliveryError(
                f"DELIVERY_ACCOUNT {self.account_id!r} is a mailbox this agent reads. "
                "Sending must use a separate account with its own consent, so that "
                "a compromised triage can never become outbound mail."
            )

    def _credentials(self):
        from ..google_auth import get_credentials

        token_file = SETTINGS.credentials_dir / f"token-send-{self.account_id}.json"
        if not token_file.exists():
            raise DeliveryError(
                f"no send token at {token_file}. Authorise the delivery account "
                "separately — it needs gmail.send, which the reading mailbox does "
                "not have and must not be given."
            )
        return get_credentials(
            token_file=token_file, scopes=SEND_SCOPES, consent_timeout=60
        )

    def deliver(self, *, brief, recipient: str, note: str = "") -> DeliveryOutcome:
        from ..briefs import to_markdown

        try:
            from googleapiclient.discovery import build

            service = build("gmail", "v1", credentials=self._credentials(),
                            cache_discovery=False)

            message = MimeMessage()
            message["To"] = recipient
            message["Subject"] = f"Brief: {brief.company}"
            body = to_markdown(brief)
            if note:
                body += f"\n\n---\n\n## Note from the approver\n\n{note}\n"
            message.set_content(body)

            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            log.info("Brief %s sent to %s (%s)", brief.lead_id, recipient, sent.get("id"))
            return DeliveryOutcome(
                delivered=True, destination=recipient, provider=self.name
            )
        except DeliveryError as exc:
            return DeliveryOutcome(error=str(exc), provider=self.name)
        except Exception as exc:
            log.exception("Sending the brief failed")
            return DeliveryOutcome(
                error=f"{type(exc).__name__}: {exc}", provider=self.name
            )
