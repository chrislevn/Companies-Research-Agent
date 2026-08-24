"""The local web app.

Binds to 127.0.0.1 only and is protected by a per-run token embedded in the
page. Both matter: this server can read your mail and holds your API key, and
any website you have open can otherwise POST to ``localhost``. A cross-origin
page cannot read our HTML, so it cannot learn the token, so it cannot drive the
API.

Exposing it beyond 127.0.0.1 — through a tunnel or on a server — changes the
threat model: the token is *in the page*, so the token alone no longer proves
much. Two more gates cover that case (see DEPLOYMENT.md):

- ``PUBLIC_HOSTS`` — hostnames the server agrees to answer as. Anything else
  is refused before the page (and its token) is served, which is also what
  stops a DNS-rebinding page from reading the token off ``localhost``. The
  same list drives the cross-origin check, so a tunnel domain works at all.
- The session gate below — accounts and logins from ``auth.py`` — is what
  says *who* is asking once more than one person can reach the page.
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
import ipaddress
from urllib.parse import urlparse, urlsplit

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..accounts import AccountsError
from ..config import ALL_TOOL_SCOPES, SETTINGS, set_env_values
from ..pipeline import seed_known_senders, start_watching
from ..store import Store
from . import auth, mailboxes
from .jobs import RUNNER, JobBusy, friendly_error
from .watcher import WATCHER, check_now

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(24)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from ..obs import start as start_observability

    start_observability()
    WATCHER.start()
    try:
        yield
    finally:
        WATCHER.stop()


app = FastAPI(
    title="Companies Research Agent", docs_url=None, redoc_url=None, lifespan=lifespan
)


# --- guards ----------------------------------------------------------------


# Endpoints reachable without a session: the ones you use to *get* a session,
# plus the health probe. Everything else under /api requires a logged-in user.
PUBLIC_API = {
    "/api/auth/status", "/api/auth/signup", "/api/auth/login", "/api/auth/logout",
    # Google sign-in happens before a session exists, so these skip the session
    # gate — but only the session gate. The Host(421), Origin(403) and
    # x-cr-token(401) checks above still run, and each endpoint additionally
    # refuses any non-loopback request. The poll uses a static path with a
    # query param so it can be whitelisted here without opening the generic,
    # session-gated /api/jobs/{id}.
    "/api/auth/google/start", "/api/auth/google/poll", "/api/auth/google/finish",
}


def _request_is_local(request: Request) -> bool:
    """Is this request physically on the same machine as the server?

    Gates the Google sign-in flow, which runs ``InstalledAppFlow.run_local_server``
    — a browser + loopback redirect on the *server*. Only a person at the machine
    can complete it; a request over a tunnel must never launch it, or on a public
    box a stranger drives the operator's own browser and Google session.

    Deliberately does NOT trust the ``Host`` header — a client sets that freely,
    so ``Host: 127.0.0.1`` from a remote attacker would otherwise pass. Two
    signals a client cannot forge instead:

    * ``public_hosts`` — the operator's own declaration that this is a public
      deployment (set for the tunnel). If any is configured, this is not a local
      run, full stop. This also closes the case where a same-host reverse proxy
      makes the peer address below look like loopback.
    * the socket peer address — TCP makes it unspoofable for a direct connection,
      so a genuine remote attacker is rejected even on a bare ``0.0.0.0`` bind
      with no ``public_hosts`` set.
    """
    if SETTINGS.public_hosts:
        return False
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _host_allowed(hostname: str | None) -> bool:
    """Is this a name the server has agreed to answer as?

    Localhost names always are. Public names must be listed in
    ``PUBLIC_HOSTS``, either exactly (``demo.example.com``) or as a wildcard
    suffix (``*.trycloudflare.com`` — a quick tunnel gets a random subdomain,
    so the exact name cannot be known in advance).
    """
    if not hostname:
        return False
    name = hostname.lower()
    if name in LOCAL_HOSTS:
        return True
    for allowed in SETTINGS.public_hosts:
        if allowed.startswith("*.") and name.endswith(allowed[1:]):
            return True
        if name == allowed:
            return True
    return False


@app.middleware("http")
async def guard(request: Request, call_next):
    # Refuse to answer as a name we were never told about, before anything is
    # served: the page carries the run token, so serving it to a hostname we
    # do not recognise (a DNS-rebinding page, a stray proxy) hands the token
    # over. On a plain local run PUBLIC_HOSTS is empty and this accepts
    # exactly what the old loopback-only behaviour accepted.
    try:
        host_name = urlsplit(f"//{request.headers.get('host', '')}").hostname
    except ValueError:  # a Host header urlsplit cannot parse is not one we serve
        host_name = None
    if not _host_allowed(host_name):
        return JSONResponse({"error": "unrecognised host"}, status_code=421)

    path = request.url.path
    if path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and not _host_allowed(urlparse(origin).hostname):
            return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        if request.headers.get("x-cr-token") != TOKEN:
            return JSONResponse({"error": "stale page — please reload"}, status_code=401)
        # The session gate. The per-run token proves the request came from our
        # own page; the session proves *who* is asking. Both are required, and
        # they answer different questions — a shared demo box has one token and
        # many people.
        if path not in PUBLIC_API:
            from . import auth as _auth

            user = _auth.user_for_session(request.cookies.get("cra_session", ""))
            # Local dev bypass. Both conditions must hold: the operator opted in
            # with AUTH_DISABLED, AND the request is genuinely on this machine.
            # _request_is_local returns False the moment public_hosts is set, so
            # a tunneled box can never take this branch — the login wall stays up
            # for anyone off-machine even with the flag on. Falls back to the
            # owner account so is_owner() and per-user state still resolve.
            if user is None and SETTINGS.auth_disabled and _request_is_local(request):
                user = _auth.ensure_local_owner()
            if user is None:
                return JSONResponse({"error": "not signed in"}, status_code=401)
            request.state.user = user
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


def _ollama_reachable() -> bool:
    """Is a local model actually there? The UI says so before you pick it."""
    import httpx

    try:
        return httpx.get(f"{SETTINGS.ollama_host.rstrip('/')}/api/tags", timeout=1.0).is_success
    except Exception:
        return False


def _vllm_reachable() -> bool:
    """Same question for a vLLM server, via its OpenAI-compatible /models."""
    import httpx

    headers = (
        {"Authorization": f"Bearer {SETTINGS.vllm_api_key}"}
        if SETTINGS.vllm_api_key else {}
    )
    try:
        return httpx.get(
            f"{SETTINGS.vllm_base_url.rstrip('/')}/models", headers=headers, timeout=1.0
        ).is_success
    except Exception:
        return False


def _mask(value: str | None) -> str:
    if not value:
        return ""
    return f"{value[:7]}…{value[-4:]}" if len(value) > 14 else "…"




# --- authentication --------------------------------------------------------

_SESSION_COOKIE = "cra_session"


def _set_session_cookie(response: Response, token: str) -> None:
    # httponly so page scripts cannot read it (a stolen token is a stolen
    # login); samesite=strict so another site cannot ride the cookie; not
    # `secure`, because this is served over plain http on loopback and a secure
    # cookie would simply never be sent.
    response.set_cookie(
        _SESSION_COOKIE, token, max_age=30 * 24 * 3600,
        httponly=True, samesite="strict", path="/",
    )


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    """Who, if anyone, is signed in — and whether any account exists yet.

    ``has_users`` lets the page open on *Create account* for a fresh install
    and on *Log in* afterwards, so the first-run and returning cases each get
    the form they need without a toggle.
    """
    from . import auth

    user = auth.user_for_session(request.cookies.get(_SESSION_COOKIE, ""))
    # Mirror the guard's local bypass so the PAGE agrees with the API: with
    # AUTH_DISABLED on and the request genuinely local, report the owner as
    # signed in, so the front-end skips the login screen instead of showing a
    # form for a wall the API is no longer enforcing. Same two-condition gate as
    # the guard — _request_is_local is False over a tunnel, so this can never
    # report authenticated on a deployed box.
    if user is None and SETTINGS.auth_disabled and _request_is_local(request):
        user = auth.ensure_local_owner()
    return {
        "authenticated": user is not None,
        "has_users": auth.user_count() > 0,
        "user": {"email": user.email, "name": user.name} if user else None,
        # The page hides the Google button unless it could actually work here:
        # the request is on this machine AND a Google OAuth client is set up.
        "google_login_available": (
            _request_is_local(request) and SETTINGS.google_credentials_file.exists()
        ),
    }


@app.post("/api/auth/signup")
def auth_signup(payload: dict = Body(...)) -> Response:
    from . import auth

    try:
        user = auth.create_user(
            email=str(payload.get("email", "")),
            password=str(payload.get("password", "")),
            name=str(payload.get("name", "")),
        )
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from None
    token = auth.open_session(user)
    response = JSONResponse({"ok": True, "user": {"email": user.email, "name": user.name}})
    _set_session_cookie(response, token)
    return response


@app.post("/api/auth/login")
def auth_login(payload: dict = Body(...)) -> Response:
    from . import auth

    try:
        user = auth.authenticate(
            email=str(payload.get("email", "")),
            password=str(payload.get("password", "")),
        )
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from None
    token = auth.open_session(user)
    response = JSONResponse({"ok": True, "user": {"email": user.email, "name": user.name}})
    _set_session_cookie(response, token)
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> Response:
    from . import auth

    auth.close_session(request.cookies.get(_SESSION_COOKIE, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(_SESSION_COOKIE, path="/")
    return response


# --- Google sign-in (local only) -------------------------------------------
#
# Same act as connecting the mailbox: a completed consent yields the owner's
# verified address (via users.getProfile, under the gmail.readonly scope already
# granted — no scope change), which is used as the login identity. Three steps
# because the consent runs in a background job: start it, poll it, then trade a
# finished job for a session cookie. The verified email is read from the
# server-side job result, never from the request body, so nothing the client
# sends can name a different account.


@app.post("/api/auth/google/start")
def google_login_start(request: Request) -> dict:
    if not _request_is_local(request):
        raise HTTPException(404)
    if not SETTINGS.google_credentials_file.exists():
        raise HTTPException(400, "Google sign-in is not set up on this machine.")

    def work(progress) -> dict:
        from ..google_auth import get_credentials

        pending = mailboxes.pending_token_path()
        pending.unlink(missing_ok=True)
        progress("Opening Google in your browser — approve access there")
        get_credentials(token_file=pending, consent_timeout=300,
                        on_cancel=RUNNER.set_canceller)
        progress("Checking the connection")
        # Resolves the verified email AND connects the mailbox in one step —
        # signing in with Google and connecting Gmail are the same act here.
        profile = mailboxes.add_gmail_oauth_mailbox(pending)
        return {"email": profile.email}

    started = _start("auth-google", work)
    # A secret only this caller learns. poll and finish require it, so a second
    # party who guesses or sees the job id still cannot read the identity or
    # mint the session.
    secret = secrets.token_urlsafe(24)
    job = RUNNER.get(started["id"])
    if job is not None:
        job.secret = secret
    return {**started, "secret": secret}


def _auth_job_or_404(request: Request, job_id: str, secret: str):
    """Shared guard for poll/finish: local, right kind, right secret."""
    if not _request_is_local(request):
        raise HTTPException(404)
    job = RUNNER.get(job_id)
    if job is None or job.kind != "auth-google":
        raise HTTPException(404, "No such sign-in")
    if not (job.secret and secrets.compare_digest(secret, job.secret)):
        raise HTTPException(404, "No such sign-in")
    return job


@app.get("/api/auth/google/poll")
def google_login_poll(request: Request) -> dict:
    job = _auth_job_or_404(request, request.query_params.get("job_id", ""),
                           request.query_params.get("secret", ""))
    return job.as_dict()


@app.post("/api/auth/google/finish")
def google_login_finish(request: Request, payload: dict = Body(...)) -> Response:
    if not _request_is_local(request):
        raise HTTPException(404)
    from . import auth

    # Validate secret + kind + locality first, then take the job atomically so
    # it can be spent exactly once — a finished sign-in cannot be replayed into
    # a second session, and its identity stops being readable once claimed.
    guard = _auth_job_or_404(request, str(payload.get("job_id", "")),
                             str(payload.get("secret", "")))
    if guard.status != "done":
        raise HTTPException(409, "That sign-in has not finished yet.")
    job = RUNNER.take(str(payload.get("job_id", "")), kind="auth-google")
    if job is None:
        raise HTTPException(409, "That sign-in was already used.")
    email = (job.result or {}).get("email")
    if not email:
        raise HTTPException(400, "That sign-in produced no identity.")
    try:
        user = auth.login_or_create_google(email=email)
    except auth.AuthError as exc:
        raise HTTPException(403, str(exc)) from None
    token = auth.open_session(user)
    response = JSONResponse({"ok": True, "user": {"email": user.email, "name": user.name}})
    _set_session_cookie(response, token)
    return response


@app.get("/api/state")
def state() -> dict:
    store = Store()
    accounts = mailboxes.configured_accounts()
    job = RUNNER.current()
    # An auth-google job carries a verified email in its result and its id is
    # the poll handle; /api/state must not surface either, or a signed-in party
    # on a shared box could read another visitor's in-flight sign-in.
    if job is not None and job.kind == "auth-google":
        job = None
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
            "model": SETTINGS.active_triage_model,
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
            "triage_backend": SETTINGS.triage_backend,
            "ollama_model": SETTINGS.ollama_model,
            "ollama_host": SETTINGS.ollama_host,
            "vllm_model": SETTINGS.vllm_model,
            "vllm_base_url": SETTINGS.vllm_base_url,
            "research_enabled": SETTINGS.research_enabled,
            "research_effort": SETTINGS.research_effort,
            "research_max_searches": SETTINGS.research_max_searches,
            "research_max_companies": SETTINGS.research_max_companies,
            "research_ttl_days": SETTINGS.research_ttl_days,
            "calendar_enabled": SETTINGS.calendar_enabled,
            "calendar_lookahead_days": SETTINGS.calendar_lookahead_days,
            "delivery_provider": SETTINGS.delivery_provider,
            "delivery_account": SETTINGS.delivery_account,
            "allowed_recipients": sorted(SETTINGS.recipient_allowlist),
            "tool_scopes": sorted(SETTINGS.tool_scopes),
            "all_tool_scopes": sorted(ALL_TOOL_SCOPES),
            "ollama_reachable": _ollama_reachable(),
            "vllm_reachable": _vllm_reachable(),
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


@app.get("/api/briefs/{brief_id}/pdf")
def brief_pdf(brief_id: str) -> Response:
    """The brief as a PDF, for preview and download.

    Served as bytes rather than a file path, and fetched by the page with the
    same token header as every other call. An `<iframe src=...>` cannot send a
    header, so the alternative would be putting the run token in a URL — where
    it lands in history, in referrers and in any log that records paths. The
    page fetches this as a blob instead and points the frame at the blob.
    """
    from ..briefs import PdfUnavailable, to_pdf
    from ..models import Brief

    record = Store().get_brief(brief_id)
    if record is None:
        raise HTTPException(404, "No such brief")
    brief = Brief.model_validate_json(record["brief_json"])
    try:
        data = to_pdf(brief)
    except PdfUnavailable as exc:
        raise HTTPException(503, str(exc)) from None

    stem = _slugify(brief.company or brief.domain or brief.lead_id)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            # `inline` so the browser renders it in the frame instead of
            # offering a download the sandbox would block anyway.
            "Content-Disposition": f'inline; filename="{stem}.pdf"',
            "Cache-Control": "no-store",
        },
    )


def _slugify(text: str) -> str:
    import re
    import unicodedata

    folded = unicodedata.normalize("NFKD", text or "brief")
    ascii_only = folded.encode("ascii", "ignore").decode() or "brief"
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", ascii_only)
    # "Agora, Inc." ends in a full stop, which would meet the extension
    # and produce "agora-inc..pdf".
    return slug.strip("-.").lower() or "brief"


@app.get("/api/briefs/{brief_id}/docx")
def brief_docx(brief_id: str) -> Response:
    """The brief as an editable Word document."""
    from ..briefs import DocxUnavailable, to_docx
    from ..models import Brief

    record = Store().get_brief(brief_id)
    if record is None:
        raise HTTPException(404, "No such brief")
    brief = Brief.model_validate_json(record["brief_json"])
    try:
        data = to_docx(brief)
    except DocxUnavailable as exc:
        raise HTTPException(503, str(exc)) from None

    stem = _slugify(brief.company or brief.domain or brief.lead_id)
    return Response(
        content=data,
        media_type=("application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"),
        headers={
            "Content-Disposition": f'attachment; filename="{stem}.docx"',
            "Cache-Control": "no-store",
        },
    )


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


@app.post("/api/briefs/{brief_id}/unapprove")
def unapprove_brief(brief_id: str, payload: dict = Body(default={})) -> dict:
    """Withdraw a decision, returning the brief to draft.

    Refused once the brief has been delivered. At that point the withdrawal
    would be a lie: the mail has gone, and a UI showing `draft` would say it
    had not. The state machine refuses it too — this check exists to give the
    person a reason rather than a silent failure.
    """
    who = str(payload.get("approved_by", "") or "operator").strip()
    store = Store()
    record = store.get_brief(brief_id)
    if record is None:
        raise HTTPException(404, "No such brief")
    if record["status"] == "draft":
        return {"ok": True, "status": "draft", "note": "already a draft"}
    if record["status"] == "delivered":
        raise HTTPException(
            409,
            "This brief has already been delivered. A delivery cannot be "
            "withdrawn — the mail has left the machine.",
        )
    if not store.set_brief_status(brief_id, "draft", approved_by=who):
        raise HTTPException(409, f"Cannot withdraw a brief that is {record['status']}")
    return {"ok": True, "status": "draft"}


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


@app.get("/api/prompts")
def get_prompts() -> dict:
    from .. import prompts
    from ..agents.triage import SYSTEM_PROMPT as TRIAGE_DEFAULT
    from ..research.claude_web import DEFAULT_SYSTEM_PROMPT as RESEARCH_DEFAULT

    out = {}
    for name, default in (("triage", TRIAGE_DEFAULT), ("research", RESEARCH_DEFAULT)):
        loaded = prompts.load(name, default)
        out[name] = {
            "text": loaded.text, "source": loaded.source,
            "customised": loaded.customised, "default": default,
        }
    return {"prompts": out}


@app.post("/api/prompts/{name}")
def set_prompt(name: str, payload: dict = Body(...)) -> dict:
    """Write or reset one prompt. Empty text restores the built-in."""
    from .. import prompts
    from ..agents.triage import SYSTEM_PROMPT as TRIAGE_DEFAULT
    from ..research.claude_web import DEFAULT_SYSTEM_PROMPT as RESEARCH_DEFAULT

    defaults = {"triage": TRIAGE_DEFAULT, "research": RESEARCH_DEFAULT}
    if name not in defaults:
        raise HTTPException(404, f"No prompt called {name!r}.")

    text = str(payload.get("text", "")).strip()
    path = prompts.prompt_path(name)
    if not text:
        path.unlink(missing_ok=True)      # back to the built-in
        return {"ok": True, "customised": False}

    # Same scrub the loader applies, done here so the file on disk is clean too.
    cleaned = prompts.scrub_credentials(text, where=f"prompts/{name}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cleaned.rstrip() + "\n", encoding="utf-8")
    return {"ok": True, "customised": True, "redacted": cleaned != text}


@app.get("/api/profile")
def get_profile() -> dict:
    from .. import org

    profile = org.load()
    return {"profile": profile.model_dump(), "configured": profile.configured}


@app.post("/api/profile")
def set_profile(payload: dict = Body(...)) -> dict:
    """Save the operator's own company profile.

    Validated through the same Pydantic model the prompts read, so a malformed
    save fails here rather than producing a prompt nobody can explain.
    """
    from .. import org
    from ..models import OrgProfile

    try:
        profile = OrgProfile.model_validate(payload.get("profile") or {})
    except Exception as exc:
        raise HTTPException(400, f"That profile could not be saved: {exc}") from None
    org.save(profile)
    return {"ok": True, "configured": profile.configured}


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


def _require_owner(request: Request) -> None:
    """Settings, the API key and purge stay with the account that claimed the
    instance. Every session may read and run; only the operator reconfigures —
    otherwise a SIGNUP_OPEN demo visitor could grant delivery to themselves,
    repoint the triage backend at a host they control, or erase the data.
    """
    user = getattr(request.state, "user", None)
    if user is None or not auth.is_owner(user):
        raise HTTPException(
            403, "Only the account that claimed this instance can change this."
        )


@app.post("/api/anthropic")
def set_anthropic(request: Request, payload: dict = Body(...)) -> dict:
    _require_owner(request)
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
def update_settings(request: Request, payload: dict = Body(...)) -> dict:
    _require_owner(request)
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

    # --- where triage runs ---
    if "triage_backend" in payload:
        backend = str(payload["triage_backend"]).strip().lower()
        if backend not in ("anthropic", "ollama", "vllm"):
            raise HTTPException(
                400, "Triage runs on the Claude API, on Ollama, or on a vLLM server."
            )
        values["TRIAGE_BACKEND"] = backend
    if "ollama_model" in payload:
        values["OLLAMA_MODEL"] = str(payload["ollama_model"]).strip()
    if "ollama_host" in payload:
        values["OLLAMA_HOST"] = str(payload["ollama_host"]).strip()
    if "vllm_model" in payload:
        values["VLLM_MODEL"] = str(payload["vllm_model"]).strip()
    if "vllm_base_url" in payload:
        values["VLLM_BASE_URL"] = str(payload["vllm_base_url"]).strip()

    # --- research ---
    if "research_enabled" in payload:
        values["RESEARCH_ENABLED"] = "1" if payload["research_enabled"] else "0"
    if "research_effort" in payload:
        effort = str(payload["research_effort"]).strip().lower()
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise HTTPException(400, "Effort must be low, medium, high, xhigh or max.")
        values["RESEARCH_EFFORT"] = effort
    if "research_max_searches" in payload:
        values["RESEARCH_MAX_SEARCHES"] = str(max(1, int(payload["research_max_searches"])))
    if "research_max_companies" in payload:
        values["RESEARCH_MAX_COMPANIES"] = str(max(1, int(payload["research_max_companies"])))
    if "research_ttl_days" in payload:
        values["RESEARCH_TTL_DAYS"] = str(max(1, int(payload["research_ttl_days"])))

    # --- calendar ---
    if "calendar_enabled" in payload:
        values["CALENDAR_ENABLED"] = "1" if payload["calendar_enabled"] else "0"
    if "calendar_lookahead_days" in payload:
        values["CALENDAR_LOOKAHEAD_DAYS"] = str(max(1, int(payload["calendar_lookahead_days"])))

    # --- delivery and the permission gate ---
    if "delivery_provider" in payload:
        provider = str(payload["delivery_provider"]).strip().lower()
        if provider not in ("file", "gmail_send"):
            raise HTTPException(400, "Delivery is either a local file or a separate Gmail account.")
        values["DELIVERY_PROVIDER"] = provider
    if "delivery_account" in payload:
        values["DELIVERY_ACCOUNT"] = str(payload["delivery_account"]).strip()
    if "allowed_recipients" in payload:
        raw = payload["allowed_recipients"]
        items = raw if isinstance(raw, list) else str(raw).split(",")
        values["ALLOWED_RECIPIENTS"] = ",".join(
            sorted({a.strip().lower() for a in items if a.strip()})
        )
    if "tool_scopes" in payload:
        from ..config import ALL_TOOL_SCOPES

        raw = payload["tool_scopes"]
        items = raw if isinstance(raw, list) else str(raw).split(",")
        asked = {s.strip().lower() for s in items if s.strip()}
        unknown = asked - ALL_TOOL_SCOPES
        if unknown:
            raise HTTPException(400, f"Unknown permission(s): {', '.join(sorted(unknown))}")
        # Turning delivery on with nowhere safe to send it is a footgun, not a
        # setting: refuse rather than let the gate be the one to say no later.
        if "brief:deliver" in asked:
            allowed = values.get("ALLOWED_RECIPIENTS")
            if allowed is None:
                allowed = ",".join(sorted(SETTINGS.recipient_allowlist))
            if not allowed:
                raise HTTPException(
                    400,
                    "Add at least one allowed recipient before turning on sending — "
                    "otherwise every delivery would be refused anyway.",
                )
        values["TOOL_SCOPES"] = ",".join(sorted(asked))

    set_env_values(values)
    return {"ok": True, "changed": sorted(values)}


@app.post("/api/purge")
def purge(request: Request) -> dict:
    _require_owner(request)
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


def serve(port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1") -> None:
    import uvicorn

    local = host in ("127.0.0.1", "localhost", "::1")
    port = _free_port(port) if local else port
    url = f"http://127.0.0.1:{port}/"

    print("\n  Companies Research Agent", flush=True)
    print(f"  Open this in your browser:  {url}", flush=True)
    if not local:
        # A wider bind is for containers and tunnels, and it is only safe
        # because of the guards above — say what still has to be true.
        print(
            f"  Listening on {host}:{port}. Remember: only hostnames in "
            "PUBLIC_HOSTS will be answered — see DEPLOYMENT.md.",
            flush=True,
        )
    print("  Press Ctrl+C here to stop.\n", flush=True)

    if open_browser and local:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")
