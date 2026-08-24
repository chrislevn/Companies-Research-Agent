"""Observability: metrics, cost accounting and the compose stack.

Two things are worth testing here and one is not. Worth testing: that cost
arithmetic is right (a wrong number is worse than no number), and that the
compose stack is internally consistent (a port mismatch is invisible until a
demo). Not worth testing: that Prometheus counts, which is Prometheus's job.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from companies_research.obs import cost, metrics
from companies_research.obs.cost import CostLedger, Usage

ROOT = pathlib.Path(__file__).resolve().parents[1]


# --- cost arithmetic --------------------------------------------------------


def test_plain_tokens_price_at_list():
    # 1M in at $5 + 1M out at $25
    assert cost.price(Usage(model="claude-opus-5",
                            input_tokens=1_000_000, output_tokens=1_000_000)) == 30.0


def test_cached_reads_are_cheaper_than_fresh_input():
    fresh = cost.price(Usage(model="claude-opus-5", input_tokens=1_000_000))
    cached = cost.price(Usage(model="claude-opus-5", cache_read_tokens=1_000_000))
    assert cached == pytest.approx(fresh * 0.1)


def test_cache_writes_cost_more_than_fresh_input():
    fresh = cost.price(Usage(model="claude-opus-5", input_tokens=1_000_000))
    written = cost.price(Usage(model="claude-opus-5", cache_write_tokens=1_000_000))
    assert written == pytest.approx(fresh * 1.25)


def test_searches_are_billed_separately():
    assert cost.price(Usage(model="claude-opus-5", searches=1000)) == pytest.approx(10.0)


def test_a_local_model_costs_nothing():
    assert cost.price(Usage(model="qwen3:8b", input_tokens=5_000_000)) == 0.0


def test_an_unknown_model_prices_at_zero_not_a_guess():
    """Better to under-report than to invent a number someone might budget on."""
    assert cost.price(Usage(model="some-future-model", input_tokens=1_000_000)) == 0.0


def test_a_dated_model_id_still_prices_by_prefix():
    assert cost.price(Usage(model="claude-opus-5-20260101", input_tokens=1_000_000)) == 5.0


def test_longest_prefix_wins():
    """`claude-opus-4-8` must not be priced by a shorter `claude-opus` entry."""
    from companies_research.config import PRICING

    assert "claude-opus-4-8" in PRICING
    assert cost.price(Usage(model="claude-opus-4-8", input_tokens=1_000_000)) == 5.0


def test_ledger_accumulates_by_stage_and_model():
    ledger = CostLedger()
    ledger.add(Usage(model="claude-opus-5", input_tokens=1_000_000), stage="triage")
    ledger.add(Usage(model="claude-opus-5", input_tokens=1_000_000, searches=100),
               stage="research")
    assert ledger.by_stage["triage"] == 5.0
    assert ledger.by_stage["research"] == 6.0
    assert ledger.total_usd == 11.0
    assert ledger.by_model["claude-opus-5"] == 11.0
    assert "$11.0000" in ledger.summary()


def test_usage_is_read_off_a_response_not_estimated():
    class FakeUsage:
        input_tokens, output_tokens = 1200, 300
        cache_read_input_tokens, cache_creation_input_tokens = 900, 0

    class FakeResponse:
        usage, model = FakeUsage(), "claude-opus-5"

    usage = cost.usage_from_response(FakeResponse(), searches=2)
    assert (usage.input_tokens, usage.output_tokens) == (1200, 300)
    assert usage.cache_read_tokens == 900
    assert usage.searches == 2


def test_missing_usage_fields_do_not_raise():
    class Bare:
        pass

    assert cost.usage_from_response(Bare(), model="claude-opus-5").input_tokens == 0


# --- metrics ---------------------------------------------------------------


def test_every_required_metric_family_exists():
    """The eight the work order names."""
    required = {
        "agent_tool_calls_total", "agent_tool_denied_total",
        "agent_tool_duration_seconds", "agent_llm_tokens_total",
        "agent_llm_cost_usd_total", "agent_stage_duration_seconds",
        "agent_brief_cost_usd", "agent_scan_leads_total",
        "agent_start_time_seconds", "agent_build_info",
    }
    metrics.record_tool_call(tool="t", caller="c", ok=True, denied_at=None, duration_ms=5)
    metrics.record_tool_call(tool="t", caller="c", ok=False, denied_at="scopes", duration_ms=1)
    metrics.record_stage("triage", 0.5)
    metrics.record_usage(model="m", stage="triage", input_tokens=1, output_tokens=1,
                         cost_usd=0.01)
    metrics.record_brief_cost(0.02)
    metrics.record_scan_outcome("lead", 1)
    metrics.mark_started()

    text = metrics.snapshot()
    for family in required:
        assert family in text, f"{family} is not exported"


def test_a_denial_is_labelled_with_the_gate():
    metrics.record_tool_call(tool="deliver_brief", caller="x", ok=False,
                             denied_at="scopes", duration_ms=0)
    text = metrics.snapshot()
    assert 'agent_tool_denied_total{gate="scopes",tool="deliver_brief"}' in text


def test_latency_buckets_cover_a_multi_minute_research_call():
    """Default buckets top out at 10s and would bin most research as +Inf."""
    assert max(metrics.LATENCY_BUCKETS) >= 300


def test_metrics_bind_to_localhost_by_default():
    """The rest of this app is 127.0.0.1-only; metrics should not differ."""
    from companies_research.config import SETTINGS

    assert SETTINGS.metrics_host == "127.0.0.1"


def test_metrics_never_carry_message_content():
    """An audit trail that copies mail out of the database is a second leak."""
    text = metrics.snapshot()
    for label in ("subject", "body", "sender", "email", "recipient"):
        assert f"{label}=" not in text


# --- the compose stack ------------------------------------------------------


def _yaml(name: str):
    return yaml.safe_load((ROOT / name).read_text())


def test_compose_file_is_valid_and_complete():
    services = _yaml("docker-compose.yml")["services"]
    assert {"prometheus", "grafana", "tempo"} <= set(services)


def test_prometheus_scrapes_the_port_the_agent_exports():
    """A port mismatch here is invisible until the dashboard is empty."""
    from companies_research.config import SETTINGS

    target = _yaml("prometheus.yml")["scrape_configs"][0]["static_configs"][0]["targets"][0]
    assert target.endswith(f":{SETTINGS.metrics_port}")


def test_metrics_port_differs_from_the_web_interface():
    """The work order requires them separated; the UI is token-gated, this is not."""
    from companies_research.config import SETTINGS

    assert SETTINGS.metrics_port != 8765


def test_dashboard_is_provisioned_where_grafana_looks_for_it():
    provider = _yaml("grafana/provisioning/dashboards/dashboards.yml")["providers"][0]
    path = provider["options"]["path"]
    mounts = [v.split(":")[1] for v in _yaml("docker-compose.yml")["services"]["grafana"]["volumes"]]
    assert path in mounts, "the dashboard directory is not mounted into the container"


def test_dashboard_has_every_required_row():
    dashboard = json.loads((ROOT / "grafana/dashboards/agent.json").read_text())
    rows = [p["title"] for p in dashboard["panels"] if p["type"] == "row"]
    for required in ("Health & uptime", "Scan overview", "Per-agent success rate",
                     "Tool gate denials", "Latency by stage", "Cost per brief",
                     "Recent traces", "Dependencies & throughput"):
        assert any(required.lower() in r.lower() for r in rows), f"missing row: {required}"


def test_dashboard_panel_ids_are_unique():
    """Duplicate ids make Grafana drop panels silently."""
    dashboard = json.loads((ROOT / "grafana/dashboards/agent.json").read_text())
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))


def test_every_dashboard_query_references_a_metric_we_emit():
    """A panel querying a metric that does not exist is a blank panel.

    Names are pulled *out of the query* rather than checked against a list we
    also maintain here — a list would only catch names it already knew, so a
    typo would match nothing and the test would pass having checked nothing.
    """
    import re

    dashboard = json.loads((ROOT / "grafana/dashboards/agent.json").read_text())
    metrics.mark_started()          # labelled gauges have no child until set
    exported = metrics.snapshot()
    # Everything prometheus_client derives from a histogram or counter.
    suffixes = ("_bucket", "_count", "_sum", "_total", "_created")

    checked = 0
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            for name in set(re.findall(r"\bagent_[a-z0-9_]+", target.get("expr", ""))):
                base = name
                for suffix in suffixes:
                    if base.endswith(suffix):
                        base = base[: -len(suffix)]
                        break
                assert base in exported, (
                    f"panel {panel['title']!r} queries {name!r}, which the agent "
                    "does not export"
                )
                checked += 1
    assert checked > 10, "the scan found almost no queries — the extraction is broken"


def test_tempo_receives_on_the_port_the_agent_sends_to():
    from companies_research.config import SETTINGS

    endpoint = _yaml("tempo.yml")["distributor"]["receivers"]["otlp"]["protocols"]["http"]["endpoint"]
    assert endpoint.rsplit(":", 1)[1] in SETTINGS.otlp_endpoint


# --- alerting ---------------------------------------------------------------


def test_alert_rules_are_valid_and_grouped():
    groups = _yaml("alerts.yml")["groups"]
    assert groups, "no alert groups"
    for group in groups:
        assert group["rules"], f"group {group['name']!r} has no rules"
        for rule in group["rules"]:
            assert rule["expr"].strip(), f"{rule['alert']} has an empty expression"
            assert rule["labels"]["severity"] in ("critical", "warning", "info")
            assert rule["annotations"]["summary"], f"{rule['alert']} has no summary"


def test_every_alert_queries_a_metric_we_actually_emit():
    """A rule on a metric that does not exist never fires, and looks fine."""
    import re

    metrics.mark_started()
    exported = metrics.snapshot()
    suffixes = ("_bucket", "_count", "_sum", "_total", "_created")

    checked = 0
    for group in _yaml("alerts.yml")["groups"]:
        for rule in group["rules"]:
            for name in set(re.findall(r"\bagent_[a-z0-9_]+", rule["expr"])):
                base = name
                for suffix in suffixes:
                    if base.endswith(suffix):
                        base = base[: -len(suffix)]
                        break
                assert base in exported, (
                    f"alert {rule['alert']!r} queries {name!r}, which the agent "
                    "does not export"
                )
                checked += 1
    assert checked > 5, "the scan found almost no metrics — the extraction is broken"


def test_prometheus_loads_the_rules_file_that_is_mounted():
    """A rules file present on disk but unmounted is a file nobody evaluates."""
    referenced = _yaml("prometheus.yml")["rule_files"]
    mounts = [v.split(":")[1]
              for v in _yaml("docker-compose.yml")["services"]["prometheus"]["volumes"]]
    for path in referenced:
        assert path in mounts, f"{path} is referenced but never mounted"


def test_the_agent_down_alert_exists_and_matches_the_scrape_job():
    """A job-name typo makes AgentDown silently unfirable."""
    job = _yaml("prometheus.yml")["scrape_configs"][0]["job_name"]
    rules = [r for g in _yaml("alerts.yml")["groups"] for r in g["rules"]]
    down = next((r for r in rules if r["alert"] == "AgentDown"), None)
    assert down, "there is no alert for the agent being unreachable"
    assert job in down["expr"], f"AgentDown does not reference the {job!r} job"


def test_tempo_pushes_to_a_prometheus_that_accepts_remote_write():
    """The service graph exists only if this seam lines up."""
    url = _yaml("tempo.yml")["metrics_generator"]["storage"]["remote_write"][0]["url"]
    assert "prometheus:9090" in url
    cmd = _yaml("docker-compose.yml")["services"]["prometheus"]["command"]
    assert any("remote-write-receiver" in c for c in cmd), (
        "Tempo pushes service-graph metrics but Prometheus will refuse them"
    )
