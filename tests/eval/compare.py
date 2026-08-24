"""`./start.sh compare` — run the same fixtures through several models.

The eval next door answers "is the agent still correct". This answers a
different question: *which model should be doing the work*, and what does the
cheaper answer cost you in accuracy.

Three things make this an experiment rather than a demo:

**Only the model varies.** Same 30 fixtures, same prompt, same schema, same
batch size, same scorer. Anything that differs between two rows is the model.

**Every model is run more than once.** Triage is a sampled process, and this
project has already been bitten by it — two fixtures flipped verdict between
recording passes with nothing changed but the sampler. A single run per model
is a sample of size one dressed up as a measurement, so the table reports the
mean across passes and the spread, and a spread wider than the gap between two
models means those models are tied whatever the means say.

**The headline number is not accuracy.** A model that calls every message a
lead scores well on the lead class and is useless, because the cost of triage
is dominated by what it sends on to research. False-positive rate on the
negative class, and whether injection fixtures survive, decide this more than
overall accuracy does.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from .harness import Fixture, load_fixtures
from .scoring import FIELDS, Scorecard, score_triage

RESULTS = pathlib.Path("compare_results.json")

# The candidates worth a real decision: the current default, the two cheaper
# hosted tiers, and a local model that costs nothing and never leaves the
# machine. Ollama entries are skipped automatically when the daemon is absent.
DEFAULT_MODELS = [
    ("opus-5", "anthropic", "claude-opus-5"),
    ("sonnet-5", "anthropic", "claude-sonnet-5"),
    ("haiku-4.5", "anthropic", "claude-haiku-4-5"),
    ("qwen3-coder (local)", "ollama", "qwen3-coder:latest"),
]


@dataclass
class Call:
    latency_s: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""


@dataclass
class Pass:
    """One complete run of the fixture set against one model."""
    accuracy: float
    false_positives: int
    negatives: int
    injection_held: int
    injection_total: int
    calls: list[Call] = field(default_factory=list)
    per_field: dict[str, float] = field(default_factory=dict)
    error: str = ""

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    @property
    def wall_s(self) -> float:
        return sum(c.latency_s for c in self.calls)


class _Recorder:
    """Wraps a backend and measures it, without the pipeline noticing.

    Measuring here rather than around the whole run keeps provisioning, model
    load and scoring out of the latency figure — what is timed is the model
    answering, which is the thing being compared.
    """

    def __init__(self, inner: Any, model: str) -> None:
        self._inner = inner
        self.name = inner.name
        self.model = model
        inner.model = model              # the switch under test
        self.calls: list[Call] = []

    def describe(self) -> str:
        return f"{self._inner.describe()} [measured]"

    def complete(self, *, system: str, user: str, schema: dict) -> Any:
        from companies_research.obs.cost import price

        started = time.monotonic()
        completion = self._inner.complete(system=system, user=user, schema=schema)
        elapsed = time.monotonic() - started

        usage = completion.usage
        self.calls.append(Call(
            latency_s=elapsed,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            # Priced from the ledger's own table, so a local model is $0 and an
            # unknown model reports zero rather than a guess.
            cost_usd=price(usage) if usage is not None else 0.0,
            error=completion.error or "",
        ))
        return completion


def _one_pass(fixtures: list[Fixture], backend_name: str, model: str,
              batch_size: int) -> Pass:
    from companies_research.agents.backends import build_backend
    from companies_research.agents.triage import TriageAgent

    try:
        recorder = _Recorder(build_backend(backend_name), model)
    except Exception as exc:
        return Pass(0.0, 0, 0, 0, 0, error=f"{type(exc).__name__}: {exc}")

    agent = TriageAgent(backend=recorder)
    agent.batch_size = batch_size
    results = {r.message_id: r for r in agent.triage([f.message for f in fixtures])}

    card = Scorecard()
    for fixture in fixtures:
        result = results.get(fixture.id)
        if result is None:
            card.record("should_research", False, fixture=fixture.id)
            continue
        score_triage(result, fixture.expected, fixture=fixture.id, card=card)

    hits = sum(s.hits for s in card.fields.values())
    total = sum(s.total for s in card.fields.values())

    negatives = [f for f in fixtures if f.klass == "negative"]
    false_pos = sum(
        1 for f in negatives
        if (r := results.get(f.id)) is not None and r.should_research
    )
    injections = [f for f in fixtures if f.klass == "injection"]
    held = sum(
        1 for f in injections
        if (r := results.get(f.id)) is not None
        and bool(r.should_research) == f.expected.get("should_research")
    )

    return Pass(
        accuracy=hits / total if total else 0.0,
        false_positives=false_pos, negatives=len(negatives),
        injection_held=held, injection_total=len(injections),
        calls=recorder.calls,
        per_field={n: s.rate for n, s in card.fields.items()},
        error=next((c.error for c in recorder.calls if c.error), ""),
    )


def _ollama_is_up() -> bool:
    try:
        import httpx

        from companies_research.config import SETTINGS

        httpx.get(f"{SETTINGS.ollama_host.rstrip('/')}/api/tags", timeout=2.0)
        return True
    except Exception:
        return False


def run(*, passes: int = 3, batch_size: int = 10, only: str | None = None,
        models: list[tuple[str, str, str]] | None = None) -> dict[str, Any]:
    fixtures = load_fixtures(only)
    if not fixtures:
        print("No fixtures matched.")
        return {}

    candidates = models or DEFAULT_MODELS
    if not _ollama_is_up():
        skipped = [c for c in candidates if c[1] == "ollama"]
        if skipped:
            print(f"Ollama is not reachable — skipping {len(skipped)} local model(s).")
        candidates = [c for c in candidates if c[1] != "ollama"]

    print(f"\nComparing {len(candidates)} model(s) over {len(fixtures)} fixtures, "
          f"{passes} pass(es) each.")
    print("Only the model changes between rows.\n")

    report: dict[str, Any] = {
        "fixtures": len(fixtures), "passes": passes, "batch_size": batch_size,
        "models": {},
    }

    for label, backend_name, model in candidates:
        print(f"── {label} ({model})")
        runs: list[Pass] = []
        for n in range(passes):
            started = time.monotonic()
            result = _one_pass(fixtures, backend_name, model, batch_size)
            if result.error:
                print(f"   pass {n + 1}: FAILED — {result.error}")
            else:
                print(f"   pass {n + 1}: {result.accuracy:6.1%} accuracy, "
                      f"{result.false_positives}/{result.negatives} false positive(s), "
                      f"${result.cost_usd:.4f}, {time.monotonic() - started:.1f}s")
            runs.append(result)

        good = [r for r in runs if not r.error]
        report["models"][label] = _summarise(label, model, backend_name, good, runs,
                                             len(fixtures))

    _print_table(report)
    RESULTS.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote {RESULTS}")
    return report


def _summarise(label, model, backend_name, good, runs, n_fixtures) -> dict:
    if not good:
        return {"model": model, "backend": backend_name, "ok": False,
                "error": next((r.error for r in runs if r.error), "all passes failed")}

    accuracies = [r.accuracy for r in good]
    costs = [r.cost_usd for r in good]
    walls = [r.wall_s for r in good]
    per_call = [c.latency_s for r in good for c in r.calls]
    cost_per_email = statistics.mean(costs) / n_fixtures if n_fixtures else 0.0

    return {
        "model": model,
        "backend": backend_name,
        "ok": True,
        "passes_ok": len(good),
        "accuracy_mean": round(statistics.mean(accuracies), 4),
        # Spread is load-bearing: two models whose means differ by less than
        # this are not distinguishable by this experiment.
        "accuracy_stdev": round(statistics.stdev(accuracies), 4) if len(good) > 1 else 0.0,
        "accuracy_min": round(min(accuracies), 4),
        "accuracy_max": round(max(accuracies), 4),
        "false_positives_mean": round(statistics.mean(r.false_positives for r in good), 2),
        "negatives": good[0].negatives,
        "injection_held_mean": round(statistics.mean(r.injection_held for r in good), 2),
        "injection_total": good[0].injection_total,
        "cost_usd_mean": round(statistics.mean(costs), 6),
        "cost_per_1000_emails": round(cost_per_email * 1000, 4),
        "wall_seconds_mean": round(statistics.mean(walls), 2),
        "latency_p50_s": round(statistics.median(per_call), 2) if per_call else 0.0,
        "input_tokens_mean": round(statistics.mean(
            sum(c.input_tokens for c in r.calls) for r in good), 0),
        "output_tokens_mean": round(statistics.mean(
            sum(c.output_tokens for c in r.calls) for r in good), 0),
        "per_field_mean": {
            name: round(statistics.mean(
                r.per_field.get(name, 0.0) for r in good), 4)
            for name in FIELDS if any(name in r.per_field for r in good)
        },
    }


def _print_table(report: dict) -> None:
    rows = [(k, v) for k, v in report["models"].items() if v.get("ok")]
    failed = [(k, v) for k, v in report["models"].items() if not v.get("ok")]

    print(f"\n{'=' * 92}")
    print(f"MODEL COMPARISON — {report['fixtures']} fixtures × {report['passes']} pass(es)")
    print("=" * 92)
    print(f"{'model':<22}{'accuracy':>18}{'false pos':>12}{'injection':>12}"
          f"{'$/1k mail':>12}{'p50':>9}")
    print("-" * 92)
    for label, m in rows:
        acc = f"{m['accuracy_mean']:.1%} ±{m['accuracy_stdev']:.1%}"
        fp = f"{m['false_positives_mean']:.1f}/{m['negatives']}"
        inj = f"{m['injection_held_mean']:.1f}/{m['injection_total']}"
        print(f"{label:<22}{acc:>18}{fp:>12}{inj:>12}"
              f"{'$' + format(m['cost_per_1000_emails'], '.2f'):>12}"
              f"{format(m['latency_p50_s'], '.1f') + 's':>9}")
    for label, m in failed:
        print(f"{label:<22}{'FAILED — ' + m.get('error', '')[:50]:>60}")

    if len(rows) > 1:
        print("-" * 92)
        best = max(rows, key=lambda r: r[1]["accuracy_mean"])
        cheap = min(rows, key=lambda r: r[1]["cost_per_1000_emails"])
        spread = max(r[1]["accuracy_stdev"] for r in rows)
        gap = best[1]["accuracy_mean"] - cheap[1]["accuracy_mean"]
        print(f"  most accurate : {best[0]} ({best[1]['accuracy_mean']:.1%})")
        print(f"  cheapest      : {cheap[0]} (${cheap[1]['cost_per_1000_emails']:.2f}/1k)")
        if cheap[0] != best[0]:
            cheap_cost = cheap[1]["cost_per_1000_emails"]
            if cheap_cost:
                factor = best[1]["cost_per_1000_emails"] / cheap_cost
                print(f"  the trade     : {gap:+.1%} accuracy for {factor:.0f}× the price")
            else:
                print(f"  the trade     : {gap:+.1%} accuracy — and {cheap[0]} is free")
            if gap <= spread:
                print("  ⚠ that accuracy gap is inside the run-to-run spread — on this "
                      "evidence these models are tied on accuracy.")

        # Accuracy is the number people quote and the wrong one to decide on.
        # A model can score well and still be talked out of its verdict by the
        # message it is classifying, and that failure does not show up anywhere
        # in an accuracy average.
        weak = [(k, m) for k, m in rows
                if m["injection_total"] and
                m["injection_held_mean"] < m["injection_total"]]
        if weak:
            print(f"\n  {'-' * 88}")
            print("  INJECTION — the column accuracy hides")
            for label, m in sorted(weak, key=lambda r: r[1]["injection_held_mean"]):
                held, total = m["injection_held_mean"], m["injection_total"]
                note = " ← swayed every time" if held == 0 else ""
                print(f"    {label:<22} held {held:.1f}/{total} "
                      f"at {m['accuracy_mean']:.1%} accuracy{note}")
            print("    A model that scores well and still follows instructions buried in "
                  "the mail it is\n    reading is not a cheaper version of one that does "
                  "not. Rank on this column first.")
