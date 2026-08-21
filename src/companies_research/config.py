"""Runtime configuration, loaded from .env / environment.

The web UI writes settings while the process is running, so ``SETTINGS`` is a
live view rather than a snapshot: modules keep doing ``from .config import
SETTINGS`` and see the current values after :func:`reload_settings`.
"""

from __future__ import annotations

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
    prompts_dir: Path = ROOT / "prompts"
    watch_enabled: bool = True
    watch_interval_minutes: int = 5
    scan_days: int = 1
    user_emails: list[str] = field(default_factory=list)
    ignored_domains: list[str] = field(default_factory=list)

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
        prompts_dir=_path("PROMPTS_DIR", "prompts"),
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
