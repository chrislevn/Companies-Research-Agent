"""Clicking things in a real browser.

Everything here exists because a bug got through every other check. The
settings tabs looked correct in static analysis (handler present, ids unique,
selectors resolve) and correct in screenshots (deep links rendered different
sections). They were still broken: a pre-existing `$$(".tab")` handler claimed
every tab on the page and, being wired last, silently replaced the newer ones.
The active underline still moved, so it looked like it worked while every
section showed the same content.

Only a real click finds that. These tests are skipped when Playwright or a
browser is unavailable, so the suite still runs anywhere.
"""

from __future__ import annotations

import pathlib
import socket
import subprocess
import time

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

REPO = __import__("pathlib").Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app():
    """The real server, on a scratch database so nothing touches real state."""
    import os
    import tempfile

    tmp = tempfile.mkdtemp()
    port = _free_port()
    env = {
        **os.environ,
        "DB_PATH": f"{tmp}/ui.db", "PROMPTS_DIR": f"{tmp}/prompts",
        "ORG_PROFILE_FILE": f"{tmp}/profile.json", "DELIVERY_DIR": f"{tmp}/briefs",
        "METRICS_ENABLED": "false", "WATCH_ENABLED": "false",
        # These tests drive the real login wall (they sign up through the form),
        # so pin the bypass off regardless of the developer's .env — otherwise
        # AUTH_DISABLED=true auto-signs-in and #view-auth never appears.
        "AUTH_DISABLED": "false",
        "PYTHONPATH": str(REPO / "src"),
    }
    proc = subprocess.Popen(
        ["python", "-m", "companies_research", "ui", "--port", str(port), "--no-browser"],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("the web app did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def page(app):
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            pytest.skip("no Chrome available")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(app, wait_until="networkidle")
        # The login wall is real, and the scratch database starts with no
        # accounts — so create one through the actual form, exactly as a first
        # visitor would, before any test tries to reach the app behind it.
        page.wait_for_selector("#view-auth", state="visible")
        page.click('.auth-tab[data-auth="signup"]')
        page.fill("#auth-email", "ui-tests@example.com")
        page.fill("#auth-password", "ui-tests-password")
        page.click("#auth-submit")
        page.wait_for_selector("#view-auth", state="hidden")
        page.errors = errors        # type: ignore[attr-defined]
        yield page
        browser.close()


def open_settings(page):
    """Reach Settings by URL, not by the topbar.

    On a scratch database first-run setup is incomplete, and the app correctly
    hides the nav until it is done — there is nowhere else to go yet. The tab
    clicks below are what is under test, so navigate past that deliberately.
    """
    page.goto(page.url.split("#")[0] + "#settings", wait_until="networkidle")
    page.wait_for_timeout(250)


SECTIONS = [
    ("Connection", "connection"),
    ("Your company", "company"),
    ("How it works", "behaviour"),
    ("Permissions", "permissions"),
    ("Prompts", "prompts"),
    ("Your data", "data"),
]


@pytest.mark.parametrize("label,group", SECTIONS)
def test_clicking_a_settings_tab_shows_that_section(page, label, group):
    """The bug: every tab left the first section showing."""
    open_settings(page)
    page.click(f'#settings-nav .tab:has-text("{label}")')
    page.wait_for_timeout(200)
    visible = page.eval_on_selector_all(
        ".settings-group", "els => els.filter(e => !e.hidden).map(e => e.dataset.group)")
    assert visible == [group], f"clicking {label!r} showed {visible}"


def test_exactly_one_section_is_ever_visible(page):
    open_settings(page)
    for label, _ in SECTIONS:
        page.click(f'#settings-nav .tab:has-text("{label}")')
        page.wait_for_timeout(150)
        count = page.eval_on_selector_all(
            ".settings-group", "els => els.filter(e => !e.hidden).length")
        assert count == 1, f"{count} sections visible after clicking {label!r}"


def test_the_prompt_switcher_changes_the_text(page):
    """Broken by the same handler collision, so it is worth its own test."""
    open_settings(page)
    page.click('#settings-nav .tab:has-text("Prompts")')
    page.wait_for_timeout(250)
    triage = page.input_value("#prompt-text")
    page.click('#prompt-tabs .tab:has-text("Researching")')
    page.wait_for_timeout(250)
    research = page.input_value("#prompt-text")
    assert triage and research and triage != research


def test_the_mailbox_tabs_still_work(page):
    """The handler those originally belonged to must survive being scoped."""
    page.goto(page.url.split("#")[0] + "#mailbox", wait_until="networkidle")
    page.wait_for_timeout(300)
    page.click('#mailbox-tabs .tab:has-text("Sign in with Google")')
    page.wait_for_timeout(250)
    visible = page.eval_on_selector_all(
        ".tabpanel", "els => els.filter(e => !e.hidden).map(e => e.dataset.panel)")
    assert visible == ["google"]


def test_no_class_is_claimed_by_more_than_one_global_handler():
    """Guard the shape of the bug, not just this instance.

    A bare `$$(".thing")` that assigns `.onclick` will silently replace any
    handler assigned earlier to the same elements. Scoping to a container makes
    the collision impossible.
    """
    import re

    js = (REPO / "src/companies_research/webapp/static/app.js").read_text()
    bare = re.findall(r'\$\$\("\.([\w-]+)"\)\.forEach\(\s*\(?\w+\)?\s*=>\s*\{?[^}]*\.onclick', js)
    assert not bare, f"unscoped onclick assignment to every .{bare} on the page"


def test_the_page_raises_no_errors(page):
    assert page.errors == [], page.errors


def test_review_renders_are_serialised_by_generation():
    """Two overlapping renders must not both write to the list.

    The list is emptied before the fetch and filled after it, so without a
    guard a tab click landing during the initial load leaves every brief on
    screen twice — which reads as a data bug and is not one.
    """
    js = (pathlib.Path(__file__).resolve().parents[1]
          / "src/companies_research/webapp/static/app.js").read_text()
    body = js[js.index("async function renderReview()"):]
    body = body[:body.index("\nfunction ") if "\nfunction " in body else len(body)]
    assert "++reviewGeneration" in body, "renderReview takes no generation ticket"
    assert body.count("if (generation !== reviewGeneration) return;") >= 2, (
        "a stale render can still write to the list"
    )


def test_the_google_signin_button_is_on_the_auth_card():
    """Wired by unique id (not a bare class), so it does not trip the
    unscoped-handler guard, and reuses the existing signin.google string."""
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "src/companies_research/webapp/static/index.html").read_text()
    auth_view = html[html.index('id="view-auth"'):html.index('id="view-signin"')]
    assert 'id="auth-google"' in auth_view, "no Google button on the auth card"
    assert 'data-i18n="signin.google"' in auth_view

    js = (root / "src/companies_research/webapp/static/app.js").read_text()
    # selected and wired by unique id, never by a shared class
    assert '$("#auth-google")' in js, "the Google button is not selected by id"
    assert "googleButton.onclick" in js, "the Google button has no handler"
