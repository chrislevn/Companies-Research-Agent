"""`./start.sh langfuse verify` — prove the integration, don't trust it.

Sync says what it wrote and experiment says what it ran; both are the writer
grading its own homework. This asks the server, over the public REST API with
no SDK in between, whether each piece the integration claims to have is
actually there:

    health      the stack is up and answering
    prompts     triage + research exist and carry the `production` label
    datasets    both datasets exist, with at least as many items as the repo has
    configs     every score config this lane emits or asks a human for
    queue       the annotation queue exists (item count is reported, not judged
                — an empty queue after a clean run is a good sign, not a bad one)
    runs        at least one experiment run exists over the fixtures
    scores      scores actually landed server-side

Exit code is the answer: 0 means every required check passed, 1 names what is
missing and the command that creates it. This is what CI should call.
"""

from __future__ import annotations

from dataclasses import dataclass

from .langfuse_lane import Api, LaneError, require_config
from .langfuse_sync import (
    HUMAN_CONFIGS,
    MACHINE_CONFIGS,
    QUALITY_DATASET,
    QUEUE_NAME,
    TRIAGE_DATASET,
    _known_prompts,
    enquiry_items,
    fixture_items,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _check_health(api: Api) -> Check:
    body = api.get("/api/public/health")
    ok = bool(body) and str(body.get("status", "")).upper() == "OK"
    return Check("health", ok, f"{api.host} → {body or 'no response'}")


def _check_prompts(api: Api) -> list[Check]:
    checks = []
    for name in sorted(_known_prompts()):
        prompt = api.get(f"/api/public/v2/prompts/{name}")
        if prompt is None:
            checks.append(Check(f"prompt:{name}", False,
                                "not found — run `langfuse sync`"))
            continue
        labels = prompt.get("labels", [])
        checks.append(Check(
            f"prompt:{name}", "production" in labels,
            f"v{prompt.get('version')} labels={labels}",
        ))
    return checks


def _check_datasets(api: Api) -> list[Check]:
    checks = []
    for name, expected in ((TRIAGE_DATASET, len(fixture_items())),
                           (QUALITY_DATASET, len(enquiry_items()))):
        dataset = api.get(f"/api/public/v2/datasets/{name}")
        if dataset is None:
            checks.append(Check(f"dataset:{name}", False,
                                "not found — run `langfuse sync`"))
            continue
        listing = api.get("/api/public/dataset-items",
                          datasetName=name, limit=1) or {}
        total = (listing.get("meta") or {}).get("totalItems", 0)
        checks.append(Check(
            f"dataset:{name}", total >= expected,
            f"{total} item(s) on the server, {expected} in the repo",
        ))
    return checks


def _check_score_configs(api: Api) -> Check:
    present: set[str] = set()
    page = 1
    while True:
        listing = api.get("/api/public/score-configs", page=page, limit=100) or {}
        present |= {c["name"] for c in listing.get("data", [])}
        if page >= ((listing.get("meta") or {}).get("totalPages") or 1):
            break
        page += 1
    wanted = {c["name"] for c in MACHINE_CONFIGS + HUMAN_CONFIGS}
    missing = sorted(wanted - present)
    return Check(
        "score-configs", not missing,
        f"{len(wanted - set(missing))}/{len(wanted)} present"
        + (f"; missing: {', '.join(missing)}" if missing else ""),
    )


def _check_queue(api: Api) -> Check:
    listing = api.get("/api/public/annotation-queues", limit=100) or {}
    queue = next((q for q in listing.get("data", [])
                  if q.get("name") == QUEUE_NAME), None)
    if queue is None:
        return Check("annotation-queue", False,
                     f"{QUEUE_NAME!r} not found — run `langfuse sync`")
    items = api.get(f"/api/public/annotation-queues/{queue['id']}/items",
                    limit=1) or {}
    total = (items.get("meta") or {}).get("totalItems", 0)
    return Check("annotation-queue", True,
                 f"{QUEUE_NAME!r} exists, {total} item(s) awaiting review")


def _check_runs(api: Api) -> Check:
    # The modern endpoint on purpose: v4 deployments in events_only mode have
    # already dropped /datasets/{name}/runs, and verify must outlive that.
    dataset = api.get(f"/api/public/v2/datasets/{TRIAGE_DATASET}") or {}
    listing = api.get("/api/public/experiments",
                      fromStartTime="2020-01-01T00:00:00Z",
                      datasetId=dataset.get("id"), limit=50)
    runs = (listing or {}).get("data", [])
    if not runs:
        return Check("experiment-runs", False,
                     "no runs over the fixtures — run `langfuse experiment`")
    names = [r.get("name", "?") for r in runs[:4]]
    more = f" (+{len(runs) - 4} more)" if len(runs) > 4 else ""
    return Check("experiment-runs", True,
                 f"{len(runs)} run(s): {', '.join(names)}{more}")


def _check_scores(api: Api) -> Check:
    # v3 paginates by cursor and reports no total, so the count is "at least".
    listing = api.get("/api/public/v3/scores", limit=100,
                      name="triage.accuracy") or {}
    found = len(listing.get("data", []))
    more = "+" if (listing.get("meta") or {}).get("cursor") else ""
    return Check("scores", found > 0,
                 f"{found}{more} `triage.accuracy` score(s) on the server")


def run(say=print) -> bool:
    try:
        require_config()
        api = Api()
        checks: list[Check] = [_check_health(api)]
        if not checks[0].ok:
            raise LaneError(f"health check failed: {checks[0].detail}")
        checks += _check_prompts(api)
        checks += _check_datasets(api)
        checks.append(_check_score_configs(api))
        checks.append(_check_queue(api))
        checks.append(_check_runs(api))
        checks.append(_check_scores(api))
        api.close()
    except LaneError as exc:
        say(f"\n✗ {exc}")
        return False

    say(f"\n{'=' * 74}")
    say("LANGFUSE INTEGRATION — verified against the public API")
    say("=" * 74)
    for check in checks:
        mark = "✓" if check.ok else "✗"
        say(f"  {mark} {check.name:<28} {check.detail}")

    failed = [c for c in checks if c.required and not c.ok]
    if failed:
        say(f"\n{len(failed)} check(s) failed.")
    else:
        say("\nAll checks passed.")
    return not failed
