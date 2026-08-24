"""Which mailboxes the agent watches.

Two modes, same code path:

**Solo** — no config file. One Gmail account is synthesised from ``.env``
(``USER_EMAILS``), using the browser OAuth flow. Nothing to configure.

**Team / enterprise** — an ``accounts.json`` listing any number of mailboxes
across Gmail, Microsoft 365 and IMAP, each with its own auth block and
``user_id``. Secrets are references (``env:`` / ``file:``), never literals, so
the file is safe to commit or ship in a config map.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .config import ROOT, SETTINGS
from .providers import Account

log = logging.getLogger(__name__)

DEFAULT_ACCOUNTS_FILENAMES = ("accounts.json", "config/accounts.json")


class AccountsError(RuntimeError):
    pass


def accounts_file() -> Path | None:
    if configured := os.getenv("ACCOUNTS_FILE"):
        path = Path(configured)
        return path if path.is_absolute() else ROOT / path
    for name in DEFAULT_ACCOUNTS_FILENAMES:
        candidate = ROOT / name
        if candidate.exists():
            return candidate
    return None


def load_accounts(*, include_disabled: bool = False) -> list[Account]:
    path = accounts_file()
    accounts = _from_file(path) if path and path.exists() else [_solo_gmail_account()]
    if include_disabled:
        return accounts
    return [a for a in accounts if a.enabled]


# ---------------------------------------------------------------------------


def _solo_gmail_account() -> Account:
    """Single-user fallback: the first address in USER_EMAILS, over Gmail OAuth."""
    email = SETTINGS.user_emails[0] if SETTINGS.user_emails else ""
    return Account(
        account_id="default",
        provider="gmail",
        email=email,
        user_id="default",
        label="Personal Gmail",
        auth={"type": "oauth_desktop"},
    )


def _from_file(path: Path) -> list[Account]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AccountsError(f"{path} is not valid JSON: {exc}") from exc

    entries = payload.get("accounts") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise AccountsError(f"{path}: expected a list of accounts (or {{'accounts': [...]}})")

    accounts: list[Account] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        account = _parse_entry(entry, path, index)
        if account.account_id in seen:
            raise AccountsError(f"{path}: duplicate account_id {account.account_id!r}")
        seen.add(account.account_id)
        accounts.append(account)

    log.info("Loaded %d account(s) from %s", len(accounts), path)
    return accounts


def _parse_entry(entry: object, path: Path, index: int) -> Account:
    if not isinstance(entry, dict):
        raise AccountsError(f"{path}: account #{index} is not an object")
    for required in ("account_id", "provider", "email"):
        if not entry.get(required):
            raise AccountsError(f"{path}: account #{index} is missing {required!r}")

    return Account(
        account_id=str(entry["account_id"]),
        provider=str(entry["provider"]).lower(),
        email=str(entry["email"]).lower(),
        user_id=str(entry.get("user_id", "default")),
        label=str(entry.get("label", "")),
        enabled=bool(entry.get("enabled", True)),
        auth=dict(entry.get("auth", {}) or {}),
        options=dict(entry.get("options", {}) or {}),
    )
