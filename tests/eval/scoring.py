"""Per-field, binary scoring.

Binary on purpose. A 1-10 quality score from a model judge looks precise and
is not: the same output scored twice lands on different numbers, and nobody can
say what separates a 6 from a 7. "Did it get the domain right — yes or no" is a
question with an answer, and a table of those adds up to something you can act
on.

Every field reports a numerator and a denominator. A field only some fixtures
can exercise — `industry` needs a research recording — is scored over the
fixtures that have one, and the denominator says so rather than the rate being
quietly computed against 30.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# Fields the harness scores, in report order.
FIELDS = (
    "should_research",
    "relationship",
    "company_name",
    "domain",
    "contact_name",
    "industry",
    "news_recency",
    "no_fabricated_claims",
)

# A story older than this is not "recent news" for a meeting brief.
NEWS_MAX_AGE = timedelta(days=550)


@dataclass
class FieldScore:
    hits: int = 0
    total: int = 0

    @property
    def rate(self) -> float:
        return self.hits / self.total if self.total else 0.0

    def render(self) -> str:
        if not self.total:
            return "     n/a"
        return f"{self.hits:>3}/{self.total:<3} ({self.rate:5.0%})"


@dataclass
class Scorecard:
    fields: dict[str, FieldScore] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def record(self, name: str, ok: bool, *, fixture: str = "", detail: str = "") -> None:
        score = self.fields.setdefault(name, FieldScore())
        score.total += 1
        if ok:
            score.hits += 1
        else:
            self.failures.append({"fixture": fixture, "field": name, "detail": detail})


# Legal-form wrappers carry no identifying information. English ones trail the
# name; Vietnamese ones lead it — "Công ty Cổ phần Điện Thủ Đức" is the same
# company as "Điện Thủ Đức", and a scorer that calls that a miss is measuring
# its own normaliser rather than the agent.
LEGAL_SUFFIXES = (" inc", " llc", " ltd", " limited", " corp", " corporation",
                  " co", " gmbh", " jsc", " company", " pte", " plc", " ag", " sa")
LEGAL_PREFIXES = ("cong ty co phan ", "cong ty tnhh ", "cong ty ", "cong ti ",
                  "cty ", "tnhh ", "co phan ", "pt ", "the ")


def _strip_marks(text: str) -> str:
    """Fold diacritics so 'Điện' and 'Dien' compare equal."""
    import unicodedata

    # Vietnamese đ/Đ has no combining form, so it needs handling by hand.
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _norm(text: str | None) -> str:
    """Compare names the way a person would: case, accents and legal form are noise."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", _strip_marks(text or "").lower())
    cleaned = " ".join(cleaned.split())
    for _ in range(3):                      # prefixes stack: "công ty cổ phần"
        for prefix in LEGAL_PREFIXES:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break
        else:
            break
    for suffix in LEGAL_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    return " ".join(cleaned.split())


def _name_matches(actual: str | None, expected: Any) -> bool:
    """``expected`` may be a string, a list of acceptable answers, or None."""
    if expected is None:
        return True                      # fixture does not assert this field
    if isinstance(expected, list):
        return any(_name_matches(actual, option) for option in expected)
    got, want = _norm(actual), _norm(expected)
    if not want:
        return not got                   # expected empty means it must be empty
    if not got:
        return False
    # Exact, once legal form and accents are normalised away. A prefix rule was
    # tried and rejected: it let "Acme Bank" satisfy an expectation of "Acme",
    # which is a different company. Where several answers are genuinely right,
    # the fixture lists them — that judgement belongs with the fixture, not in
    # a matcher trying to guess which extra words are meaningful.
    return got == want


def score_triage(result, expected: dict, *, fixture: str, card: Scorecard) -> None:
    """The five fields triage is responsible for."""
    if "should_research" in expected:
        got = bool(result.should_research)
        card.record("should_research", got == expected["should_research"],
                    fixture=fixture,
                    detail=f"expected {expected['should_research']}, got {got}")

    if expected.get("relationship") is not None:
        want = expected["relationship"]
        options = want if isinstance(want, list) else [want]
        got = result.relationship.value
        card.record("relationship", got in options, fixture=fixture,
                    detail=f"expected {'|'.join(options)}, got {got}")

    if "company_name" in expected:
        card.record("company_name", _name_matches(result.company_name, expected["company_name"]),
                    fixture=fixture,
                    detail=f"expected {expected['company_name']!r}, got {result.company_name!r}")

    if "domain" in expected:
        want = expected["domain"]
        got = (result.company_domain or "").strip().lower().lstrip("@")
        options = [(w or "").lower() for w in (want if isinstance(want, list) else [want])]
        card.record("domain", got in options, fixture=fixture,
                    detail=f"expected {'|'.join(options) or '(empty)'}, got {got or '(empty)'}")

    if "contact_name" in expected:
        card.record("contact_name", _name_matches(result.contact_name, expected["contact_name"]),
                    fixture=fixture,
                    detail=f"expected {expected['contact_name']!r}, got {result.contact_name!r}")


def score_research(profile, expected: dict, *, fixture: str, card: Scorecard,
                   now: datetime | None = None) -> None:
    """The three fields that need a research profile to judge."""
    now = now or datetime.now(timezone.utc)

    if expected.get("industry") is not None:
        card.record("industry", _industry_matches(profile.industry, expected["industry"]),
                    fixture=fixture,
                    detail=f"expected ~{expected['industry']!r}, got {profile.industry!r}")

    if expected.get("news_recency") is not None:
        ok, why = _news_is_recent(profile, now)
        card.record("news_recency", ok if expected["news_recency"] else not ok,
                    fixture=fixture, detail=why)

    if expected.get("no_fabricated_claims") is not None:
        ok, why = _nothing_fabricated(profile, expected)
        card.record("no_fabricated_claims", ok, fixture=fixture, detail=why)


def _industry_matches(actual: str | None, expected: Any) -> bool:
    """Industry is prose, so match on keywords the fixture nominates."""
    got = (actual or "").lower()
    if not got:
        return False
    keywords = expected if isinstance(expected, list) else [expected]
    return any(str(k).lower() in got for k in keywords)


def _news_is_recent(profile, now: datetime) -> tuple[bool, str]:
    if not profile.news:
        return False, "no news items"
    dated = 0
    for item in profile.news:
        when = _parse_date(item.published)
        if when is None:
            continue
        dated += 1
        if when > now + timedelta(days=2):
            return False, f"news dated in the future: {item.published}"
        if now - when > NEWS_MAX_AGE:
            return False, f"news older than {NEWS_MAX_AGE.days} days: {item.published}"
    if not dated:
        return False, "no news item carries a parseable date"
    return True, f"{dated} dated item(s), all recent"


def _parse_date(raw: str) -> datetime | None:
    text = (raw or "").strip()
    for pattern in ("%Y-%m-%d", "%Y-%m", "%Y", "%d %B %Y", "%B %Y"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _nothing_fabricated(profile, expected: dict) -> tuple[bool, str]:
    """Two ways a profile lies: unsourced claims, and known-wrong values.

    The second matters most on the hard fixtures — a company with no website
    invites the model to fill the gap from a similarly-named firm, and the
    fixture names what must not appear.
    """
    banned = [b.lower() for b in expected.get("must_not_contain", [])]
    if banned:
        haystack = " ".join([
            profile.name or "", profile.one_liner or "", profile.description or "",
            profile.industry or "", profile.hq_location or "",
            profile.size_estimate or "", profile.founded or "",
            " ".join(profile.products or []),
        ]).lower()
        for phrase in banned:
            if phrase in haystack:
                return False, f"contains a value the fixture forbids: {phrase!r}"

    # A claim with no source is not a lie, but a profile that is mostly
    # unsourced is not evidence either.
    substantive = [v for v in (profile.one_liner, profile.industry, profile.hq_location,
                               profile.size_estimate, profile.founded) if v]
    if substantive:
        sourced = sum(
            1 for name in ("one_liner", "industry", "hq_location", "size_estimate", "founded")
            if getattr(profile, name, "") and profile.source_for(name)
        )
        if sourced == 0:
            return False, f"{len(substantive)} claim(s), none attributed to a page"
    return True, "no forbidden value; at least one claim sourced"
