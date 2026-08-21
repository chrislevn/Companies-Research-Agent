"""Provider-agnostic email parsing.

Gmail hands us a JSON payload tree, Microsoft Graph a JSON object, IMAP raw
RFC822 bytes. Everything they have in common — HTML to text, address parsing,
signature extraction, bulk-mail detection — lives here so all three providers
produce identically shaped :class:`EmailMessage` objects.
"""

from __future__ import annotations

import logging
import re
from email.message import Message
from email.utils import getaddresses
from html.parser import HTMLParser
from typing import Iterable, Mapping

from .models import EmailAddress

log = logging.getLogger(__name__)

# Local parts that are almost never a human reaching out.
AUTOMATED_LOCAL_PARTS = (
    "no-reply", "noreply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "notifications", "notification", "alerts", "alert",
    "bounce", "bounces", "news", "newsletter", "marketing", "updates",
    "billing", "invoice", "receipts", "automated", "mailer",
)

# Headers that mark bulk / marketing / system mail.
BULK_HEADERS = ("list-unsubscribe", "list-id", "precedence", "auto-submitted")

SIGNATURE_HINT = re.compile(
    r"(?:^|\n)\s*(?:--\s*\n|best regards|kind regards|regards|thanks|cheers|"
    r"sincerely|tr[aâ]n tr[oọ]ng|th[aâ]n m[eế]n|c[aả]m [oơ]n)\b",
    re.IGNORECASE,
)

# Quoted reply chains — stripping them keeps prompts small and avoids
# re-classifying text the sender never wrote.
QUOTE_MARKERS = (
    re.compile(r"\n-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"\n_{10,}"),
    re.compile(r"\nOn .{0,120}\bwrote:\s*\n", re.IGNORECASE),
    re.compile(r"\nFrom:\s.+\nSent:\s", re.IGNORECASE),
    re.compile(r"\nV[aà]o .{0,120}\b[dđ][ãa] vi[eế]t:\s*\n", re.IGNORECASE),
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self._chunks)).strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        log.debug("HTML parse failed; falling back to tag strip")
        return re.sub(r"<[^>]+>", " ", html).strip()
    return parser.text()


def parse_addresses(raw: str | None) -> list[EmailAddress]:
    if not raw:
        return []
    return [
        EmailAddress(name=name.strip(), email=email.strip().lower())
        for name, email in getaddresses([raw])
        if email
    ]


def strip_quoted_reply(body: str) -> str:
    """Cut the message at the first quoted-reply marker."""
    cut = len(body)
    for marker in QUOTE_MARKERS:
        match = marker.search(body)
        if match:
            cut = min(cut, match.start())
    trimmed = body[:cut].strip()
    # A reply with no new text at all — keep the original rather than nothing.
    return trimmed or body.strip()


def signature_block(body: str) -> str:
    """Tail of the message after the sign-off — where title and phone live."""
    match = SIGNATURE_HINT.search(body)
    if not match:
        return "\n".join(body.strip().splitlines()[-8:])
    return body[match.start():][:800].strip()


def looks_automated(sender_email: str, headers: Mapping[str, str] | Iterable[str]) -> bool:
    email = (sender_email or "").lower()
    local = email.split("@")[0]
    if local and any(local.startswith(p) or p in email for p in AUTOMATED_LOCAL_PARTS):
        return True
    names = headers.keys() if isinstance(headers, Mapping) else headers
    lowered = {name.lower() for name in names}
    return any(h in lowered for h in BULK_HEADERS)


def body_from_rfc822(message: Message) -> str:
    """Best-effort text body from a parsed RFC822 message (IMAP path)."""
    plain, html = "", ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():  # attachment
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
        except (LookupError, ValueError):
            continue
        if content_type == "text/plain" and not plain:
            plain = text
        elif content_type == "text/html" and not html:
            html = text

    if plain.strip():
        return plain.strip()
    return html_to_text(html) if html else ""
