"""`./start.sh eval` — score the fixtures and print the table."""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

from .harness import (
    Fixture,
    iter_research,
    load_fixtures,
    save_recordings,
    triage_fixtures,
)
from .harness import RECORDINGS
from .scoring import FIELDS, Scorecard, score_research, score_triage

RESULTS = pathlib.Path("eval_results.json")


def _record_research(fixtures: list[Fixture], results: dict) -> None:
    """Research the fixtures that assert on research, and keep the profiles.

    Their domains are `.example`, which never resolves — deliberately. The
    question these fixtures ask is what the model does when it can find
    nothing, and the honest answer is empty fields plus a note saying so.
    """
    from companies_research.research import build_researcher

    needing = [f for f in fixtures if f.expected_research]
    if not needing:
        return
    print(f"\nRecording research for {len(needing)} fixture(s)…")
    researcher = build_researcher()
    recorded: dict[str, Any] = {}
    for fixture in needing:
        triaged = results.get(fixture.id)
        domain = (triaged.company_domain if triaged else "") or ""
        company = (triaged.company_name if triaged else "") or ""
        print(f"  {fixture.id} — {company or domain}")
        outcome = researcher.research(company=company, domain=domain)
        if outcome.ok and outcome.profile is not None:
            recorded[fixture.id] = json.loads(outcome.profile.model_dump_json())
        else:
            print(f"    (no profile: {outcome.error})")
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    (RECORDINGS / "research.json").write_text(
        json.dumps(recorded, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )
    print(f"  wrote {len(recorded)} research recording(s)")


def run(*, live: bool = False, only: str | None = None,
        results_path: pathlib.Path | None = None) -> dict[str, Any]:
    started = time.monotonic()
    fixtures = load_fixtures(only)
    if not fixtures:
        print("No fixtures matched.")
        return {}

    results, backend = triage_fixtures(fixtures, live=live)

    if live:
        # Recording is the point of a live run: capture what the model said so
        # every later run can score it for free.
        save_recordings({
            mid: json.loads(r.model_dump_json()) for mid, r in results.items()
        })
        _record_research(fixtures, results)

    overall = Scorecard()
    # The negative class is scored separately, not folded into an average that
    # a strong lead score would hide. A false positive on a bank receipt is the
    # failure that wastes somebody's afternoon.
    by_class: dict[str, Scorecard] = {}

    for fixture in fixtures:
        result = results.get(fixture.id)
        if result is None:
            overall.record("should_research", False, fixture=fixture.id,
                           detail="no result produced")
            continue
        card = by_class.setdefault(fixture.klass, Scorecard())
        score_triage(result, fixture.expected, fixture=fixture.id, card=overall)
        score_triage(result, fixture.expected, fixture=fixture.id, card=card)

    for fixture, profile in iter_research(fixtures):
        card = by_class.setdefault(fixture.klass, Scorecard())
        if profile is None:
            for name in ("industry", "news_recency", "no_fabricated_claims"):
                if fixture.expected_research.get(name) is not None:
                    overall.record(name, False, fixture=fixture.id,
                                   detail="no research recording")
            continue
        score_research(profile, fixture.expected_research, fixture=fixture.id, card=overall)
        score_research(profile, fixture.expected_research, fixture=fixture.id, card=card)

    elapsed = time.monotonic() - started
    report = _report(fixtures, results, overall, by_class, elapsed, live=live,
                     misses=getattr(backend, "misses", []))
    _print(report, overall, by_class)

    path = results_path or RESULTS
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote {path}")
    return report


def _false_positive_rate(fixtures: list[Fixture], results: dict) -> tuple[int, int, list[str]]:
    """How often a non-lead was called a lead. The number that matters most."""
    negatives = [f for f in fixtures if f.klass == "negative"]
    wrong = [
        f.id for f in negatives
        if (r := results.get(f.id)) is not None and r.should_research
    ]
    return len(wrong), len(negatives), wrong


def _injection_holds(fixtures: list[Fixture], results: dict) -> tuple[int, int, list[str]]:
    """Did triage still classify correctly while being told not to?"""
    injections = [f for f in fixtures if f.klass == "injection"]
    held = []
    for f in injections:
        r = results.get(f.id)
        if r is not None and bool(r.should_research) == f.expected.get("should_research"):
            held.append(f.id)
    return len(held), len(injections), [f.id for f in injections if f.id not in held]


def _report(fixtures, results, overall, by_class, elapsed, *, live, misses) -> dict:
    fp_count, fp_total, fp_ids = _false_positive_rate(fixtures, results)
    inj_held, inj_total, inj_failed = _injection_holds(fixtures, results)
    return {
        "mode": "live" if live else "replay",
        "fixtures": len(fixtures),
        "elapsed_seconds": round(elapsed, 2),
        "missing_recordings": sorted(set(misses)),
        "fields": {
            name: {"hits": s.hits, "total": s.total, "rate": round(s.rate, 4)}
            for name, s in overall.fields.items()
        },
        "by_class": {
            klass: {
                name: {"hits": s.hits, "total": s.total, "rate": round(s.rate, 4)}
                for name, s in card.fields.items()
            }
            for klass, card in by_class.items()
        },
        "negative_class": {
            "false_positives": fp_count, "total": fp_total,
            "false_positive_rate": round(fp_count / fp_total, 4) if fp_total else 0.0,
            "offenders": fp_ids,
        },
        "injection": {
            "classified_correctly": inj_held, "total": inj_total, "failed": inj_failed,
        },
        "failures": overall.failures,
    }


def _print(report, overall, by_class) -> None:
    print(f"\n{'=' * 66}")
    print(f"EVAL — {report['fixtures']} fixtures, {report['mode']} mode, "
          f"{report['elapsed_seconds']}s")
    print("=" * 66)

    if report["missing_recordings"]:
        print(f"\n⚠ {len(report['missing_recordings'])} fixture(s) have no recording. "
              "Run `./start.sh eval --record` first; scores below are incomplete.")

    print(f"\n{'field':<22} {'score':>16}")
    print("-" * 40)
    for name in FIELDS:
        score = overall.fields.get(name)
        print(f"{name:<22} {score.render() if score else '     n/a':>16}")

    neg = report["negative_class"]
    print(f"\n{'-' * 66}")
    print("NEGATIVE CLASS — the number that matters most")
    print(f"{'-' * 66}")
    print(f"  false positives: {neg['false_positives']}/{neg['total']} "
          f"({neg['false_positive_rate']:.0%})")
    print("  A non-lead called a lead costs a research call and somebody's attention.")
    if neg["offenders"]:
        print(f"  offenders: {', '.join(neg['offenders'])}")

    inj = report["injection"]
    print(f"\n  injection: {inj['classified_correctly']}/{inj['total']} still classified "
          "correctly while being instructed otherwise")
    if inj["failed"]:
        print(f"  swayed: {', '.join(inj['failed'])}")

    print(f"\n{'-' * 66}")
    print("BY CLASS")
    print(f"{'-' * 66}")
    for klass in sorted(by_class):
        card = by_class[klass]
        hits = sum(s.hits for s in card.fields.values())
        total = sum(s.total for s in card.fields.values())
        rate = hits / total if total else 0
        print(f"  {klass:<12} {hits:>3}/{total:<3} ({rate:5.0%})")

    if report["failures"]:
        print(f"\n{'-' * 66}")
        print(f"FAILURES ({len(report['failures'])})")
        print(f"{'-' * 66}")
        for f in report["failures"][:25]:
            print(f"  {f['fixture']:<26} {f['field']:<20} {f['detail']}")
        if len(report["failures"]) > 25:
            print(f"  … and {len(report['failures']) - 25} more (see eval_results.json)")
