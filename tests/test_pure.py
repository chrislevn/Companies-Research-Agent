"""The two pure, load-bearing functions that had no tests.

`_skip_reason` decides what never reaches a model — it is the cheapest and
highest-volume decision in the system. `json_schema_for` decides whether a
request is even valid. Both are pure, both are exercised on every scan, and
until now neither had a single test.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, ConfigDict, Field

from companies_research.models import EmailAddress, EmailMessage, Relationship, TriageResult
from companies_research.pipeline import _skip_reason
from companies_research.providers.base import Account
from companies_research.schema_utils import UnsupportedSchema, json_schema_for


def _msg(**over) -> EmailMessage:
    base = dict(
        message_id="m1", provider="gmail", account_id="acct",
        sender=EmailAddress(name="Sarah", email="sarah@acme.com"),
        to=[EmailAddress(email="owner@example.com")],
        subject="Hello", body_text="Hi", received_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return EmailMessage(**base)


ACCOUNT = Account(account_id="acct", provider="gmail", email="owner@example.com")


class _Store:
    """Minimal stand-in — the filter only asks these two questions."""

    def __init__(self, processed=(), known=()):
        self._processed, self._known = set(processed), set(known)

    def is_processed(self, message):
        return message.uid in self._processed

    def is_known_sender(self, email, *, user_id="default", match_domain=True):
        email = (email or "").lower()
        if email in self._known:
            return True
        domain = email.split("@")[-1] if "@" in email else ""
        return bool(match_domain and domain and domain in self._known)


def _skip(message, store=None, **kw):
    options = {"include_known_senders": False, "reprocess": False}
    options.update(kw)
    return _skip_reason(message, ACCOUNT, store or _Store(), **options)


# --- the filter ------------------------------------------------------------


def test_a_normal_new_sender_passes():
    assert _skip(_msg()) is None


def test_mail_without_a_sender_is_skipped():
    assert _skip(_msg(sender=EmailAddress(email=""))) == "no sender address"


def test_your_own_mail_is_skipped():
    assert _skip(_msg(sender=EmailAddress(email="owner@example.com"))) == "sent by you"


def test_automated_mail_is_skipped():
    """The rule that removes almost all real inbox volume."""
    assert _skip(_msg(is_automated=True)) == "automated or bulk mail"


def test_a_known_sender_is_skipped():
    store = _Store(known={"sarah@acme.com"})
    assert _skip(_msg(), store) == "known sender"


def test_include_known_overrides_that():
    store = _Store(known={"sarah@acme.com"})
    assert _skip(_msg(), store, include_known_senders=True) is None


def test_a_colleague_of_a_known_sender_is_skipped_by_domain():
    """Two people from one company are one relationship, not two."""
    store = _Store(known={"acme.com"})
    assert _skip(_msg(sender=EmailAddress(email="bob@acme.com")), store) == "known sender"


def test_already_processed_is_skipped():
    store = _Store(processed={"gmail:acct:m1"})
    assert _skip(_msg(), store) == "already processed"


def test_reprocess_overrides_already_processed():
    store = _Store(processed={"gmail:acct:m1"})
    assert _skip(_msg(), store, reprocess=True) is None


def test_ordering_cheap_checks_run_before_expensive_ones():
    """A message that trips several rules reports the cheapest one.

    Not cosmetic: the later checks hit the database, so an early return is the
    difference between one query and thousands on a large scan.
    """
    store = _Store(processed={"gmail:acct:m1"})
    assert _skip(_msg(sender=EmailAddress(email="owner@example.com")), store) == "sent by you"


def test_a_consumer_domain_is_never_treated_as_your_own():
    """A solo founder on Gmail must still see Gmail-based leads."""
    gmail_account = Account(account_id="a", provider="gmail", email="me@gmail.com")
    reason = _skip_reason(
        _msg(sender=EmailAddress(email="someone@gmail.com")), gmail_account, _Store(),
        include_known_senders=False, reprocess=False,
    )
    assert reason != "ignored/own domain"


# --- the schema ------------------------------------------------------------


def test_every_object_forbids_extra_properties():
    """The API requires it, and it is what stops a body reaching an audit row."""
    schema = json_schema_for(TriageResult)
    assert schema["additionalProperties"] is False


def test_every_property_is_required():
    """Structured outputs will not fill a field it was not told to fill."""
    schema = json_schema_for(TriageResult)
    assert set(schema["required"]) == set(schema["properties"])


def test_unsupported_keywords_are_stripped():
    class Constrained(BaseModel):
        model_config = ConfigDict(extra="forbid")
        score: float = Field(ge=0.0, le=1.0)
        name: str = Field(min_length=2, max_length=8, pattern="^[a-z]+$")

    schema = json_schema_for(Constrained)
    text = str(schema)
    for keyword in ("minimum", "maximum", "minLength", "maxLength", "pattern"):
        assert keyword not in text


def test_constraints_are_still_enforced_client_side():
    """Stripping them from the schema must not stop Pydantic checking them."""
    with pytest.raises(Exception):
        TriageResult(message_id="m", is_business_contact=True,
                     relationship=Relationship.UNKNOWN, should_research=False,
                     confidence=1.5)


def test_a_property_named_like_a_keyword_survives():
    """`properties` is a namespace, so a field called `pattern` must not be stripped."""
    class Odd(BaseModel):
        model_config = ConfigDict(extra="forbid")
        pattern: str = ""
        maximum: int = 0

    schema = json_schema_for(Odd)
    assert set(schema["properties"]) == {"pattern", "maximum"}


def test_open_ended_objects_are_rejected_with_a_useful_message():
    class Bad(BaseModel):
        mapping: dict[str, str]

    with pytest.raises(UnsupportedSchema) as exc:
        json_schema_for(Bad)
    assert "list of" in str(exc.value), "the error should name the fix"


def test_nested_models_are_sanitised_too():
    from companies_research.models import CompanyProfile

    schema = json_schema_for(CompanyProfile)
    for definition in schema.get("$defs", {}).values():
        if definition.get("type") == "object" and "properties" in definition:
            assert definition["additionalProperties"] is False
