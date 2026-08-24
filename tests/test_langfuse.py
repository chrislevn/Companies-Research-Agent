"""Langfuse integration.

Almost everything here is about the two ways this can go wrong, neither of
which is "the traces look nice":

1. It leaks. Langfuse's whole value is showing the prompt and the completion,
   and in this system those contain message bodies and the names of real
   people. The default has to be metadata-only, and the redactor has to hold
   when it is turned on.
2. It breaks the caller. Telemetry sits directly in the path of every model
   call now, so a Langfuse failure must never become a triage failure — and,
   less obviously, must never *hide* one either.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from companies_research.obs import langfuse as lf

ROOT = pathlib.Path(__file__).resolve().parents[1]


# --- redaction --------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "chris@acme.com",
    "Contact: Chris Le <chris.le@acme.co.uk>",
    "reply to CHRIS@ACME.COM please",
    "chris+tag@sub.acme.io",
])
def test_no_address_survives_redaction(text):
    assert "@" not in lf.redact(text), f"an address survived: {lf.redact(text)}"


def test_phone_numbers_are_removed():
    assert "555" not in lf.redact("call +1 415 555 0134")


def test_the_same_address_always_hashes_the_same():
    """'this sender again' must stay askable without knowing who they are."""
    assert lf.pseudonym("a@b.com") == lf.pseudonym("  A@B.COM ")
    assert lf.pseudonym("a@b.com") != lf.pseudonym("c@d.com")


def test_redaction_truncates_long_bodies():
    out = lf.redact("x" * 5000, limit=100)
    assert len(out) < 200 and "+4900 chars" in out


# --- the default is metadata only -------------------------------------------


def test_content_is_withheld_by_default(monkeypatch):
    monkeypatch.delenv("LANGFUSE_CAPTURE_CONTENT", raising=False)
    from companies_research.config import reload_settings

    reload_settings()
    payload = lf._payload({"system": "you are a triager", "user": "chris@acme.com wrote"})
    assert payload == {"withheld": "LANGFUSE_CAPTURE_CONTENT is off"}
    assert "acme" not in str(payload)


def test_opting_in_still_redacts_addresses(monkeypatch):
    monkeypatch.setenv("LANGFUSE_CAPTURE_CONTENT", "true")
    from companies_research.config import reload_settings

    reload_settings()
    payload = lf._payload({"user": "from chris@acme.com about a deal"})
    assert "@" not in str(payload)
    assert "deal" in str(payload), "opting in should still carry the useful text"


def test_nested_structures_are_redacted_all_the_way_down(monkeypatch):
    monkeypatch.setenv("LANGFUSE_CAPTURE_CONTENT", "true")
    from companies_research.config import reload_settings

    reload_settings()
    payload = lf._payload({"a": [{"b": "x@y.com"}], "c": ("p@q.com",)})
    assert "@" not in str(payload)


# --- it must not break, or mask, the caller ---------------------------------


def test_a_caller_exception_is_never_swallowed():
    """The bug this guards: a dead API key looking like an empty batch."""
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with lf.generation("t", model="m", stage="s"):
            raise Boom("the API failed")


def test_everything_is_a_no_op_when_disabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    from companies_research.config import reload_settings

    reload_settings()
    lf._configured = False
    with lf.generation("t", model="m", stage="s", prompt="hi") as gen:
        gen.finish(output="there", usage=None)   # must not raise


def test_setup_declines_without_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    from companies_research.config import reload_settings

    reload_settings()
    lf._configured = False
    lf._client = None
    assert lf.setup() is False, "enabled without keys must decline, not crash"


# --- the stack ---------------------------------------------------------------


def _lfyaml():
    return yaml.safe_load((ROOT / "docker-compose.langfuse.yml").read_text())


def _published(service) -> list[str]:
    return [str(p).split(":")[-2] for p in service.get("ports", [])]


def _host_port(port) -> str:
    """The host-side port of a compose mapping, with or without a bind IP."""
    return str(port).strip('"').split(":")[-2]


def test_langfuse_never_claims_a_port_the_other_stack_uses():
    """3000 is Grafana and 9090 is Prometheus; upstream wants both."""
    main = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    taken = {_host_port(p) for svc in main.values() for p in svc.get("ports", [])}

    for name, svc in _lfyaml()["services"].items():
        for port in svc.get("ports", []):
            host = _host_port(port)
            assert host not in taken, f"{name} publishes {host}, already used by the metrics stack"


def test_the_agent_default_host_matches_the_port_langfuse_publishes():
    from companies_research.config import SETTINGS

    web = _lfyaml()["services"]["langfuse-web"]
    published = [_host_port(p) for p in web["ports"]]
    assert any(p in SETTINGS.langfuse_host for p in published), (
        f"LANGFUSE_HOST is {SETTINGS.langfuse_host} but langfuse-web publishes {published}"
    )


def test_every_published_port_binds_loopback_only():
    """A port published without a bind IP is on every interface, and Docker's
    iptables rules walk straight past ufw — on a VPS that is public. Grafana
    here is anonymous-admin and Langfuse holds mail-derived data, so every
    mapping must carry the 127.0.0.1: prefix; reaching one remotely is what
    SSH tunnels are for."""
    for filename in ("docker-compose.yml", "docker-compose.langfuse.yml",
                     "docker-compose.deploy.yml"):
        services = yaml.safe_load((ROOT / filename).read_text())["services"]
        for name, svc in services.items():
            for port in svc.get("ports", []):
                assert str(port).strip('"').startswith("127.0.0.1:"), (
                    f"{filename}: {name} publishes {port} beyond loopback"
                )


def test_no_s3_url_still_points_at_the_old_minio_port():
    """9090 became Prometheus; a stale URL sends attachment links there."""
    raw = (ROOT / "docker-compose.langfuse.yml").read_text()
    assert "localhost:9090" not in raw


def test_the_keys_in_env_example_match_what_the_stack_provisions():
    """Otherwise first boot looks broken for a reason nobody can see."""
    env = (ROOT / ".env.example").read_text()
    web = _lfyaml()["services"]["langfuse-web"]["environment"]
    for var, key in (("LANGFUSE_INIT_PROJECT_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY"),
                     ("LANGFUSE_INIT_PROJECT_SECRET_KEY", "LANGFUSE_SECRET_KEY")):
        provisioned = str(web[var]).split(":-")[1].rstrip("}")
        assert f"{key}={provisioned}" in env, f"{key} in .env.example != {var}"


def test_langfuse_is_off_by_default():
    """Six containers must never be a prerequisite for the agent starting."""
    env = (ROOT / ".env.example").read_text()
    assert "LANGFUSE_ENABLED=false" in env
    assert "LANGFUSE_CAPTURE_CONTENT=false" in env
