"""The server's front door, exercised from both sides of a tunnel.

The guard has to hold two promises at once. On a laptop, nothing changes: the
server answers localhost and refuses everything else, exactly as before
PUBLIC_HOSTS existed. Through a tunnel, the page must load and its API calls
must pass — because the whole demo dies on the first 403 otherwise — while a
hostname nobody configured is still turned away before the token-bearing page
is served.

These run against the real ASGI app with no server process: TestClient is used
without a context manager on purpose, so the lifespan (watcher, metrics) never
starts.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client_factory(monkeypatch):
    """Build a TestClient pretending to arrive at the given hostname."""
    from companies_research.webapp import server

    def make(base_url: str = "http://127.0.0.1", public_hosts: str = "") -> TestClient:
        monkeypatch.setenv("PUBLIC_HOSTS", public_hosts)
        from companies_research.config import reload_settings

        reload_settings()
        return TestClient(server.app, base_url=base_url)

    return make


def _token() -> str:
    from companies_research.webapp import server

    return server.TOKEN


# --- the local case must not change ----------------------------------------


def test_localhost_page_serves_and_carries_token(client_factory):
    page = client_factory().get("/")
    assert page.status_code == 200
    assert _token() in page.text


def test_api_refuses_missing_token(client_factory):
    response = client_factory().get("/api/auth/status")
    assert response.status_code == 401


def test_api_accepts_local_request_with_token(client_factory):
    response = client_factory().get("/api/auth/status", headers={"x-cr-token": _token()})
    assert response.status_code == 200


def test_api_refuses_foreign_origin(client_factory):
    response = client_factory().post(
        "/api/auth/login",
        json={"email": "a@b.co", "password": "x"},
        headers={"x-cr-token": _token(), "origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_unknown_host_is_refused_before_the_page(client_factory):
    """DNS rebinding: attacker.example resolves to 127.0.0.1, asks for /."""
    response = client_factory(base_url="http://attacker.example").get("/")
    assert response.status_code == 421
    assert _token() not in response.text


# --- through a tunnel -------------------------------------------------------

TUNNEL = "https://real-demo.trycloudflare.com"


def test_wildcard_public_host_serves_the_page(client_factory):
    client = client_factory(base_url=TUNNEL, public_hosts="*.trycloudflare.com")
    assert client.get("/").status_code == 200


def test_tunnel_origin_passes_the_cross_origin_check(client_factory):
    """The page's own fetch() calls carry the tunnel domain as Origin."""
    client = client_factory(base_url=TUNNEL, public_hosts="*.trycloudflare.com")
    response = client.post(
        "/api/auth/login",
        json={"email": "a@b.co", "password": "wrong-password"},
        headers={"x-cr-token": _token(), "origin": TUNNEL},
    )
    # 401 is auth.py saying "wrong password" — the guard let the call through.
    assert response.status_code == 401


def test_wildcard_does_not_leak_to_lookalike_hosts(client_factory):
    client = client_factory(
        base_url="https://evil-trycloudflare.com", public_hosts="*.trycloudflare.com"
    )
    assert client.get("/").status_code == 421


def test_exact_public_host(client_factory):
    client = client_factory(base_url="https://demo.example.com", public_hosts="demo.example.com")
    assert client.get("/").status_code == 200


def test_unrelated_host_still_refused_when_public_hosts_set(client_factory):
    client = client_factory(base_url="https://other.example.com", public_hosts="demo.example.com")
    assert client.get("/").status_code == 421


# --- the session gate keeps holding through a tunnel ------------------------


def test_signup_then_state_through_tunnel(client_factory):
    """The full public-demo path: arrive, create an account, use the app."""
    client = client_factory(base_url=TUNNEL, public_hosts="*.trycloudflare.com")
    headers = {"x-cr-token": _token(), "origin": TUNNEL}

    # Before signing in, the app itself is closed.
    assert client.get("/api/state", headers=headers).status_code == 401

    created = client.post(
        "/api/auth/signup",
        json={"email": "grader@example.com", "password": "long-enough-pw"},
        headers=headers,
    )
    assert created.status_code == 200

    # The session cookie from signup now opens the rest of the API.
    assert client.get("/api/state", headers=headers).status_code == 200
