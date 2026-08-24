"""Long-term memory as RAG: chunk, embed locally, store in SQLite, search by cosine.

Everything runs on this machine. Embeddings come from Ollama (``nomic-embed-text``
by default) because what gets remembered is whatever the user asked to remember —
their preferences, the contents of their files — and that should not leave the
box just to become a vector. Storage is the same SQLite file as everything else;
at demo scale a linear cosine scan answers in microseconds, so a vector database
would be a second service for the same result.

The functions here are services, not tools: the gated tool wrappers in
:mod:`companies_research.tools.builtin` call them, and nothing reaches them
except through the six gates.
"""

from __future__ import annotations

import logging
import math

import httpx

from .config import SETTINGS
from .store import Store

log = logging.getLogger(__name__)

# nomic-embed-text sees ~2k tokens; chunks stay well inside that so no memory
# is silently truncated at embedding time. Overlap keeps a fact that straddles
# a boundary findable from either side.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150


class MemoryUnavailable(RuntimeError):
    """Embeddings could not be produced — carries the actionable reason."""


def chunk_text(text: str, *, max_chars: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split on the nearest whitespace before the limit, with overlap.

    A hard cut mid-word would embed half a word at each edge; backing up to
    whitespace costs a few characters and keeps every token intact.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = text.rfind(" ", start, end)
            if cut > start + max_chars // 2:
                end = cut
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        # The overlap start also snaps to a word boundary — an overlap that
        # begins mid-word would embed a fragment no query contains.
        back = text.find(" ", max(end - overlap, start + 1), end)
        start = back + 1 if back != -1 else end
    return chunks


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed with Ollama's ``/api/embed``. Raises :class:`MemoryUnavailable`."""
    host = SETTINGS.ollama_host.rstrip("/")
    model = SETTINGS.ollama_embed_model
    try:
        response = httpx.post(
            f"{host}/api/embed",
            json={"model": model, "input": texts},
            timeout=SETTINGS.ollama_timeout,
        )
        response.raise_for_status()
        body = response.json()
    except httpx.ConnectError:
        raise MemoryUnavailable(
            f"no Ollama at {host} — start it with `ollama serve`"
        ) from None
    except httpx.HTTPStatusError as exc:
        from .agents.backends import _ollama_error

        detail = _ollama_error(exc.response)
        if "not found" in detail:
            detail += f" — pull it with `ollama pull {model}`"
        raise MemoryUnavailable(f"embedding failed: {detail}") from None
    except Exception as exc:
        raise MemoryUnavailable(f"embedding failed: {exc}") from None

    embeddings = body.get("embeddings") or []
    if len(embeddings) != len(texts):
        raise MemoryUnavailable(
            f"expected {len(texts)} embedding(s), got {len(embeddings)}"
        )
    return embeddings


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def remember(content: str, *, category: str = "general", source: str = "",
             user_id: str = "default", store: Store | None = None) -> dict:
    """Chunk, embed and persist one piece of information."""
    store = store or Store()
    chunks = chunk_text(content)
    if not chunks:
        return {"status": "empty", "saved_chunks": 0, "category": category}

    vectors = embed_texts(chunks)
    for index, (piece, vector) in enumerate(zip(chunks, vectors)):
        store.add_memory(text=piece, embedding=vector, category=category,
                         source=source, chunk_index=index, user_id=user_id)
    return {
        "status": "saved",
        "saved_chunks": len(chunks),
        "category": category,
        "content_preview": content[:100],
    }


def _render_profile(record: dict) -> str:
    """One research row as compact prose — what an embedding can hold onto."""
    profile = record.get("profile") or {}
    name = profile.get("name") or record.get("company_name") or record.get("domain", "")
    lines = [f"{name} ({record.get('domain', '')})"]
    if profile.get("one_liner"):
        lines.append(profile["one_liner"])
    if profile.get("description"):
        lines.append(profile["description"])
    facts = [f"{label}: {profile[key]}" for label, key in (
        ("Industry", "industry"), ("HQ", "hq_location"),
        ("Size", "size_estimate"), ("Founded", "founded"),
    ) if profile.get(key)]
    if facts:
        lines.append("; ".join(facts))
    if profile.get("products"):
        lines.append("Products: " + ", ".join(map(str, profile["products"])))
    if profile.get("meeting_prep"):
        lines.append("Meeting prep: " + " ".join(map(str, profile["meeting_prep"])))
    return "\n".join(lines)


def index_knowledge(*, user_id: str = "default", store: Store | None = None) -> dict:
    """Embed what this agent already knows — research profiles and briefs.

    This is what turns the memory from a notepad into the agent's knowledge
    base: after indexing, ``search_memory`` answers from the same corpus the
    dashboard shows. Idempotent — each source's rows are replaced, so running
    it after every scan only re-embeds what exists.
    """
    store = store or Store()
    indexed = {"research": 0, "briefs": 0, "chunks": 0}

    def reindex(source: str, text: str) -> int:
        """Embed first, replace second: a dead Ollama must cost nothing,
        not silently delete what an earlier run indexed."""
        chunks = chunk_text(text)
        if not chunks:
            return 0
        vectors = embed_texts(chunks)  # raises before anything is touched
        store.replace_memories_for_source(source, user_id=user_id)
        for index, (piece, vector) in enumerate(zip(chunks, vectors)):
            store.add_memory(text=piece, embedding=vector, category="knowledge",
                             source=source, chunk_index=index, user_id=user_id)
        return len(chunks)

    for record in store.iter_research():
        text = _render_profile(record)
        if not text.strip():
            continue
        indexed["chunks"] += reindex(f"research:{record['domain']}", text)
        indexed["research"] += 1

    for record in store.list_briefs():
        if not record.get("brief"):
            continue
        try:
            from .briefs.render import to_markdown
            from .models import Brief

            text = to_markdown(Brief.model_validate(record["brief"]))
        except Exception:  # an unrenderable brief still has a headline
            text = f"Brief for {record.get('company', '')} ({record.get('domain', '')})"
        indexed["chunks"] += reindex(f"brief:{record['id']}", text)
        indexed["briefs"] += 1

    return indexed


def recall(query: str, *, top_k: int = 5, user_id: str = "default",
           store: Store | None = None) -> dict:
    """Semantic search over everything remembered, best matches first."""
    store = store or Store()
    rows = store.iter_memories(user_id=user_id)
    if not rows:
        return {"query": query, "results_count": 0, "memories": []}

    query_vector = embed_texts([query])[0]
    scored = sorted(
        ((_cosine(query_vector, row["embedding"]), row) for row in rows),
        key=lambda pair: pair[0],
        reverse=True,
    )
    memories = [
        {
            "text": row["text"],
            "score": round(score, 4),
            "category": row["category"],
            "source": row["source"],
            "created_at": row["created_at"],
        }
        for score, row in scored[: max(top_k, 1)]
    ]
    return {"query": query, "results_count": len(memories), "memories": memories}
