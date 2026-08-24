"""`./start.sh compare-embeddings` — which embedding model should do retrieval.

The LLM comparison next door asks which model should *classify*. This asks
which should *retrieve*, because the two are separate decisions and the second
one is the gate on adding memory to this agent at all: if a local embedder is
competitive, retrieval over past mail can be built without any message text
leaving the machine, and if it is not, that is worth knowing before designing
around it.

The task is the one this system would actually run. Every fixture email is a
document; every query is a company or contact someone would search for; a hit
is the retriever returning that company's own email. No synthetic sentence
pairs — the corpus is the mail, and ground truth comes from the fixtures'
existing `expected` block, which was hand-labelled for the triage eval and is
reused here rather than invented.

Reported as recall@1, recall@3 and MRR. Recall@1 is the honest headline: a
brief that quotes the wrong company's history is worse than a brief with no
history, so rank 1 is the only rank that helps unattended.

There is no vector database here, and that is a decision rather than an
omission. Thirty documents is a numpy dot product — exact, instant, and with
no index to build or keep warm. A vector database answers a scale question
this corpus does not ask, and adding one would measure Qdrant rather than the
embeddings.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .harness import Fixture, load_fixtures

RESULTS = pathlib.Path("compare_embeddings_results.json")

# Published rates, USD per million tokens. Local models are free, which is
# most of the point of including one.
EMBED_PRICING = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "nomic-embed-text": 0.0,
}


@dataclass
class Candidate:
    label: str
    provider: str          # "openai" | "ollama"
    model: str
    dimensions: int = 0    # filled in from the first vector returned


@dataclass
class Outcome:
    label: str
    model: str
    provider: str
    dimensions: int = 0
    recall_at_1: float = 0.0
    recall_at_3: float = 0.0
    mrr: float = 0.0
    embed_seconds: float = 0.0
    query_latency_p50: float = 0.0
    tokens: int = 0
    cost_per_1000_docs: float = 0.0
    misses: list[str] = field(default_factory=list)
    error: str = ""


# --- the corpus and the questions -------------------------------------------


def _document(fixture: Fixture) -> str:
    """What would be stored: the message as a person would recognise it."""
    message = fixture.message
    sender = message.sender.email or ""
    return "\n".join([
        f"From: {message.sender.name or ''} <{sender}>",
        f"Subject: {message.subject or ''}",
        (message.body_text or message.snippet or "")[:2000],
    ])


def _queries(fixtures: list[Fixture]) -> list[tuple[str, str]]:
    """(query, fixture_id) pairs whose answer the fixtures already assert.

    Only fixtures naming a company are usable: the question "which email was
    from Acme" needs a fixture that says Acme is the answer. Fixtures with a
    list of acceptable names contribute their first, which is the canonical one.
    """
    out: list[tuple[str, str]] = []
    for fixture in fixtures:
        want = fixture.expected.get("company_name")
        if isinstance(want, list):
            want = want[0] if want else None
        if not want:
            continue
        # Two shapes of the same question. A retriever that only handles bare
        # keywords is not much use behind a brief, so it is asked both ways.
        out.append((str(want), fixture.id))
        out.append((f"What did {want} contact us about?", fixture.id))
    return out


# --- embedding backends ------------------------------------------------------


def _openai_embedder(model: str) -> Callable[[list[str]], list[list[float]]]:
    import os

    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    def embed(texts: list[str]) -> list[list[float]]:
        response = client.embeddings.create(input=texts, model=model)
        return [item.embedding for item in response.data]

    return embed


def _ollama_embedder(model: str) -> Callable[[list[str]], list[list[float]]]:
    import httpx

    from companies_research.config import SETTINGS

    host = SETTINGS.ollama_host.rstrip("/")

    def embed(texts: list[str]) -> list[list[float]]:
        # Ollama's batch endpoint; one request rather than one per document so
        # the latency figure is comparable with the hosted APIs.
        response = httpx.post(f"{host}/api/embed",
                              json={"model": model, "input": texts}, timeout=180.0)
        response.raise_for_status()
        return response.json()["embeddings"]

    return embed


def _build(candidate: Candidate) -> Callable[[list[str]], list[list[float]]]:
    if candidate.provider == "openai":
        return _openai_embedder(candidate.model)
    if candidate.provider == "ollama":
        return _ollama_embedder(candidate.model)
    raise ValueError(f"unknown provider {candidate.provider!r}")


# --- scoring -----------------------------------------------------------------


def _evaluate(candidate: Candidate, fixtures: list[Fixture],
              queries: list[tuple[str, str]]) -> Outcome:
    import numpy as np

    out = Outcome(label=candidate.label, model=candidate.model,
                  provider=candidate.provider)
    try:
        embed = _build(candidate)
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        return out

    documents = [_document(f) for f in fixtures]
    ids = [f.id for f in fixtures]

    try:
        started = time.monotonic()
        doc_vectors = embed(documents)
        out.embed_seconds = round(time.monotonic() - started, 2)

        latencies: list[float] = []
        query_vectors: list[list[float]] = []
        for text, _ in queries:
            t0 = time.monotonic()
            query_vectors.append(embed([text])[0])
            latencies.append(time.monotonic() - t0)
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        return out

    matrix = np.array(doc_vectors, dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    out.dimensions = matrix.shape[1]

    hits1 = hits3 = 0
    reciprocal: list[float] = []
    for (text, want), vector in zip(queries, query_vectors):
        q = np.array(vector, dtype=np.float32)
        q /= np.linalg.norm(q)
        order = np.argsort(-(matrix @ q))
        ranked = [ids[i] for i in order]
        rank = ranked.index(want) + 1 if want in ranked else 0
        if rank == 1:
            hits1 += 1
        else:
            out.misses.append(f"{text[:40]!r} → {ranked[0]} (wanted {want}, rank {rank})")
        if 1 <= rank <= 3:
            hits3 += 1
        reciprocal.append(1.0 / rank if rank else 0.0)

    n = len(queries) or 1
    out.recall_at_1 = round(hits1 / n, 4)
    out.recall_at_3 = round(hits3 / n, 4)
    out.mrr = round(statistics.mean(reciprocal), 4)
    out.query_latency_p50 = round(statistics.median(latencies), 3) if latencies else 0.0

    # Tokens are estimated at 4 chars each — good to ~10%, and the figure is a
    # price comparison rather than an invoice.
    out.tokens = sum(len(d) for d in documents) // 4
    rate = next((v for k, v in EMBED_PRICING.items() if candidate.model.startswith(k)), 0.0)
    per_doc = (out.tokens / len(documents)) if documents else 0
    out.cost_per_1000_docs = round(per_doc * 1000 / 1_000_000 * rate, 4)
    return out


DEFAULT_CANDIDATES = [
    Candidate("openai-3-small", "openai", "text-embedding-3-small"),
    Candidate("openai-3-large", "openai", "text-embedding-3-large"),
    Candidate("nomic (local)", "ollama", "nomic-embed-text"),
]


def run(*, only: str | None = None,
        candidates: list[Candidate] | None = None) -> dict[str, Any]:
    fixtures = load_fixtures(only)
    if not fixtures:
        print("No fixtures matched.")
        return {}

    queries = _queries(fixtures)
    if not queries:
        print("No fixture names a company, so there is nothing to retrieve.")
        return {}

    picked = candidates or DEFAULT_CANDIDATES
    print(f"\nRetrieval over {len(fixtures)} emails, {len(queries)} queries, "
          f"{len(picked)} embedding model(s).")
    print("A hit means the query returned that company's own email.\n")

    outcomes = []
    for candidate in picked:
        print(f"── {candidate.label} ({candidate.model})")
        outcome = _evaluate(candidate, fixtures, queries)
        if outcome.error:
            print(f"   FAILED — {outcome.error}")
        else:
            print(f"   recall@1 {outcome.recall_at_1:.1%} · recall@3 "
                  f"{outcome.recall_at_3:.1%} · MRR {outcome.mrr:.3f} · "
                  f"{outcome.dimensions}d")
        outcomes.append(outcome)

    report = {
        "fixtures": len(fixtures),
        "queries": len(queries),
        "models": {o.label: {k: v for k, v in vars(o).items() if k != "label"}
                   for o in outcomes},
    }
    _print_table(outcomes, len(queries))
    RESULTS.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote {RESULTS}")
    return report


def _print_table(outcomes: list[Outcome], n_queries: int = 0) -> None:
    good = [o for o in outcomes if not o.error]
    print(f"\n{'=' * 88}")
    print("EMBEDDING COMPARISON — retrieval over the fixture mailbox")
    print("=" * 88)
    print(f"{'model':<20}{'dims':>7}{'recall@1':>11}{'recall@3':>11}{'MRR':>8}"
          f"{'$/1k docs':>12}{'query p50':>12}")
    print("-" * 88)
    for o in outcomes:
        if o.error:
            print(f"{o.label:<20}{'FAILED — ' + o.error[:45]:>68}")
            continue
        print(f"{o.label:<20}{o.dimensions:>7}{o.recall_at_1:>10.1%}"
              f"{o.recall_at_3:>11.1%}{o.mrr:>8.3f}"
              f"{'$' + format(o.cost_per_1000_docs, '.4f'):>12}"
              f"{format(o.query_latency_p50, '.3f') + 's':>12}")

    if len(good) > 1:
        print("-" * 88)
        best = max(good, key=lambda o: o.recall_at_1)
        local = [o for o in good if o.provider == "ollama"]
        print(f"  best recall@1 : {best.label} ({best.recall_at_1:.1%})")

        # A percentage over 42 queries moves in steps of 2.4 points, so a gap
        # has to be read as a number of queries before it means anything. One
        # standard error on a proportion is sqrt(p(1-p)/n); a difference inside
        # that is a coin landing differently, not a better model.
        import math

        n = n_queries or 1
        p_hat = best.recall_at_1
        stderr = math.sqrt(max(p_hat * (1 - p_hat), 1e-9) / n)
        print(f"  sample        : {n} queries — one standard error is ±{stderr:.1%}, "
              f"so gaps under that are noise")

        if local:
            gap = best.recall_at_1 - local[0].recall_at_1
            queries = round(gap * n)
            print(f"  local costs   : {gap:+.1%} recall@1 ({queries} quer{'y' if abs(queries) == 1 else 'ies'}) "
                  f"at $0, with no message text leaving the machine")
            if gap <= stderr:
                print("  → that is inside one standard error. On this evidence the local "
                      "model is not measurably worse, and it is the only one that keeps "
                      "the mail on the machine.")
            else:
                print("  → a real gap. Weigh it against sending message text to a "
                      "third party.")

        if all(o.recall_at_3 >= 0.999 for o in good):
            print("  note          : every model scores 100% at recall@3, so this corpus "
                  "only discriminates at rank 1. Behind a brief that shows the top 3, "
                  "these models are interchangeable.")
