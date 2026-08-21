"""Google OAuth (installed/desktop app flow) with on-disk token caching.

First run opens a browser for consent. After that the refresh token in
``credentials/token.json`` keeps things headless, so the daily cron job works
without interaction.
"""

from __future__ import annotations

import logging
import socket
import threading
import urllib.request
from pathlib import Path
from typing import Callable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from .config import GOOGLE_SCOPES, SETTINGS

log = logging.getLogger(__name__)


class MissingClientSecret(RuntimeError):
    pass


class ConsentTimeout(RuntimeError):
    pass


class ConsentCancelled(RuntimeError):
    pass


def _free_local_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _knock(port: int) -> None:
    """Answer the consent flow's own redirect so it stops waiting.

    The flow blocks on a one-shot local web server, and a thread parked in
    ``accept()`` cannot be interrupted from outside — so the only way to free it
    is to be the request it is waiting for. What it makes of that request does
    not matter: any reply ends the wait, and the caller knows a cancel was asked
    for without having to recognise whichever error oauthlib raises.
    """
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/?cancelled=1", timeout=5).read()
    except Exception:  # already gone, or never started — either way it is stopped
        log.debug("Consent server on port %s did not answer", port, exc_info=True)


# Google shows an "app is not verified" warning for these scopes, and getting
# past it is not obvious — so say so here rather than only in the setup screen.
_TIMEOUT_HELP = (
    "The Google sign-in was not finished in time. Start it again. On the "
    "“Google hasn’t verified this app” screen, click Advanced at the bottom "
    "left, then “Go to … (unsafe)”, and approve. If Google instead refuses with "
    "“has not completed the Google verification process”, add the address you "
    "are signing in with to Test users under Audience in the Cloud Console."
)


def get_credentials(
    *,
    credentials_file: Path | None = None,
    token_file: Path | None = None,
    scopes: list[str] | None = None,
    consent_timeout: int | None = None,
    on_cancel: Callable[[Callable[[], None]], None] | None = None,
) -> Credentials:
    """Return usable credentials, running browser consent if necessary.

    ``consent_timeout`` bounds how long the local redirect server waits. The
    web UI sets it so an abandoned sign-in eventually releases the worker
    thread instead of blocking every later action.
    """
    credentials_file = credentials_file or SETTINGS.google_credentials_file
    token_file = token_file or SETTINGS.google_token_file
    scopes = scopes or GOOGLE_SCOPES

    creds: Credentials | None = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        log.info("Refreshing expired Google credentials")
        creds.refresh(Request())
        _save(creds, token_file)
        return creds

    if not credentials_file.exists():
        raise MissingClientSecret(
            f"OAuth client file not found at {credentials_file}.\n"
            "Create a Desktop-app OAuth client in Google Cloud Console "
            "(APIs & Services > Credentials), download the JSON, and save it there."
        )

    log.info("Starting OAuth consent flow (a browser window will open)")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes)
    # The port is chosen here rather than left to the library, so that whoever
    # started this sign-in knows where to knock if the user gives up on it.
    port = _free_local_port()
    cancelled = threading.Event()

    def stop() -> None:
        cancelled.set()
        _knock(port)

    if on_cancel:
        on_cancel(stop)
    kwargs = {"port": port, "prompt": "consent"}
    if consent_timeout:
        kwargs["timeout_seconds"] = consent_timeout
    try:
        creds = flow.run_local_server(**kwargs)
    except TypeError:  # older google-auth-oauthlib has no timeout_seconds
        creds = flow.run_local_server(port=port, prompt="consent")
    except Exception as exc:
        # When the wait elapses the local server raises rather than returning,
        # and the exception type lives in wsgiref — not worth importing to catch
        # by class when the message is unambiguous.
        # Whatever oauthlib made of our knock — a state mismatch, a missing
        # code — the flag is what says this was asked for, not a failure.
        if cancelled.is_set():
            raise ConsentCancelled("The Google sign-in was stopped.") from exc
        text = str(exc).lower()
        if "timed out" in text:
            raise ConsentTimeout(_TIMEOUT_HELP) from exc
        if "access_denied" in text or "access denied" in text:
            raise ConsentCancelled("You declined access at the Google screen.") from exc
        raise
    if creds is None:
        raise ConsentTimeout(_TIMEOUT_HELP)
    _save(creds, token_file)
    return creds


def _save(creds: Credentials, token_file: Path) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    token_file.chmod(0o600)
    log.info("Saved Google token to %s", token_file)


def calendar_service(creds: Credentials | None = None) -> Resource:
    return build("calendar", "v3", credentials=creds or get_credentials(), cache_discovery=False)
