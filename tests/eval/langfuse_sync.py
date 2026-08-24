"""`./start.sh langfuse sync` — put the eval lane's fixed assets into Langfuse.

Four kinds of asset, all idempotent so the command can run on every push:

* **Prompts** — the triage and research system prompts, versioned. A new
  version is only created when the text actually changed; otherwise Langfuse
  would grow a version per sync and the history would say nothing.
* **Datasets** — the offline fixtures and the report-quality enquiries. Item
  ids are the fixture ids, so re-syncing upserts in place instead of
  duplicating.
* **Score configs** — the schema behind every score this lane emits, plus the
  three a human annotator fills in. Configs are what make the annotation UI
  render dropdowns instead of free-text.
* **An annotation queue** — where experiment failures land for human review.

The datasets contain the *fixtures*, which are synthetic mail written for this
repo. No real mailbox content passes through here, which is why this file is
allowed to send full bodies while obs/langfuse.py withholds them by default.
"""

from __future__ import annotations

from typing import Any

from .harness import load_fixtures
from .langfuse_lane import Api, sdk_client

TRIAGE_DATASET = "triage-fixtures"
QUALITY_DATASET = "report-quality-enquiries"
QUEUE_NAME = "triage-review"

# What a human reviewing a triage verdict records. Kept small on purpose: an
# annotation queue that asks ten questions per item answers none of them.
HUMAN_CONFIGS: list[dict[str, Any]] = [
    {"name": "human.verdict", "dataType": "CATEGORICAL",
     "categories": [{"label": "correct", "value": 1},
                    {"label": "incorrect", "value": 0},
                    {"label": "unsure", "value": 0.5}],
     "description": "Was the agent's triage verdict right, read as a human?"},
    {"name": "human.usefulness", "dataType": "NUMERIC",
     "minValue": 1, "maxValue": 5,
     "description": "Would this output help the person preparing the meeting? 1–5."},
    {"name": "human.note", "dataType": "TEXT",
     "description": "What the scores cannot say — why it was wrong, what to fix."},
]

# The machine-emitted scores. BOOLEAN per field mirrors scoring.py: binary on
# purpose, a numerator and denominator you can act on.
MACHINE_CONFIGS: list[dict[str, Any]] = [
    *({"name": f"triage.{f}", "dataType": "BOOLEAN",
       "description": f"Did triage get `{f}` right for this fixture?"}
      for f in ("should_research", "relationship", "company_name",
                "domain", "contact_name")),
    {"name": "triage.accuracy", "dataType": "NUMERIC", "minValue": 0, "maxValue": 1,
     "description": "Share of asserted triage fields this item got right."},
    {"name": "judge.intent_faithful", "dataType": "NUMERIC",
     "minValue": 0, "maxValue": 1,
     "description": "LLM judge: is intent_summary faithful to the email, no invention?"},
    {"name": "report.completeness", "dataType": "NUMERIC", "minValue": 0, "maxValue": 1,
     "description": "Độ đầy đủ: share of website/industry/products/size/news/contact present."},
    {"name": "report.sourced_share", "dataType": "NUMERIC", "minValue": 0, "maxValue": 1,
     "description": "Độ chính xác: share of substantive claims carrying a source URL."},
    {"name": "report.domain_match", "dataType": "BOOLEAN",
     "description": "Does the researched domain match the company's official site?"},
    {"name": "report.sources", "dataType": "NUMERIC", "minValue": 0,
     "description": "Số nguồn tham khảo: unique source URLs behind the claims."},
    {"name": "report.news_age_days", "dataType": "NUMERIC", "minValue": 0,
     "description": "Độ mới: age in days of the newest dated news item."},
    {"name": "report.seconds", "dataType": "NUMERIC", "minValue": 0,
     "description": "Thời gian tạo báo cáo: wall clock, triage + research."},
    {"name": "report.ready", "dataType": "BOOLEAN",
     "description": "Mức độ sẵn sàng: complete, sourced and meeting-prepped."},
]


def _known_prompts() -> dict[str, str]:
    from companies_research.agents.triage import SYSTEM_PROMPT as TRIAGE
    from companies_research.research.claude_web import DEFAULT_SYSTEM_PROMPT as RESEARCH

    return {"triage": TRIAGE, "research": RESEARCH}


def sync_prompts(client: Any, say=print) -> list[str]:
    """Version the built-in prompts in Langfuse. Only on change."""
    changed = []
    for name, text in sorted(_known_prompts().items()):
        current = None
        try:
            current = client.get_prompt(name, label="production",
                                        fallback=None, max_retries=1)
        except Exception:
            pass  # first sync: the prompt does not exist yet
        if current is not None and getattr(current, "prompt", None) == text:
            say(f"  prompt {name!r}: unchanged (v{getattr(current, 'version', '?')})")
            continue
        created = client.create_prompt(
            name=name, type="text", prompt=text, labels=["production"],
        )
        changed.append(name)
        say(f"  prompt {name!r}: new version v{getattr(created, 'version', '?')} "
            f"→ label 'production'")
    return changed


def fixture_items() -> list[dict[str, Any]]:
    """Fixtures, shaped as dataset items. Item id = fixture id → upserts."""
    return [
        {
            "id": f.id,
            "input": {"email": f.email},
            "expected_output": f.expected,
            "metadata": {"class": f.klass, "note": f.note,
                         "expected_research": f.expected_research or None},
        }
        for f in load_fixtures()
    ]


def enquiry_items() -> list[dict[str, Any]]:
    from .report_quality import ENQUIRIES

    return [
        {
            "id": e.id,
            "input": {"contact": e.contact, "company": e.company,
                      "domain": e.domain, "body": e.body},
            "expected_output": {"expectation": e.expectation},
            "metadata": {"live_web": True},
        }
        for e in ENQUIRIES
    ]


def sync_datasets(client: Any, say=print) -> dict[str, int]:
    counts = {}
    for name, description, items in (
        (TRIAGE_DATASET,
         "Offline triage fixtures: lead / hard / negative / injection. "
         "Synthetic mail; every domain is .example on purpose.",
         fixture_items()),
        (QUALITY_DATASET,
         "Real-company enquiries from the report-quality harness. These "
         "domains resolve; experiments over this set hit the live web.",
         enquiry_items()),
    ):
        client.create_dataset(name=name, description=description)
        for item in items:
            client.create_dataset_item(
                dataset_name=name, id=item["id"], input=item["input"],
                expected_output=item["expected_output"], metadata=item["metadata"],
            )
        counts[name] = len(items)
        say(f"  dataset {name!r}: {len(items)} item(s) upserted")
    return counts


def sync_score_configs(api: Api, say=print) -> dict[str, str]:
    """Create any score config that does not exist yet. Returns name → id."""
    existing: dict[str, str] = {}
    page = 1
    while True:
        listing = api.get("/api/public/score-configs", page=page, limit=100) or {}
        for config in listing.get("data", []):
            existing.setdefault(config["name"], config["id"])
        if page >= (listing.get("meta", {}).get("totalPages") or 1):
            break
        page += 1

    created = 0
    for config in MACHINE_CONFIGS + HUMAN_CONFIGS:
        if config["name"] in existing:
            continue
        made = api.post("/api/public/score-configs", config)
        existing[config["name"]] = made["id"]
        created += 1
    say(f"  score configs: {created} created, "
        f"{len(MACHINE_CONFIGS) + len(HUMAN_CONFIGS) - created} already present")
    return existing


def sync_annotation_queue(api: Api, config_ids: dict[str, str], say=print) -> str:
    """The queue human review happens in. Created once, reused after."""
    listing = api.get("/api/public/annotation-queues", limit=100) or {}
    for queue in listing.get("data", []):
        if queue.get("name") == QUEUE_NAME:
            say(f"  annotation queue {QUEUE_NAME!r}: already present")
            return queue["id"]

    wanted = [config_ids[c["name"]] for c in HUMAN_CONFIGS if c["name"] in config_ids]
    made = api.post("/api/public/annotation-queues", {
        "name": QUEUE_NAME,
        "description": "Experiment items the scorer flagged — verify the verdict, "
                       "rate the usefulness, say why.",
        "scoreConfigIds": wanted,
    })
    say(f"  annotation queue {QUEUE_NAME!r}: created with "
        f"{len(wanted)} human score config(s)")
    return made["id"]


def run(say=print) -> dict[str, Any]:
    client = sdk_client()
    api = Api()
    say("Syncing eval assets to Langfuse…")
    changed = sync_prompts(client, say)
    counts = sync_datasets(client, say)
    configs = sync_score_configs(api, say)
    queue_id = sync_annotation_queue(api, configs, say)
    client.flush()
    api.close()
    from companies_research.config import SETTINGS

    say(f"\nDone. Open {SETTINGS.langfuse_host} — Prompts, Datasets, "
        "Settings → Scores, Annotation.")
    return {"prompts_changed": changed, "datasets": counts,
            "score_configs": len(configs), "queue_id": queue_id}
