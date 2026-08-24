"""Runtime configuration, loaded from .env / environment.

The web UI writes settings while the process is running, so ``SETTINGS`` is a
live view rather than a snapshot: modules keep doing ``from .config import
SETTINGS`` and see the current values after :func:`reload_settings`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# repo root = .../Companies-Research-Agent
ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"

load_dotenv(ENV_FILE)

# Read-only for now. The approval step (forwarding a report by email) will need
# gmail.send / gmail.compose added here — after changing scopes, delete the token
# file and re-run `auth` to re-consent.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

# --- tool scopes -----------------------------------------------------------
# What the agent is permitted to do, independent of what any prompt asks for.
# An injected instruction may well persuade the model to call a tool; it cannot
# grant itself the scope that tool needs, because this set comes from .env and
# is never in the model's context.
#
# `brief:deliver` is deliberately absent from the default: nothing leaves this
# machine until a human turns that on.
ALL_TOOL_SCOPES = frozenset(
    {"research:read", "mail:read", "calendar:read", "memory:write", "brief:deliver"}
)
DEFAULT_TOOL_SCOPES = frozenset(
    {"research:read", "mail:read", "calendar:read", "memory:write"}
)


# --- list prices, USD per million tokens -----------------------------------
# Published rates, matched by longest prefix so a dated id still prices. These
# are list prices: any negotiated rate is unknowable from here, so every figure
# the agent reports is an upper bound rather than an invoice.
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5":   (10.00, 50.00),
    "claude-mythos-5":  (10.00, 50.00),
    "claude-opus-5":    (5.00, 25.00),
    "claude-opus-4-8":  (5.00, 25.00),
    "claude-opus-4-7":  (5.00, 25.00),
    "claude-opus-4-6":  (5.00, 25.00),
    "claude-sonnet-5":  (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
# Server-side web search, per search. Local models cost nothing and price at 0.
SEARCH_COST_USD = 10.0 / 1000


def _path(env_key: str, default: str) -> Path:
    raw = os.getenv(env_key, default)
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _csv(env_key: str) -> list[str]:
    raw = os.getenv(env_key, "") or ""
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _bool(env_key: str, default: bool) -> bool:
    raw = (os.getenv(env_key) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _int(env_key: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(env_key, "") or default))
    except ValueError:
        return default


def _scopes(env_key: str) -> frozenset[str]:
    """Enabled tool scopes, defaulting to read-only plus local memory.

    An unrecognised name is dropped with a warning rather than silently
    granting nothing: a typo in ``TOOL_SCOPES`` should not quietly disable half
    the agent, and it must never accidentally enable something either.
    """
    raw = (os.getenv(env_key) or "").strip()
    if not raw:
        return DEFAULT_TOOL_SCOPES
    asked = {item.strip().lower() for item in raw.split(",") if item.strip()}
    unknown = asked - ALL_TOOL_SCOPES
    if unknown:
        logging.getLogger(__name__).warning(
            "Ignoring unknown scope(s) in %s: %s", env_key, ", ".join(sorted(unknown))
        )
    return frozenset(asked & ALL_TOOL_SCOPES)


def _float(env_key: str, default: float, *, minimum: float = 1.0) -> float:
    try:
        return max(minimum, float(os.getenv(env_key, "") or default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    triage_model: str
    triage_effort: str
    google_credentials_file: Path
    google_token_file: Path
    db_path: Path
    triage_backend: str = "anthropic"
    triage_batch_size: int = 10
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    ollama_num_ctx: int = 16384
    ollama_timeout: float = 600.0
    research_enabled: bool = True
    research_provider: str = "claude_web"
    research_model: str = "claude-opus-5"
    research_effort: str = "medium"
    research_max_searches: int = 8
    research_max_companies: int = 10
    research_ttl_days: int = 14
    calendar_enabled: bool = True
    calendar_provider: str = "google"
    calendar_lookahead_days: int = 30
    delivery_provider: str = "file"
    delivery_account: str = ""
    delivery_dir: Path = ROOT / "out" / "briefs"
    metrics_enabled: bool = True
    metrics_port: int = 9464
    metrics_host: str = "127.0.0.1"
    tracing_enabled: bool = False
    otlp_endpoint: str = "http://localhost:4318/v1/traces"
    langfuse_enabled: bool = False
    langfuse_host: str = "http://localhost:3001"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    # Off by default, and that default is the whole point. Langfuse exists to
    # show you the prompt and the completion — which here means email bodies
    # and the names of real people. Metadata (model, tokens, cost, latency,
    # verdict counts) answers most operational questions and carries none of
    # that, so it is what you get unless you deliberately ask for more.
    langfuse_capture_content: bool = False
    prompts_dir: Path = ROOT / "prompts"
    org_profile_file: Path = ROOT / "profile.json"
    # Empty by default: the server answers only to localhost names. Set when
    # exposing the UI through a tunnel or reverse proxy — see DEPLOYMENT.md.
    public_hosts: list[str] = field(default_factory=list)
    # First signup claims the instance; further signups are refused unless this
    # is turned on. Enforced in webapp.auth.create_user, not in the endpoint.
    signup_open: bool = False
    tool_scopes: frozenset[str] = frozenset()
    tool_audit_enabled: bool = True
    allowed_recipients: list[str] = field(default_factory=list)
    watch_enabled: bool = True
    watch_interval_minutes: int = 5
    scan_days: int = 1
    user_emails: list[str] = field(default_factory=list)
    ignored_domains: list[str] = field(default_factory=list)

    # -- derived views; every field is declared above this line ----------

    @property
    def credentials_dir(self) -> Path:
        return self.google_credentials_file.parent

    @property
    def recipient_allowlist(self) -> set[str]:
        """Addresses anything may be sent to.

        Defaults to the mailbox owner. An empty list is not "allow everyone" —
        :func:`companies_research.tools.recipient_check` treats an empty list as
        allowing only ``user_emails``, and if that is empty too, nothing at all.
        """
        return {a.lower() for a in (self.allowed_recipients or self.user_emails) if a}

    @property
    def user_domains(self) -> set[str]:
        return {e.split("@")[-1] for e in self.user_emails if "@" in e}

    @property
    def is_local_triage(self) -> bool:
        """True when no message body leaves this machine for triage."""
        return self.triage_backend.strip().lower() == "ollama"


def load_settings() -> Settings:
    backend = (os.getenv("TRIAGE_BACKEND", "anthropic") or "anthropic").strip().lower()
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        triage_model=os.getenv("TRIAGE_MODEL", "claude-opus-5"),
        triage_effort=os.getenv("TRIAGE_EFFORT", "low"),
        triage_backend=backend,
        # Ten emails at once is comfortable for a frontier model and too much for
        # a small local one, so the default follows the backend.
        triage_batch_size=_int("TRIAGE_BATCH_SIZE", 4 if backend == "ollama" else 10),
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
        # A batch of emails plus the schema overruns Ollama's small default
        # context, and the overflow is silently dropped rather than reported.
        ollama_num_ctx=_int("OLLAMA_NUM_CTX", 16384),
        ollama_timeout=_float("OLLAMA_TIMEOUT", 600.0),
        research_enabled=_bool("RESEARCH_ENABLED", True),
        research_provider=os.getenv("RESEARCH_PROVIDER", "claude_web"),
        research_model=os.getenv("RESEARCH_MODEL", "claude-opus-5"),
        research_effort=os.getenv("RESEARCH_EFFORT", "medium"),
        # Each search and fetch is billed, so the ceiling is per company and low
        # by default; raise it for thin-evidence markets.
        research_max_searches=_int("RESEARCH_MAX_SEARCHES", 8),
        # A scan that surfaces thirty leads should not quietly research thirty
        # companies. The rest wait for the next run.
        research_max_companies=_int("RESEARCH_MAX_COMPANIES", 10),
        research_ttl_days=_int("RESEARCH_TTL_DAYS", 14),
        calendar_enabled=_bool("CALENDAR_ENABLED", True),
        calendar_provider=os.getenv("CALENDAR_PROVIDER", "google"),
        # A month is far enough ahead to be worth preparing for and short enough
        # that a standing weekly invite does not drown the real meeting.
        calendar_lookahead_days=_int("CALENDAR_LOOKAHEAD_DAYS", 30),
        # `file` by default: reading mail is what this agent is for, sending it
        # is a different kind of act, and the safe option should be the one you
        # get without asking for it.
        delivery_provider=os.getenv("DELIVERY_PROVIDER", "file"),
        delivery_account=os.getenv("DELIVERY_ACCOUNT", ""),
        delivery_dir=_path("DELIVERY_DIR", "out/briefs"),
        metrics_enabled=_bool("METRICS_ENABLED", True),
        # Its own port, not the web interface's: the UI is token-gated and
        # Prometheus cannot present a token.
        metrics_port=_int("METRICS_PORT", 9464),
        # 127.0.0.1 like everything else here. Prometheus in a container cannot
        # reach that, so scraping from Docker needs 0.0.0.0 set deliberately.
        metrics_host=os.getenv("METRICS_HOST", "127.0.0.1"),
        tracing_enabled=_bool("TRACING_ENABLED", False),
        otlp_endpoint=os.getenv("OTLP_ENDPOINT", "http://localhost:4318/v1/traces"),
        langfuse_enabled=_bool("LANGFUSE_ENABLED", False),
        # 3001, not 3000: Langfuse ships on 3000 and so does Grafana.
        langfuse_host=os.getenv("LANGFUSE_HOST", "http://localhost:3001"),
        langfuse_public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
        langfuse_secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
        langfuse_capture_content=_bool("LANGFUSE_CAPTURE_CONTENT", False),
        prompts_dir=_path("PROMPTS_DIR", "prompts"),
        org_profile_file=_path("ORG_PROFILE_FILE", "profile.json"),
        public_hosts=_csv("PUBLIC_HOSTS"),
        signup_open=_bool("SIGNUP_OPEN", False),
        tool_scopes=_scopes("TOOL_SCOPES"),
        tool_audit_enabled=_bool("TOOL_AUDIT_ENABLED", True),
        allowed_recipients=_csv("ALLOWED_RECIPIENTS"),
        google_credentials_file=_path("GOOGLE_CREDENTIALS_FILE", "credentials/client_secret.json"),
        google_token_file=_path("GOOGLE_TOKEN_FILE", "credentials/token.json"),
        db_path=_path("DB_PATH", "data/agent.db"),
        watch_enabled=_bool("WATCH_ENABLED", True),
        watch_interval_minutes=_int("WATCH_INTERVAL_MINUTES", 5),
        scan_days=_int("SCAN_DAYS", 1),
        user_emails=_csv("USER_EMAILS"),
        ignored_domains=_csv("IGNORED_DOMAINS"),
    )


class _LiveSettings:
    """Stable object wrapping a replaceable :class:`Settings` snapshot.

    Every module imports ``SETTINGS`` by value at import time, so the UI could
    not otherwise change configuration without a restart.
    """

    def __init__(self) -> None:
        self._current = load_settings()

    def __getattr__(self, name: str):  # only reached for names not on self
        return getattr(self._current, name)

    def __repr__(self) -> str:
        return repr(self._current)

    def _refresh(self) -> None:
        self._current = load_settings()


SETTINGS: Settings = _LiveSettings()  # type: ignore[assignment]


def reload_settings() -> None:
    """Re-read .env and update :data:`SETTINGS` in place."""
    load_dotenv(ENV_FILE, override=True)
    SETTINGS._refresh()  # type: ignore[attr-defined]


def set_env_values(values: dict[str, str | None]) -> None:
    """Update .env and the live environment.

    Existing lines are edited in place so comments and ordering survive; a
    ``None`` value removes the key. The file is chmod 0600 because it holds
    API keys and app passwords.
    """
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    remaining = dict(values)

    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key is None or key not in remaining:
            out.append(line)
            continue
        value = remaining.pop(key)
        if value is not None:
            out.append(f"{key}={value}")

    for key, value in remaining.items():
        if value is not None:
            out.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(out).strip() + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)

    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    reload_settings()
