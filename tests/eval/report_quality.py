"""`./start.sh report-quality` — is the brief good enough to walk into a meeting with?

The fixture eval next door asks whether triage is *correct*. This asks whether
the finished report is *useful*, against the six criteria on the ProtonX brief,
and it runs against real companies with live websites rather than the `.example`
domains the offline fixtures use.

That difference is the point. Unresolvable domains are the right way to test
what the agent does when it can find nothing; they cannot tell you whether a
brief is worth reading. So this harness takes six real enquiries, runs the whole
pipeline — triage, then research over the live web — and scores what comes out.

The six criteria, and what each is actually measured from:

| criterion            | measured as                                              |
|----------------------|----------------------------------------------------------|
| Độ đầy đủ thông tin  | six required fields present: website, industry, products, |
|                      | size, news, contact                                       |
| Độ chính xác         | share of substantive claims carrying a source URL, plus   |
|                      | whether the domain matches the company's official site    |
| Thời gian tạo báo cáo| wall clock, triage + research                             |
| Số nguồn tham khảo   | unique source URLs behind the claims                      |
| Độ mới của dữ liệu   | age of the newest dated news item                         |
| Mức độ sẵn sàng      | composite: enough fields, sourced, and meeting prep       |

Accuracy is deliberately *not* "did a model judge think it was right". A judge
scoring a judge is two guesses stacked. Attribution is checkable: either a claim
names the page it came from or it does not, and a profile that is mostly
unsourced is not evidence however confident it sounds.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

RESULTS = pathlib.Path("report_quality_results.json")

# The brief asks for news "trong 30 ngày gần nhất". Measured, not assumed: the
# figure reported is the real age of the newest dated item, and the pass mark is
# separate from it so a miss is visible rather than rounded away.
FRESH_DAYS = 30

# What "đầy đủ" means, field by field. Contact comes from triage rather than
# research — it is in the email, not on the website — so the two stages are
# scored together, as the person reading the brief receives them.
REQUIRED = ("website", "industry", "products", "size", "news", "contact")


@dataclass
class Enquiry:
    """One inbound email from the brief's evaluation table."""
    id: str
    contact: str
    company: str
    domain: str
    body: str
    expectation: str


ENQUIRIES: list[Enquiry] = [
    Enquiry("fpt", "Nguyễn Văn A", "FPT Software", "fptsoftware.com",
            "Chúng tôi muốn tìm hiểu giải pháp AI cho doanh nghiệp.",
            "Research the company, aggregate profile, products and news."),
    Enquiry("vinamilk", "Trần Minh Đức", "Vinamilk", "vinamilk.com.vn",
            "Mong muốn trao đổi về ứng dụng AI trong quản lý tài liệu.",
            "Briefing on industry, size and related AI projects."),
    Enquiry("samsung", "Sarah Lee", "Samsung Electronics Vietnam", "samsung.com/vn",
            "Xin hẹn buổi giới thiệu giải pháp vào tuần tới.",
            "Research, cross-check the calendar, prepare a meeting brief."),
    Enquiry("shopee", "David Chen", "Shopee Vietnam", "shopee.vn",
            "Chúng tôi quan tâm đến giải pháp OCR.",
            "Company info, latest news, potential need, customer profile."),
    Enquiry("viettel", "Nguyễn Hoàng Anh", "Viettel Solutions", "viettelsolutions.vn",
            "Đính kèm hồ sơ năng lực để hai bên tham khảo.",
            "Read the attachment, combine with website data, summarise."),
    Enquiry("bosch", "Emily Wong", "Bosch Global Software Technologies Vietnam",
            "bosch.com/vn",
            "Chúng tôi muốn trao đổi về giải pháp AI Automation.",
            "Research, aggregate, store to the knowledge base."),
]


@dataclass
class Report:
    enquiry: Enquiry
    seconds: float = 0.0
    completeness: float = 0.0
    present: dict[str, bool] = field(default_factory=dict)
    sourced_share: float = 0.0
    domain_matches: bool = False
    sources: int = 0
    news_age_days: int | None = None
    ready: bool = False
    confidence: float = 0.0
    error: str = ""

    @property
    def fresh(self) -> bool:
        return self.news_age_days is not None and self.news_age_days <= FRESH_DAYS


def _email(enquiry: Enquiry) -> Any:
    from companies_research.models import EmailAddress, EmailMessage

    handle = enquiry.contact.split()[-1].lower()
    return EmailMessage(
        message_id=enquiry.id,
        thread_id=enquiry.id,
        subject=f"Enquiry — {enquiry.company}",
        sender=EmailAddress(name=enquiry.contact,
                            email=f"{handle}@{enquiry.domain.split('/')[0]}"),
        to=[EmailAddress(name="Sales", email="sales@example.com")],
        body_text=(
            f"Kính gửi anh/chị,\n\n{enquiry.body}\n\n"
            f"Website: https://{enquiry.domain}\n\n"
            f"Trân trọng,\n{enquiry.contact}\n{enquiry.company}"
        ),
        snippet=enquiry.body[:100],
        received_at=None,
    )


def _parse_date(raw: str) -> datetime | None:
    for pattern in ("%Y-%m-%d", "%Y-%m", "%Y", "%d %B %Y", "%B %Y"):
        try:
            return datetime.strptime((raw or "").strip(), pattern).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _score(enquiry: Enquiry, triage: Any, profile: Any, seconds: float) -> Report:
    report = Report(enquiry=enquiry, seconds=round(seconds, 1))
    if profile is None:
        report.error = "no profile produced"
        return report

    present = {
        "website": bool(profile.domain),
        "industry": bool(profile.industry),
        "products": bool(profile.products),
        "size": bool(profile.size_estimate),
        "news": bool(profile.news),
        "contact": bool(triage is not None and triage.contact_name),
    }
    report.present = present
    report.completeness = sum(present.values()) / len(REQUIRED)

    # Accuracy, as attribution rather than opinion.
    attributable = ("one_liner", "industry", "hq_location", "size_estimate", "founded")
    claimed = [n for n in attributable if getattr(profile, n, "")]
    sourced = [n for n in claimed if profile.source_for(n)]
    report.sourced_share = len(sourced) / len(claimed) if claimed else 0.0

    official = enquiry.domain.split("/")[0].lower().removeprefix("www.")
    got = (profile.domain or "").lower().removeprefix("www.")
    report.domain_matches = bool(got) and (got in official or official in got)

    urls = {s.url for s in profile.field_sources if getattr(s, "url", "")}
    urls |= {u for u in (profile.sources or []) if u}
    urls |= {n.url for n in profile.news if n.url}
    report.sources = len(urls)

    dated = [d for d in (_parse_date(n.published) for n in profile.news) if d]
    if dated:
        report.news_age_days = (datetime.now(timezone.utc) - max(dated)).days

    report.confidence = profile.confidence
    # Ready = you could walk into the meeting on this. Every clause is a thing
    # the person in the room would notice was missing.
    report.ready = (
        report.completeness >= 0.8
        and report.sourced_share >= 0.5
        and report.sources >= 3
        and bool(profile.meeting_prep)
    )
    return report


def _run_one(enquiry: Enquiry) -> Report:
    from companies_research.agents.backends import build_backend
    from companies_research.agents.triage import TriageAgent
    from companies_research.research import build_researcher

    started = time.monotonic()
    try:
        agent = TriageAgent(backend=build_backend())
        agent.batch_size = 1
        triaged = agent.triage([_email(enquiry)])
        triage = triaged[0] if triaged else None

        researcher = build_researcher()
        outcome = researcher.research(
            company=(triage.company_name if triage else "") or enquiry.company,
            domain=(triage.company_domain if triage else "") or enquiry.domain.split("/")[0],
            context=enquiry.body,
        )
    except Exception as exc:
        return Report(enquiry=enquiry, seconds=round(time.monotonic() - started, 1),
                      error=f"{type(exc).__name__}: {exc}")

    seconds = time.monotonic() - started
    if not outcome.ok or outcome.profile is None:
        report = Report(enquiry=enquiry, seconds=round(seconds, 1),
                        error=outcome.error or "research produced no profile")
        return report
    return _score(enquiry, triage, outcome.profile, seconds)


def run(*, only: str | None = None) -> dict[str, Any]:
    enquiries = [e for e in ENQUIRIES if not only or e.id == only]
    if not enquiries:
        print(f"No enquiry matched {only!r}. Known: "
              f"{', '.join(e.id for e in ENQUIRIES)}")
        return {}

    print(f"\nReport quality — {len(enquiries)} real compan"
          f"{'y' if len(enquiries) == 1 else 'ies'}, live web.")
    print("Unlike the offline fixtures, these domains resolve.\n")

    reports: list[Report] = []
    for enquiry in enquiries:
        print(f"── {enquiry.company} ({enquiry.domain})")
        report = _run_one(enquiry)
        if report.error:
            print(f"   FAILED — {report.error}")
        else:
            print(f"   {report.completeness:.0%} complete · {report.sources} source(s) · "
                  f"{report.seconds:.0f}s · "
                  f"news {report.news_age_days if report.news_age_days is not None else '—'}d")
        reports.append(report)

    payload = _report(reports)
    _print(reports, payload)
    RESULTS.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote {RESULTS}")
    return payload


def _report(reports: list[Report]) -> dict:
    good = [r for r in reports if not r.error]
    fresh = [r for r in good if r.news_age_days is not None]

    def mean(values):
        values = list(values)
        return round(statistics.mean(values), 4) if values else 0.0

    return {
        "companies": len(reports),
        "succeeded": len(good),
        "criteria": {
            "completeness": mean(r.completeness for r in good),
            "sourced_share": mean(r.sourced_share for r in good),
            "domain_match_rate": mean(1.0 if r.domain_matches else 0.0 for r in good),
            "seconds_mean": mean(r.seconds for r in good),
            "sources_mean": mean(r.sources for r in good),
            "news_age_days_median": (
                round(statistics.median(r.news_age_days for r in fresh)) if fresh else None
            ),
            "fresh_within_30d_rate": mean(1.0 if r.fresh else 0.0 for r in good),
            "ready_rate": mean(1.0 if r.ready else 0.0 for r in good),
        },
        "per_company": [
            {"id": r.enquiry.id, "company": r.enquiry.company,
             "completeness": round(r.completeness, 4), "present": r.present,
             "sourced_share": round(r.sourced_share, 4),
             "domain_matches": r.domain_matches, "sources": r.sources,
             "news_age_days": r.news_age_days, "seconds": r.seconds,
             "confidence": r.confidence, "ready": r.ready, "error": r.error}
            for r in reports
        ],
    }


def _print(reports: list[Report], payload: dict) -> None:
    c = payload["criteria"]
    print(f"\n{'=' * 82}")
    print("REPORT QUALITY")
    print("=" * 82)
    print(f"{'tiêu chí':<26}{'kết quả':<40}")
    print("-" * 82)
    missing = sorted({k for r in reports if not r.error
                      for k, v in r.present.items() if not v})
    print(f"{'Độ đầy đủ thông tin':<26}"
          f"{c['completeness']:.0%} of "
          f"{'/'.join(REQUIRED)}")
    if missing:
        print(f"{'':<26}most often missing: {', '.join(missing)}")
    print(f"{'Độ chính xác':<26}{c['sourced_share']:.0%} of claims carry a source URL; "
          f"domain matches {c['domain_match_rate']:.0%}")
    print(f"{'Thời gian tạo báo cáo':<26}{c['seconds_mean']:.0f} giây (mean)")
    print(f"{'Số nguồn tham khảo':<26}{c['sources_mean']:.1f} nguồn (mean)")
    age = c["news_age_days_median"]
    print(f"{'Độ mới của dữ liệu':<26}"
          f"{'median ' + str(age) + ' ngày' if age is not None else 'no dated news'}"
          f" · {c['fresh_within_30d_rate']:.0%} within {FRESH_DAYS}d")
    print(f"{'Mức độ sẵn sàng':<26}{c['ready_rate']:.0%} ready to walk into the meeting")

    print(f"\n{'-' * 82}")
    print(f"{'company':<34}{'complete':>10}{'sourced':>9}{'src':>6}{'news':>7}{'ready':>7}")
    print("-" * 82)
    for r in reports:
        if r.error:
            print(f"{r.enquiry.company[:33]:<34}{'FAILED — ' + r.error[:30]:>39}")
            continue
        age = f"{r.news_age_days}d" if r.news_age_days is not None else "—"
        print(f"{r.enquiry.company[:33]:<34}{r.completeness:>9.0%}"
              f"{r.sourced_share:>9.0%}{r.sources:>6}{age:>7}"
              f"{('yes' if r.ready else 'no'):>7}")
