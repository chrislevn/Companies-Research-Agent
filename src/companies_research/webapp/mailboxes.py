"""Adding and removing mailboxes from the UI.

The CLI reads ``accounts.json`` if it exists and otherwise synthesises a single
Gmail account from ``.env``. The UI always writes ``accounts.json`` — one code
path for "my mailbox" and "our five mailboxes", so adding a second one later is
not a migration.

Secrets never land in ``accounts.json``. A password typed into the UI is written
to ``.env`` as ``IMAP_PASSWORD_<SLUG>`` and referenced as ``env:IMAP_PASSWORD_<SLUG>``,
which is the same indirection an enterprise deployment uses with a secret manager.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..accounts import accounts_file, load_accounts
from ..config import ROOT, SETTINGS, set_env_values
from ..providers import Account, ProviderError, build_provider
from ..providers.base import ProviderProfile

log = logging.getLogger(__name__)

ACCOUNTS_PATH = ROOT / "accounts.json"

# Everything a non-technical user needs to connect a mailbox with an app
# password: the server settings they would otherwise have to look up, and a
# direct link to the page where the password is generated.
PRESETS: dict[str, dict] = {
    "gmail": {
        "name": "Gmail / Google Workspace",
        "domains": ["gmail.com", "googlemail.com"],
        "host": "imap.gmail.com",
        "port": 993,
        "app_password_url": "https://myaccount.google.com/apppasswords",
        "needs_2fa": True,
    },
    "outlook": {
        "name": "Outlook / Hotmail / Microsoft 365",
        "domains": ["outlook.com", "hotmail.com", "live.com", "msn.com"],
        "host": "outlook.office365.com",
        "port": 993,
        "app_password_url": "https://account.live.com/proofs/AppPassword",
        "needs_2fa": True,
    },
    "yahoo": {
        "name": "Yahoo Mail",
        "domains": ["yahoo.com", "yahoo.co.uk", "ymail.com"],
        "host": "imap.mail.yahoo.com",
        "port": 993,
        "app_password_url": "https://login.yahoo.com/myaccount/security/app-password",
        "needs_2fa": True,
    },
    "icloud": {
        "name": "iCloud Mail",
        "domains": ["icloud.com", "me.com", "mac.com"],
        "host": "imap.mail.me.com",
        "port": 993,
        "app_password_url": "https://account.apple.com/account/manage",
        "needs_2fa": True,
    },
    "zoho": {
        "name": "Zoho Mail",
        "domains": ["zoho.com", "zohomail.com"],
        "host": "imap.zoho.com",
        "port": 993,
        "app_password_url": "https://accounts.zoho.com/home#security/app_password",
        "needs_2fa": False,
    },
    "fastmail": {
        "name": "Fastmail",
        "domains": ["fastmail.com", "fastmail.fm"],
        "host": "imap.fastmail.com",
        "port": 993,
        "app_password_url": "https://app.fastmail.com/settings/security/apps",
        "needs_2fa": False,
    },
    "other": {
        "name": "Other / company mail server",
        "domains": [],
        "host": "",
        "port": 993,
        "app_password_url": "",
        "needs_2fa": False,
    },
}


def preset_for(email: str) -> str:
    domain = email.split("@")[-1].lower() if "@" in email else ""
    for key, preset in PRESETS.items():
        if domain in preset["domains"]:
            return key
    return "other"


def presets_payload() -> list[dict]:
    return [{"id": key, **preset} for key, preset in PRESETS.items()]


# ---------------------------------------------------------------------------


class MailboxError(RuntimeError):
    pass


def _slug(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", email.lower()).strip("-") or "mailbox"


def _env_key(account_id: str) -> str:
    return "IMAP_PASSWORD_" + re.sub(r"[^A-Z0-9]+", "_", account_id.upper())


def configured_accounts(*, include_disabled: bool = True) -> list[Account]:
    """Mailboxes the UI should show as connected.

    ``load_accounts`` always returns *something* — with no config file it
    synthesises a solo Gmail account from ``.env``. That placeholder is not a
    working mailbox, and showing it would make the setup wizard skip step one
    and leave the user with an app that quietly does nothing. Only count the
    fallback once the CLI has actually authorised it.
    """
    accounts = load_accounts(include_disabled=include_disabled)
    if accounts_file():
        return accounts
    return [a for a in accounts if a.email and SETTINGS.google_token_file.exists()]


def _read_raw() -> list[dict]:
    if not ACCOUNTS_PATH.exists():
        return []
    payload = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    entries = payload.get("accounts") if isinstance(payload, dict) else payload
    return list(entries or [])


def _write_raw(entries: list[dict]) -> None:
    ACCOUNTS_PATH.write_text(
        json.dumps({"accounts": entries}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _sync_user_emails(entries)


def _sync_user_emails(entries: list[dict]) -> None:
    """Keep USER_EMAILS in step so the agent never flags your own mail as a lead."""
    emails = sorted({str(e.get("email", "")).lower() for e in entries if e.get("email")})
    if emails:
        set_env_values({"USER_EMAILS": ",".join(emails)})


def _upsert(entry: dict) -> None:
    entries = [e for e in _read_raw() if e.get("account_id") != entry["account_id"]]
    entries.append(entry)
    _write_raw(entries)


def remove_account(account_id: str) -> None:
    entries = _read_raw()
    doomed = next((e for e in entries if e.get("account_id") == account_id), None)
    if doomed is None:
        raise MailboxError(f"No mailbox {account_id!r}")

    _write_raw([e for e in entries if e.get("account_id") != account_id])
    set_env_values({_env_key(account_id): None})

    # Drop the stored credential too — "remove" should mean the app can no
    # longer reach that mailbox, not just that it stopped listing it.
    token_file = (doomed.get("auth") or {}).get("token_file")
    if token_file:
        Path(token_file).unlink(missing_ok=True)


def set_enabled(account_id: str, enabled: bool) -> None:
    entries = _read_raw()
    for entry in entries:
        if entry.get("account_id") == account_id:
            entry["enabled"] = enabled
            _write_raw(entries)
            return
    raise MailboxError(f"No mailbox {account_id!r}")


# ---------------------------------------------------------------------------


def add_imap_mailbox(
    *, email: str, password: str, host: str = "", port: int = 993, label: str = ""
) -> ProviderProfile:
    """Connect a mailbox with an app password. Verified before it is saved."""
    email = email.strip().lower()
    if "@" not in email:
        raise MailboxError("That does not look like an email address.")
    if not password.strip():
        raise MailboxError("Please paste the app password.")

    preset = PRESETS[preset_for(email)]
    host = (host or preset["host"]).strip()
    if not host:
        raise MailboxError(
            "We don't know the mail server for that address — enter it manually "
            "(your IT team or mail provider can tell you the IMAP server name)."
        )

    account_id = _slug(email)
    env_key = _env_key(account_id)

    # Verify with the real password before writing anything, so a typo never
    # leaves a broken mailbox in the config.
    probe = Account(
        account_id=account_id,
        provider="imap",
        email=email,
        label=label or preset["name"],
        auth={"host": host, "port": port, "username": email, "password": password},
    )
    profile = _verify(probe)

    set_env_values({env_key: password})
    _upsert(
        {
            "account_id": account_id,
            "provider": "imap",
            "email": email,
            "user_id": "default",
            "label": label or preset["name"],
            "enabled": True,
            "auth": {
                "host": host,
                "port": port,
                "username": email,
                "password": f"env:{env_key}",
            },
        }
    )
    log.info("Added IMAP mailbox %s", email)
    return profile


def pending_token_path() -> Path:
    """Where a not-yet-identified Google sign-in parks its token.

    The account id comes from the address on the token, which is only known
    *after* consent — so consent writes here first and the file is renamed once
    we know who signed in. That is also what lets a second Google account be
    added without clobbering the first one's refresh token.
    """
    path = SETTINGS.google_token_file.parent / "token-pending.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def add_gmail_oauth_mailbox(pending: Path) -> ProviderProfile:
    """Finish a Google sign-in: identify the token, file it, save the mailbox."""
    profile = _verify(
        Account(
            account_id="pending-google",
            provider="gmail",
            email="",
            auth={"type": "oauth_desktop", "token_file": str(pending)},
        )
    )

    email = profile.email.lower()
    account_id = _slug(email)
    token_file = pending.with_name(f"token-{account_id}.json")
    pending.replace(token_file)
    token_file.chmod(0o600)

    _upsert(
        {
            "account_id": account_id,
            "provider": "gmail",
            "email": email,
            "user_id": "default",
            "label": "Google sign-in",
            "enabled": True,
            "auth": {"type": "oauth_desktop", "token_file": str(token_file)},
        }
    )
    log.info("Added Gmail mailbox %s", email)
    return profile


def save_google_client_secret(raw: str) -> None:
    """Store the OAuth client JSON downloaded from Google Cloud Console."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MailboxError(f"That file is not valid JSON ({exc.msg}).") from exc

    if "web" in payload and "installed" not in payload:
        raise MailboxError(
            "This is a Web application client. Go back to Google Cloud Console "
            "and create the credential again, choosing Application type = "
            "Desktop app."
        )
    if "installed" not in payload:
        raise MailboxError(
            "This does not look like an OAuth client file. Download the JSON "
            "from Google Cloud Console → Credentials → your Desktop app client."
        )

    target = SETTINGS.google_credentials_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(raw, encoding="utf-8")
    target.chmod(0o600)
    log.info("Saved Google OAuth client to %s", target)


def _verify(account: Account) -> ProviderProfile:
    try:
        with build_provider(account) as provider:
            return provider.verify()
    except ProviderError as exc:
        raise MailboxError(str(exc)) from exc
    except Exception as exc:
        raise MailboxError(f"{type(exc).__name__}: {exc}") from exc


def check_mailbox(account_id: str) -> ProviderProfile:
    for account in configured_accounts():
        if account.account_id == account_id:
            return _verify(account)
    raise MailboxError(f"No mailbox {account_id!r}")
