"""Gmail / Google Workspace.

Two auth modes:

``oauth_desktop`` (default)
    Browser consent once, refresh token cached on disk. Zero setup beyond an
    OAuth client — the right choice for one person on their own mailbox.

``service_account``
    A Workspace service account with domain-wide delegation impersonates each
    user. No per-user consent, no tokens to store. Requires a super admin to
    authorise the client ID against the scopes in Admin Console.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..config import GOOGLE_SCOPES, SETTINGS
from ..mime import (
    html_to_text,
    looks_automated,
    parse_addresses,
    signature_block,
    strip_quoted_reply,
)
from ..models import EmailAddress, EmailMessage
from ..secret_refs import resolve_secret
from .base import Account, EmailProvider, Folder, MessageQuery, ProviderError, ProviderProfile

log = logging.getLogger(__name__)


class GmailProvider(EmailProvider):
    provider_id = "gmail"

    def __init__(self, account: Account) -> None:
        super().__init__(account)
        self._service: Any = None

    # -- auth ------------------------------------------------------------

    def _credentials(self):
        mode = self.account.auth.get("type", "oauth_desktop")
        if mode == "oauth_desktop":
            from ..google_auth import get_credentials

            return get_credentials(
                credentials_file=_opt_path(self.account.auth.get("client_secret_file"))
                or SETTINGS.google_credentials_file,
                token_file=_opt_path(self.account.auth.get("token_file"))
                or SETTINGS.google_token_file,
            )

        if mode == "service_account":
            from google.oauth2 import service_account

            key_file = _opt_path(self.account.auth.get("key_file"))
            key_json = resolve_secret(
                self.account.auth.get("key_json"), what=f"{self.account.account_id} key_json"
            )
            if key_json:
                import json

                creds = service_account.Credentials.from_service_account_info(
                    json.loads(key_json), scopes=GOOGLE_SCOPES
                )
            elif key_file and key_file.exists():
                creds = service_account.Credentials.from_service_account_file(
                    str(key_file), scopes=GOOGLE_SCOPES
                )
            else:
                raise ProviderError(
                    f"{self.account.account_id}: service_account auth needs key_file or key_json"
                )
            # Domain-wide delegation: act as this mailbox's owner.
            return creds.with_subject(self.account.auth.get("subject") or self.account.email)

        raise ProviderError(f"{self.account.account_id}: unknown gmail auth type {mode!r}")

    @property
    def service(self) -> Any:
        if self._service is None:
            from googleapiclient.discovery import build

            self._service = build(
                "gmail", "v1", credentials=self._credentials(), cache_discovery=False
            )
        return self._service

    # -- provider API ----------------------------------------------------

    def verify(self) -> ProviderProfile:
        profile = self.service.users().getProfile(userId="me").execute()
        return ProviderProfile(
            email=profile["emailAddress"],
            total_messages=profile.get("messagesTotal"),
        )

    def fetch(self, query: MessageQuery) -> list[EmailMessage]:
        search = query.raw or _to_gmail_query(query)
        log.info("[%s] gmail search: %s", self.account.account_id, search)

        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < query.max_results:
            resp = (
                self.service.users()
                .messages()
                .list(
                    userId="me",
                    q=search,
                    maxResults=min(100, query.max_results - len(ids)),
                    pageToken=page_token,
                )
                .execute()
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        messages: list[EmailMessage] = []
        for message_id in ids:
            raw = (
                self.service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
            try:
                messages.append(self._stamp(parse_gmail_message(raw)))
            except Exception:
                log.exception("[%s] failed to parse message %s", self.account.account_id, message_id)

        messages.sort(key=_received_key, reverse=True)
        return messages


# ---------------------------------------------------------------------------


def _opt_path(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def _received_key(message: EmailMessage) -> datetime:
    return message.received_at or datetime.min.replace(tzinfo=timezone.utc)


def _to_gmail_query(query: MessageQuery) -> str:
    parts = ["-in:chats"]
    parts.append("in:sent" if query.folder is Folder.SENT else "in:inbox")
    if query.since:
        parts.append(f"after:{int(query.since.timestamp())}")
    if query.until:
        parts.append(f"before:{int(query.until.timestamp())}")
    return " ".join(parts)


def _walk_parts(payload: dict) -> Iterator[dict]:
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _walk_parts(part)


def _decode(data: str | None) -> str:
    if not data:
        return ""
    import base64

    return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")


def extract_body(payload: dict) -> str:
    plain, html = "", ""
    for part in _walk_parts(payload):
        mime_type = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if not data:
            continue
        if mime_type == "text/plain" and not plain:
            plain = _decode(data)
        elif mime_type == "text/html" and not html:
            html = _decode(data)
    if plain.strip():
        return plain.strip()
    return html_to_text(html) if html else ""


def parse_gmail_message(raw: dict) -> EmailMessage:
    payload = raw.get("payload", {}) or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", []) or []}

    senders = parse_addresses(headers.get("from"))
    sender = senders[0] if senders else EmailAddress()

    received_at: datetime | None = None
    if internal := raw.get("internalDate"):
        received_at = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
    elif date_header := headers.get("date"):
        from email.utils import parsedate_to_datetime

        try:
            received_at = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            received_at = None

    body = strip_quoted_reply(extract_body(payload))

    return EmailMessage(
        message_id=raw["id"],
        thread_id=raw.get("threadId", ""),
        subject=headers.get("subject", ""),
        sender=sender,
        to=parse_addresses(headers.get("to")),
        cc=parse_addresses(headers.get("cc")),
        received_at=received_at,
        snippet=raw.get("snippet", ""),
        body_text=body,
        labels=raw.get("labelIds", []) or [],
        is_automated=looks_automated(sender.email, headers),
        signature_block=signature_block(body),
    )
