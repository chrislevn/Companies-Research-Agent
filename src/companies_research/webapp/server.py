"""The local web app.

Binds to 127.0.0.1 only and is protected by a per-run token embedded in the
page. Both matter: this server can read your mail and holds your API key, and
any website you have open can otherwise POST to ``localhost``. A cross-origin
page cannot read our HTML, so it cannot learn the token, so it cannot drive the
API.
"""

from __future__ import annotations

import logging
import secrets
import socket
import threading
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..accounts import AccountsError
from ..config import SETTINGS, set_env_values
from ..pipeline import seed_known_senders, start_watching
from ..store import Store
from . import mailboxes
from .jobs import RUNNER, JobBusy, friendly_error
from .watcher import WATCHER, check_now

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(24)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    WATCHER.start()
    try:
        yield
    finally:
        WATCHER.stop()


app = FastAPI(
    title="Companies Research Agent", docs_url=None, redoc_url=None, lifespan=lifespan
)


# --- guards ----------------------------------------------------------------


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname not in LOCAL_HOSTS:
            return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        if request.headers.get("x-cr-token") != TOKEN:
            return JSONResponse({"error": "stale page — please reload"}, status_code=401)
    return await call_next(request)


@app.exception_handler(mailboxes.MailboxError)
async def _mailbox_error(_request: Request, exc: mailboxes.MailboxError):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(AccountsError)
async def _accounts_error(_request: Request, exc: AccountsError):
    return JSONResponse({"error": str(exc)}, status_code=400)


# --- pages -----------------------------------------------------------------


def _asset_version() -> str:
    """Changes whenever any static file does.

    The stylesheet and scripts are plain files on disk with no cache headers,
    so a browser is entitled to keep serving the copy it already has — and an
    update that lands mid-session then leaves someone running new markup
    against old CSS. Stamping the mtime into the URL makes an updated file a
    different URL, so a normal reload is always enough and nobody has to know
    about hard refreshes.
    """
    newest = max(f.stat().st_mtime_ns for f in STATIC_DIR.iterdir() if f.is_file())
    return str(newest)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__CR_TOKEN__", TOKEN)
    html = html.replace("__CR_ASSET_VERSION__", _asset_version())
    # The page carries a per-run token and the asset versions, so a cached copy
    # is a stale page pointing at stale files.
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- state -----------------------------------------------------------------


def _mask(value: str | None) -> str:
    if not value:
        return ""
    return f"{value[:7]}…{value[-4:]}" if len(value) > 14 else "…"




@app.get("/api/state")
def state() -> dict:
    store = Store()
    accounts = mailboxes.configured_accounts()
    job = RUNNER.current()
    known = store.sender_count()

    return {
        "mailboxes": [
            {
                "account_id": a.account_id,
                "provider": a.provider,
                "email": a.email,
                "label": a.label,
                "enabled": a.enabled,
            }
            for a in accounts
        ],
        "anthropic": {
            "configured": bool(SETTINGS.anthropic_api_key),
            "masked": _mask(SETTINGS.anthropic_api_key),
        },
        # What will actually classify the next scan. `settings.model` below is
        # the *Claude* choice and stays put whichever backend is live, because
        # the settings screen binds a model picker to it.
        "triage": {
            "backend": SETTINGS.triage_backend,
            "model": (
                SETTINGS.ollama_model if SETTINGS.is_local_triage else SETTINGS.triage_model
            ),
            "local": SETTINGS.is_local_triage,
            "batch_size": SETTINGS.triage_batch_size,
        },
        "google_client_ready": SETTINGS.google_credentials_file.exists(),
        "research": {
            "enabled": SETTINGS.research_enabled,
            "provider": SETTINGS.research_provider,
            "model": SETTINGS.research_model,
            "companies": store.research_count(),
        },
        "known_senders": known,
        "processed": store.processed_count(),
        "seeded": store.get_state("seeded_at") is not None,
        "last_scan_at": store.get_state("last_scan_at"),
        "watching_since": store.get_state("watch_since"),
        "settings": {
            "model": SETTINGS.triage_model,
            "effort": SETTINGS.triage_effort,
            "ignored_domains": SETTINGS.ignored_domains,
            "scan_days": SETTINGS.scan_days,
            "watch_enabled": SETTINGS.watch_enabled,
            "watch_interval_minutes": SETTINGS.watch_interval_minutes,
        },
        "watcher_running": WATCHER.running,
        "job": job.as_dict() if job else None,
    }


# --- briefs: the approval queue --------------------------------------------


@app.get("/api/briefs")
def list_briefs(status: str | None = None, limit: int = 100) -> dict:
    from ..briefs import to_html
    from ..models import Brief

    store = Store()
    rows = store.list_briefs(status=status, limit=limit)
    out = []
    for row in rows:
        if not row.get("brief"):
            continue
        brief = Brief.model_validate(row["brief"])
        out.append({
            "id": row["id"],
            "status": row["status"],
            "company": row["company"],
            "domain": row["domain"],
            "generated_at": row["generated_at"],
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
            "html": to_html(brief),
            # Surfaced separately so the review screen can flag them without
            # re-deriving what counts as doubtful.
            "unknowns": brief.unknowns,
            "sources": brief.sources,
            "unverified_count": len(brief.unverified_claims),
            "verified_count": len(brief.verified_claims),
            "meeting": brief.upcoming_meeting.model_dump(mode="json")
            if brief.upcoming_meeting else None,
        })
    return {"briefs": out, "delivery": _delivery_state()}


def _delivery_state() -> dict:
    """What approving would actually do — the UI says so before the click."""
    from ..delivery import DeliveryError, build_delivery
    from .. import tools

    try:
        provider = build_delivery()
        describe, leaves, error = provider.describe(), provider.leaves_machine, ""
    except DeliveryError as exc:
        describe, leaves, error = SETTINGS.delivery_provider, False, str(exc)
    return {
        "provider": SETTINGS.delivery_provider,
        "describes_as": describe,
        "leaves_machine": leaves,
        "error": error,
        "scope_granted": tools.granted("brief:deliver"),
        "allowed_recipients": sorted(SETTINGS.recipient_allowlist),
    }


@app.post("/api/briefs/{brief_id}/approve")
def approve_brief(brief_id: str, payload: dict = Body(default={})) -> dict:
    """Record that a human approved this, then attempt delivery.

    Approving is not the same as sending. The gate is consulted on delivery
    regardless of who clicked, so an approval can be recorded even when the
    brief cannot leave the machine — which is the normal case, since
    `brief:deliver` is off by default.
    """
    from ..briefs import deliver

    recipient = str(payload.get("recipient", "")).strip()
    note = str(payload.get("note", "")).strip()
    approver = str(payload.get("approved_by", "") or "operator").strip()
    if not recipient:
        raise HTTPException(400, "A recipient is required.")

    store = Store()
    existing = store.get_brief(brief_id)
    if existing is None:
        raise HTTPException(404, "No such brief")
    if not store.set_brief_status(brief_id, "approved", approved_by=approver):
        raise HTTPException(
            409, f"This brief is already {existing['status']} and cannot be approved again."
        )

    outcome = deliver(brief_id=brief_id, recipient=recipient, note=note, store=store)
    return {
        "id": brief_id,
        "status": store.get_brief(brief_id)["status"],
        "delivered": outcome.ok,
        "destination": outcome.destination,
        "error": outcome.error,
        "provider": outcome.provider,
    }


@app.post("/api/briefs/{brief_id}/reject")
def reject_brief(brief_id: str, payload: dict = Body(default={})) -> dict:
    reason = str(payload.get("reason", "")).strip()
    approver = str(payload.get("approved_by", "") or "operator").strip()
    store = Store()
    existing = store.get_brief(brief_id)
    if existing is None:
        raise HTTPException(404, "No such brief")
    if not store.set_brief_status(brief_id, "rejected", approved_by=approver):
        raise HTTPException(
            409, f"This brief is already {existing['status']} and cannot be rejected."
        )
    log.info("Brief %s rejected: %s", brief_id, reason or "(no reason given)")
    return {"id": brief_id, "status": "rejected"}


@app.post("/api/briefs/generate")
def generate_brief(payload: dict = Body(default={})) -> dict:
    from ..briefs import generate

    domain = str(payload.get("domain", "")).strip().lower()
    if not domain:
        raise HTTPException(400, "A domain is required.")
    store = Store()
    brief = generate(domain=domain, store=store)
    if brief is None:
        raise HTTPException(404, f"No triaged lead for {domain}")
    return {"id": store.save_brief(brief), "domain": domain}


@app.get("/api/presets")
def presets() -> dict:
    return {"presets": mailboxes.presets_payload()}


@app.get("/api/leads")
def leads(limit: int = 200, all_triaged: bool = False) -> dict:
    return {"leads": Store().recent_leads(limit=limit, only_research=not all_triaged)}


# --- mailboxes -------------------------------------------------------------


@app.post("/api/mailboxes/imap")
def add_imap(payload: dict = Body(...)) -> dict:
    profile = mailboxes.add_imap_mailbox(
        email=str(payload.get("email", "")),
        password=str(payload.get("password", "")),
        host=str(payload.get("host", "")),
        port=int(payload.get("port") or 993),
    )
    return {"email": profile.email}


@app.post("/api/mailboxes/google/client-secret")
def google_client_secret(payload: dict = Body(...)) -> dict:
    mailboxes.save_google_client_secret(str(payload.get("content", "")))
    return {"ok": True}


@app.post("/api/mailboxes/google/connect")
def google_connect() -> dict:
    if not SETTINGS.google_credentials_file.exists():
        raise HTTPException(400, "Upload the Google credentials file first.")

    def work(progress) -> dict:
        from ..google_auth import get_credentials

        pending = mailboxes.pending_token_path()
        pending.unlink(missing_ok=True)
        progress("Opening Google in your browser — approve access there")
        get_credentials(
            token_file=pending,
            consent_timeout=300,
            # Registered as soon as the local server has a port, so the Cancel
            # button works for the whole time the sign-in is waiting.
            on_cancel=RUNNER.set_canceller,
        )
        progress("Checking the connection")
        profile = mailboxes.add_gmail_oauth_mailbox(pending)
        return {"email": profile.email}

    return _start("connect", work)


@app.delete("/api/mailboxes/{account_id}")
def delete_mailbox(account_id: str) -> dict:
    mailboxes.remove_account(account_id)
    return {"ok": True}


@app.post("/api/mailboxes/{account_id}/check")
def check_mailbox(account_id: str) -> dict:
    profile = mailboxes.check_mailbox(account_id)
    return {"email": profile.email, "total_messages": profile.total_messages}


# --- settings --------------------------------------------------------------


@app.post("/api/anthropic")
def set_anthropic(payload: dict = Body(...)) -> dict:
    key = str(payload.get("api_key", "")).strip()
    if not key:
        raise HTTPException(400, "Paste your Claude API key.")

    import anthropic

    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except Exception as exc:
        raise HTTPException(400, friendly_error(exc)) from exc

    set_env_values({"ANTHROPIC_API_KEY": key})
    return {"masked": _mask(key)}


@app.post("/api/settings")
def update_settings(payload: dict = Body(...)) -> dict:
    values: dict[str, str | None] = {}
    if "ignored_domains" in payload:
        raw = payload["ignored_domains"]
        items = raw if isinstance(raw, list) else str(raw).split(",")
        values["IGNORED_DOMAINS"] = ",".join(sorted({s.strip().lower() for s in items if s.strip()}))
    if "model" in payload:
        values["TRIAGE_MODEL"] = str(payload["model"])
    if "effort" in payload:
        values["TRIAGE_EFFORT"] = str(payload["effort"])
    if "scan_days" in payload:
        values["SCAN_DAYS"] = str(max(1, int(payload["scan_days"])))
    if "watch_enabled" in payload:
        values["WATCH_ENABLED"] = "1" if payload["watch_enabled"] else "0"
    if "watch_interval_minutes" in payload:
        values["WATCH_INTERVAL_MINUTES"] = str(max(1, int(payload["watch_interval_minutes"])))
    set_env_values(values)
    return {"ok": True}


@app.post("/api/purge")
def purge() -> dict:
    removed = Store().purge_user("default")
    return {"removed": removed}


# --- jobs ------------------------------------------------------------------


def _start(kind: str, work) -> dict:
    try:
        job = RUNNER.start(kind, work)
    except JobBusy as exc:
        raise HTTPException(409, f"Still busy with: {exc.kind}") from exc
    return job.as_dict()


@app.post("/api/jobs/seed")
def start_seed(payload: dict = Body(default={})) -> dict:
    # Seeding reads mail, so without a mailbox it would "succeed" at learning
    # nobody and then mark setup complete — leaving every contact looking new
    # on the first real scan.
    if not mailboxes.configured_accounts():
        raise HTTPException(400, "Connect a mailbox first.")

    months = int(payload.get("months") or 6)

    def work(progress) -> dict:
        progress("Looking through your existing conversations")
        count = seed_known_senders(since=timedelta(days=30 * months), progress=progress)
        store = Store()
        store.set_state("seeded_at", datetime.now(timezone.utc).isoformat())
        # Everything read above is history. From here on, only mail that
        # actually arrives counts as new.
        start_watching(store)
        return {"seeded": count, "known_senders": store.sender_count()}

    return _start("seed", work)


@app.post("/api/jobs/scan")
def start_scan(payload: dict = Body(default={})) -> dict:
    if not mailboxes.configured_accounts():
        raise HTTPException(400, "Connect a mailbox first.")

    def work(progress) -> dict:
        # The button and the watcher do the same thing: look at whatever has
        # arrived since the last look. Pressing it more often just means smaller
        # windows, never re-reading mail that was already dealt with.
        result = check_now(progress)
        progress(
            f"Found {result['leads']} new business contact(s) "
            f"in {result['fetched']} email(s)"
        )
        return result

    return _start("scan", work)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = RUNNER.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job.as_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = RUNNER.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    if not RUNNER.cancel(job_id):
        raise HTTPException(409, "That job has already finished, or cannot be stopped.")
    return {"ok": True}


# --- entry point -----------------------------------------------------------


def _free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return 0  # let the OS choose


def serve(port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn

    port = _free_port(port)
    url = f"http://127.0.0.1:{port}/"

    print("\n  Companies Research Agent", flush=True)
    print(f"  Open this in your browser:  {url}", flush=True)
    print("  Press Ctrl+C here to stop.\n", flush=True)

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
