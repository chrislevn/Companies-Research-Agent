"""The comparison harnesses.

Not tested here: whether one model beats another. That is what the harness
measures, and asserting it would freeze today's leaderboard into the suite.

Tested here: that the experiment is *controlled*. A comparison whose rows
differ by something other than the model under test measures nothing, and the
failure is silent — the table still prints, the numbers still look plausible,
and the conclusion is wrong. So these check the things that would quietly
invalidate a result: that only the model varies, that a local model is priced
at zero rather than at the last hosted model's rate, that a dead backend is
reported instead of being scored as 0%, and that ground truth comes from the
fixtures rather than from the thing being graded.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from eval import compare, compare_embeddings as ce   # noqa: E402
from eval.harness import load_fixtures               # noqa: E402


# --- the LLM comparison ------------------------------------------------------


class _FakeBackend:
    name = "fake"
    model = "unset"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def describe(self) -> str:
        return "fake"

    def complete(self, *, system, user, schema):
        from companies_research.agents.backends import Completion

        self.seen.append(self.model)
        return Completion(text='{"results": []}')


def test_the_recorder_switches_the_model_and_nothing_else():
    """The row label must be what the backend was actually asked to run."""
    inner = _FakeBackend()
    rec = compare._Recorder(inner, "claude-haiku-4-5")
    assert inner.model == "claude-haiku-4-5", "the model under test was not applied"
    rec.complete(system="s", user="u", schema={})
    assert inner.seen == ["claude-haiku-4-5"]


def test_a_local_model_is_priced_at_zero_not_at_the_last_hosted_rate():
    """The cheapest row is the one a pricing bug flatters most."""
    from companies_research.obs.cost import Usage, price

    assert price(Usage(model="qwen3-coder:latest",
                       input_tokens=5_000_000, output_tokens=1_000_000)) == 0.0


def test_a_dead_backend_is_reported_not_scored_as_zero():
    """Scoring an unreachable model as 0% would read as 'this model is bad'."""
    fixtures = load_fixtures()[:2]
    result = compare._one_pass(fixtures, "nonexistent-backend", "x", 10)
    assert result.error, "a missing backend produced no error"
    assert result.accuracy == 0.0


def test_more_than_one_pass_is_the_default():
    """A single run of a sampled process is not a measurement."""
    import inspect

    assert inspect.signature(compare.run).parameters["passes"].default >= 3


def test_the_summary_reports_spread_not_just_a_mean():
    runs = [compare.Pass(accuracy=a, false_positives=0, negatives=7,
                         injection_held=3, injection_total=3)
            for a in (0.90, 0.80, 0.85)]
    summary = compare._summarise("m", "m", "anthropic", runs, runs, 30)
    assert summary["accuracy_mean"] == pytest.approx(0.85, abs=1e-3)
    assert summary["accuracy_stdev"] > 0
    assert summary["accuracy_min"] == 0.80 and summary["accuracy_max"] == 0.90


# --- the embedding comparison ------------------------------------------------


def test_queries_come_from_the_fixtures_not_from_the_documents():
    """Ground truth must be the hand-labelled answer, not the text being ranked."""
    fixtures = load_fixtures()
    queries = ce._queries(fixtures)
    assert queries, "no queries were derived"
    by_id = {f.id: f for f in fixtures}
    for text, fid in queries:
        expected = by_id[fid].expected.get("company_name")
        expected = expected[0] if isinstance(expected, list) else expected
        assert str(expected).lower() in text.lower(), (
            f"query {text!r} is not derived from fixture {fid}'s expected company"
        )


def test_every_query_has_exactly_one_correct_document():
    fixtures = load_fixtures()
    ids = {f.id for f in fixtures}
    for _, fid in ce._queries(fixtures):
        assert fid in ids, f"query points at {fid}, which is not in the corpus"


def test_the_document_is_the_email_not_the_label():
    """Embedding the expected answer would score the fixture, not the model."""
    fixture = next(f for f in load_fixtures() if f.expected.get("company_name"))
    document = ce._document(fixture)
    assert "Subject:" in document and "From:" in document
    assert "expected" not in document.lower()


def test_a_local_embedder_is_free_and_a_hosted_one_is_not():
    small = ce.EMBED_PRICING["text-embedding-3-small"]
    assert ce.EMBED_PRICING["nomic-embed-text"] == 0.0
    assert small > 0
    assert ce.EMBED_PRICING["text-embedding-3-large"] > small


def test_an_unreachable_embedder_is_reported_not_scored():
    outcome = ce._evaluate(ce.Candidate("bad", "nonexistent", "x"),
                           load_fixtures()[:2], [("q", "x")])
    assert outcome.error and outcome.recall_at_1 == 0.0


def test_both_query_shapes_are_asked():
    """A retriever that only handles bare keywords is not useful behind a brief."""
    fixtures = load_fixtures()
    texts = [t for t, _ in ce._queries(fixtures)]
    assert any(t.startswith("What did") for t in texts), "no natural-language query"
    assert any(not t.startswith("What did") for t in texts), "no keyword query"


def test_the_openai_key_is_never_written_into_a_tracked_file():
    """It belongs in .env, which is gitignored — not in an example or a report."""
    import subprocess

    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, text=True).stdout.split()
    for name in tracked:
        path = ROOT / name
        if not path.is_file() or path.suffix in (".png", ".zip"):
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        assert "sk-proj-" not in body, f"an OpenAI key is committed in {name}"
