"""Run the fixtures through the real pipeline, offline.

The point is to exercise *our* code, not to re-measure the model on every run.
So the model's answer is recorded once against the live API and replayed from
then on, while everything around it — prompt assembly, the untrusted-content
fence, schema validation, parsing, the never-drop-a-message fallback — runs for
real. A regression in any of those shows up here without spending a cent or
needing a network.

What this therefore does *not* measure is model drift. Re-record to do that:

    ./start.sh eval --record     # live, costs money, updates the recordings
    ./start.sh eval              # offline, free, scores what was recorded
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Iterator

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
RECORDINGS = pathlib.Path(__file__).parent / "recordings"


@dataclass
class Fixture:
    id: str
    klass: str
    note: str
    email: dict
    expected: dict
    expected_research: dict = field(default_factory=dict)

    @property
    def message(self):
        from companies_research.models import EmailAddress, EmailMessage

        raw = dict(self.email)
        raw["sender"] = EmailAddress(**raw["sender"])
        raw["to"] = [EmailAddress(**a) for a in raw.get("to", [])]
        return EmailMessage(**raw)


def load_fixtures(only: str | None = None) -> list[Fixture]:
    out: list[Fixture] = []
    for path in sorted(FIXTURES.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if only and doc.get("class") != only:
            continue
        out.append(Fixture(
            id=doc["id"], klass=doc["class"], note=doc.get("note", ""),
            email=doc["email"], expected=doc.get("expected", {}),
            expected_research=doc.get("expected_research", {}),
        ))
    return out


# --- replay ----------------------------------------------------------------


class ReplayBackend:
    """A triage backend that answers from disk.

    Deliberately implements the same protocol as the real backends, so the
    agent above it cannot tell the difference — including when a recording is
    missing, which surfaces as an ordinary backend error and exercises the
    fallback path rather than crashing the harness.
    """

    name = "replay"
    model = "replay"

    def __init__(self, recordings: dict[str, Any]) -> None:
        self.recordings = recordings
        self.misses: list[str] = []

    def describe(self) -> str:
        return f"recorded output ({len(self.recordings)} message(s))"

    def complete(self, *, system: str, user: str, schema: dict) -> Any:
        from companies_research.agents.backends import Completion

        # The batch is identified by the message_ids the prompt asks about.
        ids = _ids_in(user)
        results = []
        for mid in ids:
            recorded = self.recordings.get(mid)
            if recorded is None:
                self.misses.append(mid)
                continue
            results.append(recorded)
        if not results:
            return Completion(error="no recording for this batch")
        return Completion(text=json.dumps({"results": results}))


def _ids_in(prompt: str) -> list[str]:
    import re

    return re.findall(r"<message_id>([^<]+)</message_id>", prompt)


def load_recordings() -> dict[str, Any]:
    path = RECORDINGS / "triage.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_recordings(data: dict[str, Any]) -> pathlib.Path:
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    path = RECORDINGS / "triage.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def load_research_recordings() -> dict[str, Any]:
    path = RECORDINGS / "research.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# --- running ---------------------------------------------------------------


def triage_fixtures(fixtures: list[Fixture], *, live: bool = False,
                    batch_size: int = 10) -> tuple[dict[str, Any], ReplayBackend | None]:
    """Classify every fixture, from recordings or from the live API."""
    from companies_research.agents.triage import TriageAgent

    messages = [f.message for f in fixtures]

    if live:
        from companies_research.agents.backends import build_backend

        agent = TriageAgent(backend=build_backend())
        agent.batch_size = batch_size
        results = agent.triage(messages)
        return {r.message_id: r for r in results}, None

    backend = ReplayBackend(load_recordings())
    agent = TriageAgent(backend=backend)
    agent.batch_size = batch_size
    results = agent.triage(messages)
    return {r.message_id: r for r in results}, backend


def iter_research(fixtures: list[Fixture]) -> Iterator[tuple[Fixture, Any]]:
    """Recorded research profiles, for the fixtures that assert on them."""
    from companies_research.models import CompanyProfile

    recorded = load_research_recordings()
    for fixture in fixtures:
        if not fixture.expected_research:
            continue
        raw = recorded.get(fixture.id)
        if raw is None:
            yield fixture, None
            continue
        yield fixture, CompanyProfile.model_validate(raw)
