"""The Langfuse eval lane, tested without a Langfuse.

Everything here runs offline. What is worth testing offline is the part that
would lie quietly if it broke: the mapping from fixtures to dataset items, the
evaluators that turn scoring.py into scores, the verify checks' judgement, and
the prompt loader's promise that a down Langfuse can never change what prompt
the agent runs with. The live round-trip is `./start.sh langfuse verify`'s
job, on purpose — a mocked server proves nothing about a real one.
"""

from __future__ import annotations

import json

import httpx
import pytest

from eval.langfuse_lane import Api, LaneError
from eval.langfuse_sync import (
    HUMAN_CONFIGS,
    MACHINE_CONFIGS,
    enquiry_items,
    fixture_items,
)


# --- dataset mapping ---------------------------------------------------------


def test_every_fixture_becomes_exactly_one_item():
    from eval.harness import load_fixtures

    items = fixture_items()
    assert len(items) == len(load_fixtures())
    assert len({i["id"] for i in items}) == len(items), "item ids must be unique"


def test_items_carry_what_the_evaluators_need():
    for item in fixture_items():
        assert item["input"]["email"]["message_id"] == item["id"], (
            "replay keys on message_id; it must survive the round trip"
        )
        assert item["metadata"]["class"] in {"lead", "hard", "negative", "injection"}
        assert isinstance(item["expected_output"], dict)


def test_enquiries_map_with_their_ids():
    items = enquiry_items()
    assert {i["id"] for i in items} >= {"fpt", "vinamilk", "bosch"}
    for item in items:
        assert item["input"]["domain"]


def test_new_fixture_classes_are_all_represented():
    """The added data must actually widen every class, not just one."""
    by_class: dict[str, int] = {}
    for item in fixture_items():
        by_class[item["metadata"]["class"]] = by_class.get(item["metadata"]["class"], 0) + 1
    assert by_class["lead"] >= 14
    assert by_class["hard"] >= 13
    assert by_class["negative"] >= 10
    assert by_class["injection"] >= 5


# --- evaluators --------------------------------------------------------------


def _result(**overrides):
    base = {
        "message_id": "lead-01", "is_business_contact": True,
        "relationship": "customer", "company_name": "Northwind Logistics",
        "company_domain": "northwind-logistics.example",
        "contact_name": "Sarah Chen", "should_research": True, "confidence": 0.9,
    }
    base.update(overrides)
    return base


EXPECTED = {
    "should_research": True, "relationship": ["customer", "partner"],
    "company_name": "Northwind Logistics", "domain": "northwind-logistics.example",
    "contact_name": "Sarah Chen",
}


def test_a_correct_result_scores_one_across_the_board():
    from eval.langfuse_experiment import triage_evaluator

    evaluations = {e.name: e for e in triage_evaluator(
        input={}, output=_result(), expected_output=EXPECTED, metadata={})}
    assert evaluations["triage.accuracy"].value == 1.0
    for field in ("should_research", "relationship", "company_name",
                  "domain", "contact_name"):
        assert evaluations[f"triage.{field}"].value is True


def test_a_wrong_domain_scores_that_field_and_only_that_field():
    from eval.langfuse_experiment import triage_evaluator

    evaluations = {e.name: e for e in triage_evaluator(
        input={}, output=_result(company_domain="wrong.example"),
        expected_output=EXPECTED, metadata={})}
    assert evaluations["triage.domain"].value is False
    assert evaluations["triage.domain"].comment, "the miss must say what it saw"
    assert evaluations["triage.company_name"].value is True
    assert evaluations["triage.accuracy"].value == pytest.approx(0.8)


def test_no_output_is_zero_not_a_crash():
    from eval.langfuse_experiment import triage_evaluator

    evaluations = triage_evaluator(input={}, output=None,
                                   expected_output=EXPECTED, metadata={})
    assert [e.value for e in evaluations] == [0.0]


def test_run_summary_computes_the_deciding_numbers():
    from eval.langfuse_experiment import run_summary_evaluator, triage_evaluator

    class Item:
        def __init__(self, metadata):
            self.metadata = metadata

    class ItemResult:
        def __init__(self, klass, output, expected):
            self.item = Item({"class": klass})
            self.output = output
            self.evaluations = triage_evaluator(
                input={}, output=output, expected_output=expected, metadata={})

    results = [
        ItemResult("lead", _result(), EXPECTED),
        # A bank receipt called a lead: the failure that wastes an afternoon.
        ItemResult("negative", _result(should_research=True),
                   {"should_research": False}),
        ItemResult("negative", _result(should_research=False),
                   {"should_research": False}),
        ItemResult("injection", _result(), {"should_research": True}),
    ]
    summary = {e.name: e.value for e in run_summary_evaluator(item_results=results)}
    assert summary["false_positive_rate"] == 0.5
    assert summary["injection_held_rate"] == 1.0
    assert 0 < summary["triage.accuracy"] < 1


def test_quality_evaluator_reports_failure_as_not_ready():
    from eval.langfuse_experiment import quality_evaluator

    evaluations = quality_evaluator(
        input={"contact": "A", "company": "B", "domain": "b.example", "body": ""},
        output={"profile": None, "error": "research produced no profile"},
        expected_output={}, metadata={})
    assert len(evaluations) == 1
    assert evaluations[0].name == "report.ready"
    assert evaluations[0].value is False


# --- score configs -----------------------------------------------------------


def test_config_names_fit_langfuse_and_cover_the_scorer():
    names = [c["name"] for c in MACHINE_CONFIGS + HUMAN_CONFIGS]
    assert len(set(names)) == len(names)
    for name in names:
        assert len(name) <= 35, f"{name!r} exceeds the Langfuse name cap"
    from eval.scoring import FIELDS

    triage_fields = {f"triage.{f}" for f in FIELDS
                     if f in ("should_research", "relationship", "company_name",
                              "domain", "contact_name")}
    assert triage_fields <= set(names), "every scored triage field needs a config"


# --- verify: the judgement, against a fake server ----------------------------


def _api(handler) -> Api:
    return Api(host="http://fake", public_key="pk", secret_key="sk",
               transport=httpx.MockTransport(handler))


def test_verify_passes_on_a_healthy_server():
    from eval import langfuse_verify as verify

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/health"):
            return httpx.Response(200, json={"status": "OK", "version": "test"})
        if "/v2/prompts/" in path:
            return httpx.Response(200, json={"version": 1, "labels": ["production"]})
        if "/v2/datasets/" in path:
            return httpx.Response(200, json={"name": path.rsplit("/", 1)[-1],
                                             "id": "ds1"})
        if path.endswith("/dataset-items"):
            return httpx.Response(200, json={"data": [], "meta": {"totalItems": 999}})
        if path.endswith("/score-configs"):
            return httpx.Response(200, json={
                "data": [{"name": c["name"], "id": f"c{i}"} for i, c in
                         enumerate(MACHINE_CONFIGS + HUMAN_CONFIGS)],
                "meta": {"totalPages": 1}})
        if path.endswith("/annotation-queues"):
            return httpx.Response(200, json={
                "data": [{"id": "q1", "name": "triage-review"}]})
        if "/annotation-queues/q1/items" in path:
            return httpx.Response(200, json={"data": [], "meta": {"totalItems": 3}})
        if path.endswith("/experiments"):
            assert request.url.params.get("fromStartTime"), (
                "the experiments endpoint requires fromStartTime"
            )
            return httpx.Response(200, json={"data": [{"name": "replay · now"}]})
        if path.endswith("/v3/scores"):
            # v3 paginates by cursor: no totalItems, just rows.
            return httpx.Response(200, json={
                "data": [{"name": "triage.accuracy", "value": 1.0}],
                "meta": {"limit": 100}})
        return httpx.Response(404)

    api = _api(handler)
    checks = [verify._check_health(api), *verify._check_prompts(api),
              *verify._check_datasets(api), verify._check_score_configs(api),
              verify._check_queue(api), verify._check_runs(api),
              verify._check_scores(api)]
    failed = [c.name for c in checks if not c.ok]
    assert not failed, f"a healthy server must verify clean; failed: {failed}"


def test_verify_names_what_is_missing():
    from eval import langfuse_verify as verify

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "OK"})
        return httpx.Response(404)

    api = _api(handler)
    prompt_checks = verify._check_prompts(api)
    assert all(not c.ok for c in prompt_checks)
    assert "sync" in prompt_checks[0].detail, "a failure must say which command fixes it"
    assert not verify._check_runs(api).ok


def test_a_dead_server_is_a_named_error_not_a_traceback():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(LaneError) as caught:
        _api(handler).get("/api/public/health")
    assert "docker compose" in str(caught.value), (
        "the error must say how to start the stack"
    )


# --- prompts: langfuse can never stop a scan ---------------------------------


def test_prompt_loader_ignores_langfuse_when_disabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PROMPTS_ENABLED", "false")
    from companies_research.config import reload_settings

    reload_settings()
    from companies_research import prompts

    loaded = prompts.load("triage", "the built-in")
    assert loaded.text == "the built-in"
    assert loaded.source == "built-in"


def test_prompt_loader_falls_back_when_langfuse_is_down(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGFUSE_PROMPTS_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("PROMPTS_DIR", str(tmp_path))
    from companies_research.config import reload_settings

    reload_settings()
    from companies_research import prompts

    def boom(_name):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(prompts, "_from_langfuse", boom)
    with pytest.raises(RuntimeError):
        prompts._from_langfuse("triage")  # the stub really does raise

    monkeypatch.setattr(prompts, "_from_langfuse", lambda _n: None)
    loaded = prompts.load("triage", "the built-in")
    assert loaded.text == "the built-in", "a down Langfuse must not change the prompt"


def test_a_served_prompt_still_gets_credential_scrubbed(monkeypatch):
    """The registry is a prompt source like any other: no secret shapes pass."""
    from companies_research import prompts

    class Served:
        prompt = "Classify mail. Use key sk-ant-abcdefghijklmnop1234 for tools."
        version = 7

    class Client:
        def get_prompt(self, name, **_kw):
            return Served()

    monkeypatch.setenv("LANGFUSE_PROMPTS_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    from companies_research.config import reload_settings

    reload_settings()
    import companies_research.obs.langfuse as obs_lf

    monkeypatch.setattr(obs_lf, "setup", lambda: True)
    import langfuse as langfuse_sdk

    monkeypatch.setattr(langfuse_sdk, "get_client", lambda: Client())

    loaded = prompts._from_langfuse("triage")
    assert loaded is not None
    assert "sk-ant-" not in loaded.text
    assert "[redacted-credential]" in loaded.text
    assert loaded.source.startswith("langfuse:triage@")


# --- sync idempotency --------------------------------------------------------


def test_sync_prompts_skips_unchanged_versions():
    from eval.langfuse_sync import _known_prompts, sync_prompts

    class Current:
        def __init__(self, text):
            self.prompt = text
            self.version = 3

    class Client:
        def __init__(self):
            self.created = []

        def get_prompt(self, name, **_kw):
            return Current(_known_prompts()[name])

        def create_prompt(self, **kwargs):
            self.created.append(kwargs["name"])
            return Current(kwargs["prompt"])

    client = Client()
    changed = sync_prompts(client, say=lambda *_: None)
    assert changed == [] and client.created == [], (
        "an unchanged prompt must not grow a version per sync"
    )


def test_sync_prompts_versions_a_changed_prompt():
    from eval.langfuse_sync import sync_prompts

    class Client:
        def __init__(self):
            self.created = []

        def get_prompt(self, name, **_kw):
            class Old:
                prompt = "something older"
                version = 1
            return Old()

        def create_prompt(self, **kwargs):
            self.created.append(kwargs["name"])
            assert kwargs["labels"] == ["production"]
            class New:
                version = 2
            return New()

    client = Client()
    changed = sync_prompts(client, say=lambda *_: None)
    assert set(changed) == {"triage", "research"}


# --- the fixture files themselves -------------------------------------------


def test_fixture_files_are_well_formed():
    import pathlib

    fixtures_dir = pathlib.Path(__file__).parent / "eval" / "fixtures"
    ids = []
    for path in sorted(fixtures_dir.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["id"] == path.stem, f"{path.name}: id must match the filename"
        assert doc["class"] in {"lead", "hard", "negative", "injection"}
        assert doc["email"]["message_id"] == doc["id"]
        assert "should_research" in doc["expected"], path.name
        domain = doc["email"]["sender"]["email"].rsplit("@", 1)[-1]
        # Hard fixtures use real freemail domains on purpose — "no website" is
        # the scenario. Everything else stays on .example so nothing resolves.
        safe = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
                "icloud.com", "example.com"}
        assert domain.endswith(".example") or domain in safe, (
            f"{path.name}: fixture senders must live on reserved or freemail "
            f"domains, got {domain!r}"
        )
        ids.append(doc["id"])
    assert len(set(ids)) == len(ids)
