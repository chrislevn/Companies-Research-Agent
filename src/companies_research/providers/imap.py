"""Generic IMAP — the catch-all for everything that isn't Gmail or Microsoft.

Covers Zoho, Fastmail, iCloud, Yahoo, on-prem Exchange, and self-hosted mail.
Auth is an app password (``env:`` / ``file:`` reference) or an OAuth2 access
token via XOAUTH2.

IMAP ``SEARCH SINCE`` only has *date* granularity, so results are re-filtered
client-side against the exact ``since``/``until`` timestamps.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from ..mime import (
    body_from_rfc822,
    looks_automated,
    parse_addresses,
    signature_block,
    strip_quoted_reply,
)
from ..models import EmailAddress, EmailMessage
from ..secret_refs import resolve_secret
from .base import Account, EmailProvider, Folder, MessageQuery, ProviderError, ProviderProfile

log = logging.getLogger(__name__)

# Tried in order when the server advertises no \Sent special-use folder.
SENT_FOLDER_GUESSES = ("Sent", "INBOX.Sent", "Sent Items", "Sent Messages", "[Gmail]/Sent Mail")

_LIST_LINE = re.compile(rb'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)')


class ImapProvider(EmailProvider):
    provider_id = "imap"

    def __init__(self, account: Account) -> None:
        super().__init__(account)
        self._conn: imaplib.IMAP4_SSL | None = None

    # -- connection ------------------------------------------------------

    def _connect(self) -> imaplib.IMAP4_SSL:
        auth = self.account.auth
        host = auth.get("host")
        if not host:
            raise ProviderError(f"{self.account.account_id}: imap auth needs host")
        port = int(auth.get("port", 993))
        username = auth.get("username") or self.account.email

        try:
            conn = imaplib.IMAP4_SSL(host, port)
        except OSError as exc:
            raise ProviderError(f"{self.account.account_id}: cannot reach {host}:{port} — {exc}") from exc

        try:
            if token_ref := auth.get("access_token"):
                token = resolve_secret(token_ref, what=f"{self.account.account_id} access_token")
                payload = f"user={username}\x01auth=Bearer {token}\x01\x01"
                conn.authenticate("XOAUTH2", lambda _: payload.encode())
            else:
                password = resolve_secret(
                    auth.get("password"), what=f"{self.account.account_id} password"
                )
                if not password:
                    raise ProviderError(
                        f"{self.account.account_id}: imap auth needs password or access_token"
                    )
                conn.login(username, password)
        except imaplib.IMAP4.error as exc:
            raise ProviderError(f"{self.account.account_id}: IMAP login failed — {exc}") from exc

        return conn

    @property
    def conn(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.logout()
        except Exception:  # logout on a dead socket is not worth surfacing
            log.debug("[%s] IMAP logout failed", self.account.account_id, exc_info=True)
        finally:
            self._conn = None

    # -- folders ---------------------------------------------------------

    def _resolve_folder(self, folder: Folder) -> str:
        if override := self.account.options.get(f"{folder.value}_folder"):
            return str(override)
        if folder is Folder.INBOX:
            return "INBOX"

        status, lines = self.conn.list()
        if status == "OK":
            for line in lines or []:
                match = _LIST_LINE.match(line if isinstance(line, bytes) else bytes(line))
                if match and b"\\Sent" in match.group("flags"):
                    return match.group("name").decode().strip('"')

        for guess in SENT_FOLDER_GUESSES:
            if self.conn.select(f'"{guess}"', readonly=True)[0] == "OK":
                return guess
        raise ProviderError(
            f"{self.account.account_id}: cannot find the Sent folder; "
            f"set options.sent_folder explicitly"
        )

    # -- provider API ----------------------------------------------------

    def verify(self) -> ProviderProfile:
        status, _ = self.conn.select("INBOX", readonly=True)
        if status != "OK":
            raise ProviderError(f"{self.account.account_id}: cannot select INBOX")
        return ProviderProfile(email=self.account.email)

    def fetch(self, query: MessageQuery) -> list[EmailMessage]:
        mailbox = self._resolve_folder(query.folder)
        status, _ = self.conn.select(f'"{mailbox}"', readonly=True)
        if status != "OK":
            raise ProviderError(f"{self.account.account_id}: cannot select {mailbox}")

        criteria = query.raw or _to_imap_criteria(query)
        log.info("[%s] imap search in %s: %s", self.account.account_id, mailbox, criteria)

        status, data = self.conn.uid("SEARCH", None, criteria)  # type: ignore[arg-type]
        if status != "OK":
            raise ProviderError(f"{self.account.account_id}: IMAP SEARCH failed")

        uids = (data[0] or b"").split()
        uids = uids[-query.max_results :]  # server returns oldest-first
        uids.reverse()

        messages: list[EmailMessage] = []
        for uid in uids:
            status, payload = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                log.warning("[%s] could not fetch uid %s", self.account.account_id, uid)
                continue
            try:
                parsed = parse_rfc822(payload[0][1], uid.decode())
            except Exception:
                log.exception("[%s] failed to parse uid %s", self.account.account_id, uid)
                continue
            if query.matches(parsed):  # SEARCH SINCE is date-only; refine here
                messages.append(self._stamp(parsed))

        messages.sort(
            key=lambda m: m.received_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True
        )
        return messages


# ---------------------------------------------------------------------------


def _to_imap_criteria(query: MessageQuery) -> str:
    parts: list[str] = []
    if query.since:
        parts.append(f"SINCE {query.since.strftime('%d-%b-%Y')}")
    if query.until:
        parts.append(f"BEFORE {query.until.strftime('%d-%b-%Y')}")
    return " ".join(parts) if parts else "ALL"


def _decode_header(raw: Any) -> str:
    if raw is None:
        return ""
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(str(raw))))
    except Exception:
        return str(raw)


def parse_rfc822(raw_bytes: bytes, uid: str) -> EmailMessage:
    parsed = email.message_from_bytes(raw_bytes)

    senders = parse_addresses(_decode_header(parsed.get("From")))
    sender = senders[0] if senders else EmailAddress()

    received_at: datetime | None = None
    if date_header := parsed.get("Date"):
        try:
            received_at = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            received_at = None
    if received_at is not None and received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)

    body = strip_quoted_reply(body_from_rfc822(parsed))
    headers = {k.lower(): v for k, v in parsed.items()}

    return EmailMessage(
        message_id=parsed.get("Message-ID", "").strip("<>") or f"uid-{uid}",
        thread_id=(parsed.get("In-Reply-To") or parsed.get("References") or "").split()[0].strip("<>")
        if (parsed.get("In-Reply-To") or parsed.get("References"))
        else "",
        subject=_decode_header(parsed.get("Subject")),
        sender=sender,
        to=parse_addresses(_decode_header(parsed.get("To"))),
        cc=parse_addresses(_decode_header(parsed.get("Cc"))),
        received_at=received_at,
        snippet=body[:200],
        body_text=body,
        is_automated=looks_automated(sender.email, headers),
        signature_block=signature_block(body),
    )
