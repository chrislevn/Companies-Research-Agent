"""Your own company profile, and the configuration surface around it."""

from __future__ import annotations

import pytest

from companies_research import org
from companies_research.models import OrgProfile


@pytest.fixture
def profile():
    return OrgProfile(
        name="Acme Freight", domain="acme.example",
        what_we_do="Sea and air freight forwarding out of Vietnam.",
        ideal_customer="Manufacturers shipping 20+ containers a year to Europe.",
        target_industries=["manufacturing"], not_interested_in=["recruitment agencies"],
        research_criteria="Always check annual shipping volume.",
    )


def test_an_empty_profile_adds_nothing_to_the_prompt():
    """No profile must mean no change in behaviour, not a half-empty section."""
    assert org.render_for_triage(OrgProfile()) == ""
    assert org.render_for_research(OrgProfile()) == ""


def test_the_profile_reaches_the_triage_prompt(profile):
    rendered = org.render_for_triage(profile)
    assert "Acme Freight" in rendered
    assert "Manufacturers shipping" in rendered
    assert "recruitment agencies" in rendered


def test_triage_context_is_marked_trusted(profile):
    """The model must be able to tell the operator's words from a stranger's."""
    rendered = org.render_for_triage(profile)
    assert "trusted context" in rendered
    assert "untrusted-" in rendered, "it must name the thing it contrasts with"


def test_relevance_and_legitimacy_stay_separate(profile):
    """An off-target company is still recorded honestly, just not researched."""
    rendered = org.render_for_triage(profile)
    assert "is_business_contact" in rendered
    assert "should_research" in rendered


def test_research_criteria_reach_the_research_prompt(profile):
    assert "annual shipping volume" in org.render_for_research(profile)


def test_criteria_are_not_a_licence_to_guess(profile):
    """Standing questions must not become pressure to fabricate."""
    rendered = org.render_for_research(profile)
    assert "not a licence to guess" in rendered
    assert "leave the field empty" in rendered


def test_credentials_pasted_into_a_profile_are_scrubbed():
    leaky = OrgProfile(name="Acme", research_criteria="Use key sk-ant-api03-" + "C" * 40)
    assert "sk-ant-api03" not in org.render_for_research(leaky)


def test_save_and_load_round_trip(profile, tmp_path, monkeypatch):
    monkeypatch.setenv("ORG_PROFILE_FILE", str(tmp_path / "profile.json"))
    from companies_research.config import reload_settings

    reload_settings()
    org.save(profile)
    assert org.load().name == "Acme Freight"
    assert org.load().research_criteria == "Always check annual shipping volume."


def test_the_profile_file_is_not_world_readable(profile, tmp_path, monkeypatch):
    """It describes your business and your customers."""
    path = tmp_path / "profile.json"
    monkeypatch.setenv("ORG_PROFILE_FILE", str(path))
    from companies_research.config import reload_settings

    reload_settings()
    org.save(profile)
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_a_corrupt_profile_does_not_stop_the_agent(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    path.write_text("{ this is not json")
    monkeypatch.setenv("ORG_PROFILE_FILE", str(path))
    from companies_research.config import reload_settings

    reload_settings()
    assert org.load().configured is False, "it should degrade, not raise"
