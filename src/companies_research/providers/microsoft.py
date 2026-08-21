"""Microsoft 365 / Outlook via Microsoft Graph.

Two auth modes:

``device_code`` (default)
    Interactive sign-in, token cached on disk. Works for a single work or
    personal Microsoft account with no tenant admin involvement.

``client_credentials``
    App-only access with the ``Mail.Read`` **application** permission and admin
    consent — the Graph counterpart of Google's domain-wide delegation.

    ⚠️ ``Mail.Read`` as an application permission grants access to *every*
    mailbox in the tenant. Restrict it with an Exchange Online application
    access policy (``New-ApplicationAccessPolicy``) scoped to a mail-enabled
    security group, or the agent can read the whole company's email.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..mime import (
    html_to_text,
    looks_automated,
    signature_block,
    strip_quoted_reply,
)
from ..models import EmailAddress, EmailMessage
from ..secret_refs import resolve_secret
from .base import Account, EmailProvider, Folder, MessageQuery, ProviderError, ProviderProfile

log = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE_APP = ["https://graph.microsoft.com/.default"]
GRAPH_SCOPE_DELEGATED = ["Mail.Read"]

MESSAGE_FIELDS = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,"
    "receivedDateTime,bodyPreview,body,internetMessageHeaders,categories"
)
# internetMessageHeaders is not selectable on every tenant/mailbox; on a 400 we
# retry without it and lose only bulk-mail header detection.
MESSAGE_FIELDS_FALLBACK = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,"
    "receivedDateTime,bodyPreview,body,categories"
)

FOLDER_IDS = {Folder.INBOX: "inbox", Folder.SENT: "sentitems"}


class MicrosoftGraphProvider(EmailProvider):
    provider_id = "microsoft"

    def __init__(self, account: Account) -> None:
        super().__init__(account)
        self._client: httpx.Client | None = None
        self._token: str | None = None

    # -- auth ------------------------------------------------------------

    def _acquire_token(self) -> str:
        try:
            import msal
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "Microsoft provider needs the 'msal' package: pip install msal"
            ) from exc

        auth = self.account.auth
        mode = auth.get("type", "device_code")
        tenant = auth.get("tenant_id", "common")
        client_id = auth.get("client_id")
        if not client_id:
            raise ProviderError(f"{self.account.account_id}: microsoft auth needs client_id")
        authority = f"https://login.microsoftonline.com/{tenant}"

        if mode == "client_credentials":
            secret = resolve_secret(
                auth.get("client_secret"), what=f"{self.account.account_id} client_secret"
            )
            if not secret:
                raise ProviderError(
                    f"{self.account.account_id}: client_credentials needs client_secret"
                )
            app = msal.ConfidentialClientApplication(
                client_id, authority=authority, client_credential=secret
            )
            result = app.acquire_token_for_client(scopes=GRAPH_SCOPE_APP)

        elif mode == "device_code":
            cache_file = Path(
                auth.get("token_cache", f"credentials/msal-{self.account.account_id}.json")
            ).expanduser()
            cache = msal.SerializableTokenCache()
            if cache_file.exists():
                cache.deserialize(cache_file.read_text(encoding="utf-8"))

            app = msal.PublicClientApplication(client_id, authority=authority, token_cache=cache)
            result = None
            for existing in app.get_accounts(username=self.account.email):
                result = app.acquire_token_silent(GRAPH_SCOPE_DELEGATED, account=existing)
                if result:
                    break

            if not result:
                flow = app.initiate_device_flow(scopes=GRAPH_SCOPE_DELEGATED)
                if "user_code" not in flow:
                    raise ProviderError(f"Device flow failed to start: {flow}")
                print(flow["message"], flush=True)
                result = app.acquire_token_by_device_flow(flow)

            if cache.has_state_changed:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(cache.serialize(), encoding="utf-8")
                cache_file.chmod(0o600)
        else:
            raise ProviderError(f"{self.account.account_id}: unknown microsoft auth type {mode!r}")

        if not result or "access_token" not in result:
            raise ProviderError(
                f"{self.account.account_id}: Microsoft auth failed: "
                f"{(result or {}).get('error_description', 'no token returned')}"
            )
        return result["access_token"]

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._token = self._acquire_token()
            self._client = httpx.Client(
                base_url=GRAPH_ROOT,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=60.0,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def _mailbox_root(self) -> str:
        """App-only tokens have no /me; address the mailbox explicitly."""
        if self.account.auth.get("type", "device_code") == "client_credentials":
            return f"/users/{self.account.email}"
        return "/me"

    # -- provider API ----------------------------------------------------

    def verify(self) -> ProviderProfile:
        resp = self.client.get(f"{self._mailbox_root}")
        _raise_for_graph(resp)
        data = resp.json()
        return ProviderProfile(
            email=data.get("mail") or data.get("userPrincipalName", self.account.email),
            display_name=data.get("displayName", ""),
        )

    def fetch(self, query: MessageQuery) -> list[EmailMessage]:
        folder = FOLDER_IDS[query.folder]
        params: dict[str, Any] = {
            "$top": min(50, query.max_results),
            "$orderby": "receivedDateTime desc",
            "$select": MESSAGE_FIELDS,
        }
        if filter_clause := query.raw or _to_odata_filter(query):
            params["$filter"] = filter_clause

        url = f"{self._mailbox_root}/mailFolders/{folder}/messages"
        log.info("[%s] graph query: %s %s", self.account.account_id, url, params.get("$filter", ""))

        messages: list[EmailMessage] = []
        next_url: str | None = url
        first = True

        while next_url and len(messages) < query.max_results:
            resp = (
                self.client.get(next_url, params=params)
                if first
                else self.client.get(next_url)
            )
            if first and resp.status_code == 400 and "internetMessageHeaders" in resp.text:
                log.warning("[%s] internetMessageHeaders unsupported; retrying", self.account.account_id)
                params["$select"] = MESSAGE_FIELDS_FALLBACK
                resp = self.client.get(next_url, params=params)
            _raise_for_graph(resp)
            first = False

            payload = resp.json()
            for item in payload.get("value", []):
                try:
                    messages.append(self._stamp(parse_graph_message(item)))
                except Exception:
                    log.exception("[%s] failed to parse message %s", self.account.account_id, item.get("id"))
            next_url = payload.get("@odata.nextLink")

        return messages[: query.max_results]


# ---------------------------------------------------------------------------


def _raise_for_graph(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    try:
        detail = resp.json()["error"]["message"]
    except Exception:
        detail = resp.text[:300]
    raise ProviderError(f"Graph {resp.status_code}: {detail}")


def _to_odata_filter(query: MessageQuery) -> str:
    clauses = []
    if query.since:
        clauses.append(f"receivedDateTime ge {_odata_time(query.since)}")
    if query.until:
        clauses.append(f"receivedDateTime le {_odata_time(query.until)}")
    return " and ".join(clauses)


def _odata_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _address(entry: dict | None) -> EmailAddress:
    payload = (entry or {}).get("emailAddress", {}) or {}
    return EmailAddress(
        name=payload.get("name", "") or "",
        email=(payload.get("address", "") or "").lower(),
    )


def parse_graph_message(item: dict) -> EmailMessage:
    sender = _address(item.get("from"))

    body_payload = item.get("body", {}) or {}
    content = body_payload.get("content", "") or ""
    body = (
        html_to_text(content)
        if (body_payload.get("contentType", "") or "").lower() == "html"
        else content
    )
    body = strip_quoted_reply(body.strip())

    received_at: datetime | None = None
    if raw_time := item.get("receivedDateTime"):
        try:
            received_at = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        except ValueError:
            received_at = None

    headers = {
        (h.get("name") or "").lower(): h.get("value", "")
        for h in item.get("internetMessageHeaders", []) or []
    }

    return EmailMessage(
        message_id=item["id"],
        thread_id=item.get("conversationId", "") or "",
        subject=item.get("subject", "") or "",
        sender=sender,
        to=[_address(r) for r in item.get("toRecipients", []) or []],
        cc=[_address(r) for r in item.get("ccRecipients", []) or []],
        received_at=received_at,
        snippet=item.get("bodyPreview", "") or "",
        body_text=body,
        labels=item.get("categories", []) or [],
        is_automated=looks_automated(sender.email, headers),
        signature_block=signature_block(body),
    )
