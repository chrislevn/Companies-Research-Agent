"""No mistakes ship: a standing guard that no secret is committed.

This exists because a one-time manual audit rots — the next person to paste an
API key into a config, or commit a token by reflex, would not be caught. So the
audit becomes a test that runs on every push.

The hard part is telling a *real* secret from the things that legitimately look
like one: the credential-detection regexes in prompts.py, and the fake keys in
the injection tests (``sk-ant-api03-`` + ``"A" * 40``). A dumb pattern match
flags all of them — the repo has already been bitten by exactly that (a commit
titled "the key scan was flagging its own detector"). So the check keys on what
separates a real secret from a decoy: a real key is high-entropy, while a
detector pattern contains regex metacharacters and a fixture repeats one
character. This file never embeds the literals it hunts for, so it does not
flag itself.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Shapes that, with real key material after them, are a live credential.
SECRET_SHAPES = (
    re.compile(r"sk-ant-api03-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    re.compile(r"GOCSPX-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ya29\.[A-Za-z0-9_-]{30,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{35}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

SKIP_SUFFIXES = {".png", ".pdf", ".zip", ".ico", ".jpg", ".jpeg", ".gif", ".woff2"}


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def _looks_real(match: str) -> bool:
    """True only for high-entropy key material — not a regex, not a fixture."""
    if "PRIVATE KEY" in match:
        return True
    if any(ch in match for ch in "[]{}\\|"):
        return False                       # a regex pattern, not a value
    body = re.sub(r"[^A-Za-z0-9]", "", match)
    # A real secret uses many different characters; a fixture like "AAAA…" or a
    # placeholder does not. Twelve distinct characters is far below any real
    # key's entropy and far above any fixture's.
    return len(set(body)) >= 12


def test_no_real_secret_is_committed():
    offenders = []
    for rel in _tracked_files():
        path = ROOT / rel
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in SECRET_SHAPES:
            for match in pattern.finditer(text):
                if _looks_real(match.group(0)):
                    offenders.append(f"{rel}: {match.group(0)[:16]}…")
    assert not offenders, (
        "a live-looking secret is committed:\n  " + "\n  ".join(offenders)
    )


def test_the_operators_real_identifiers_are_nowhere_in_the_tree():
    """The real mailbox must never appear — built from parts so this test does
    not itself contain the string it forbids."""
    needle = "locvicvn" + "1234"          # never the whole literal in this file
    offenders = []
    for rel in _tracked_files():
        path = ROOT / rel
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if needle in text:
            offenders.append(rel)
    assert not offenders, f"the operator's real Gmail appears in: {offenders}"


def test_sensitive_files_are_gitignored():
    """The files that hold real secrets must be un-committable by construction."""
    must_ignore = [
        ".env",
        "credentials/client_secret.json",
        "credentials/token.json",
        "data/accounts.db",
        "data/agent.db",
    ]
    not_ignored = [
        f for f in must_ignore
        if subprocess.run(["git", "check-ignore", f], cwd=ROOT,
                          capture_output=True).returncode != 0
    ]
    assert not not_ignored, f"these must be gitignored but are not: {not_ignored}"


def test_no_secret_bearing_file_is_tracked():
    """A belt to the gitignore braces: nothing secret-shaped is actually tracked."""
    tracked = _tracked_files()
    bad = [
        t for t in tracked
        if t == ".env"
        or t.endswith("/.env")
        or "client_secret" in t
        or t.endswith("accounts.db")
        or t.endswith("agent.db")
        or "/token-" in t or t.startswith("token-")
        or t.endswith("/token.json")
    ]
    assert not bad, f"secret-bearing files are tracked: {bad}"
