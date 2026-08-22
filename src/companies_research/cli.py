"""Command line entry point.

With no arguments this opens the web interface, which is the only thing most
people need:

    python -m companies_research

Solo (no config file needed):

    python -m companies_research auth
    python -m companies_research seed
    python -m companies_research scan --since 1d

Team / enterprise (accounts.json):

    python -m companies_research accounts
    python -m companies_research scan --account sales-m365
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path as _Path
from datetime import datetime, timedelta, timezone

ROOT_DIR = _Path(__file__).resolve().parents[2]

from .accounts import AccountsError, accounts_file, load_accounts
from .pipeline import LAST_SCAN_KEY, ScanReport, new_mail_since, scan, seed_known_senders
from .providers import ProviderError, available_providers, build_provider
from .store import Store

DURATION = re.compile(r"^(\d+)\s*([hdwm])$", re.IGNORECASE)
_UNITS = {"h": "hours", "d": "days", "w": "weeks"}


def parse_duration(value: str) -> timedelta:
    match = DURATION.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid duration {value!r}; use forms like 12h, 1d, 2w, 6m"
        )
    amount, unit = int(match.group(1)), match.group(2).lower()
    if unit == "m":  # months, approximated
        return timedelta(days=30 * amount)
    return timedelta(**{_UNITS[unit]: amount})


# Third-party loggers that say a great deal and mean very little. At -v they
# bury our own DEBUG lines — one line per HTTP request, per message fetched —
# which is exactly when those lines matter most.
NOISY_LOGGERS = (
    "googleapiclient",
    "googleapiclient.discovery_cache",
    "google_auth_httplib2",
    "google.auth",
    "urllib3",
    "httpx",
    "httpcore",
    "msal",
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Our own logger follows -v; the libraries stay at WARNING either way.
    logging.getLogger("companies_research").setLevel(
        logging.DEBUG if verbose else logging.INFO
    )
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------


def cmd_accounts(args: argparse.Namespace) -> int:
    accounts = load_accounts(include_disabled=True)
    source = accounts_file()
    print(f"Config: {source or 'none (solo mode — one Gmail account from .env)'}")
    print(f"Providers available: {', '.join(available_providers())}\n")

    failures = 0
    for account in accounts:
        state = "" if account.enabled else "  (disabled)"
        print(f"{account.describe()}{state}")
        if not args.check or not account.enabled:
            continue
        try:
            with build_provider(account) as provider:
                profile = provider.verify()
            print(f"    ✓ reachable as {profile.email}")
        except (ProviderError, Exception) as exc:  # noqa: B014 - report, never crash
            failures += 1
            print(f"    ✗ {type(exc).__name__}: {exc}")
    return 1 if failures else 0


def cmd_auth(args: argparse.Namespace) -> int:
    """Run each account's interactive auth flow and confirm it works."""
    accounts = load_accounts()
    if args.account:
        accounts = [a for a in accounts if a.account_id in set(args.account)]
    if not accounts:
        print("No accounts to authorise.", file=sys.stderr)
        return 1

    failed = 0
    for account in accounts:
        print(f"\n→ {account.describe()}")
        try:
            with build_provider(account) as provider:
                profile = provider.verify()
        except Exception as exc:
            failed += 1
            print(f"  ✗ {exc}", file=sys.stderr)
            continue
        total = f", {profile.total_messages} messages" if profile.total_messages else ""
        print(f"  ✓ authorised as {profile.email}{total}")
        if account.email and profile.email.lower() != account.email.lower():
            print(f"  ! config says {account.email}; the token is for {profile.email}")
    return 1 if failed else 0


def _say(message: str) -> None:
    """Headline progress for the terminal.

    Sent to stderr so that `--json` on stdout stays machine-readable when the
    two are piped apart.
    """
    print(f"→ {message}", file=sys.stderr, flush=True)


def cmd_seed(args: argparse.Namespace) -> int:
    count = seed_known_senders(since=args.since, account_ids=args.account, progress=_say)
    print(f"Seeded {count} records; {Store().sender_count()} unique senders now known.")
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    """Assemble and render a brief for one company."""
    from .briefs import generate, to_html, to_markdown

    store = Store()
    brief = generate(domain=args.domain, store=store,
                     refresh_calendar=not args.no_calendar)
    if brief is None:
        print(f"No triaged lead found for {args.domain!r}. Run a scan first.")
        return 1

    if not args.no_save:
        brief_id = store.save_brief(brief)
        print(f"(saved as {brief_id})\n", file=sys.stderr)

    if args.json:
        print(brief.model_dump_json(indent=2))
    elif args.html:
        print(to_html(brief))
    else:
        print(to_markdown(brief))
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    """Look for upcoming meetings with one company."""
    from .calendars import look_up

    outcome = look_up(
        domain=args.domain, company=args.name, lookahead_days=args.days
    )

    if args.json:
        print(json.dumps(
            {
                "domain": args.domain,
                "checked": outcome.checked,
                "reason": outcome.reason,
                "events_scanned": outcome.events_scanned,
                "lookahead_days": outcome.lookahead_days,
                "meetings": [m.model_dump(mode="json") for m in outcome.meetings],
            },
            ensure_ascii=False, indent=2,
        ))
        return 0

    print(f"\n{args.domain or args.name} — {outcome.summary()}")
    if not outcome.checked:
        # Not an error: a brief still renders, it just says nobody looked.
        print(f"  (scanned nothing; {outcome.reason})")
        return 0
    print(f"  scanned {outcome.events_scanned} event(s) in the next "
          f"{outcome.lookahead_days} day(s)")
    for meeting in outcome.meetings:
        print(f"\n  {meeting.starts_at:%a %d %b %Y %H:%M}  {meeting.title or '(no title)'}")
        print(f"    matched on {meeting.matched_on.replace('_', ' ')} "
              f"(confidence {meeting.confidence:.2f})")
        if meeting.attendees:
            shown = ", ".join(meeting.attendees[:5])
            more = f" +{len(meeting.attendees) - 5} more" if len(meeting.attendees) > 5 else ""
            print(f"    attendees: {shown}{more}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Score the agent against recorded fixtures. Offline unless --record."""
    import sys as _sys

    root = ROOT_DIR / "tests"
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    from eval.run import run

    report = run(live=args.record, only=args.only)
    if not report:
        return 1
    # A false positive on the negative class is the failure worth failing on.
    return 1 if report["negative_class"]["false_positives"] else 0


def cmd_tools(args: argparse.Namespace) -> int:
    """Show what the agent is permitted to do, and what it has actually done."""
    from . import tools as harness
    from .config import ALL_TOOL_SCOPES, SETTINGS

    granted = SETTINGS.tool_scopes
    print("Scopes")
    for scope in sorted(ALL_TOOL_SCOPES):
        mark = "on " if scope in granted else "OFF"
        note = "  <- nothing leaves this machine until this is on" \
            if scope == "brief:deliver" and scope not in granted else ""
        print(f"  [{mark}] {scope}{note}")

    print("\nTools")
    for spec in harness.describe_registry():
        state = "available" if spec["granted"] else "REFUSED (missing scope)"
        effect = "writes/sends" if spec["side_effect"] else "read-only"
        print(f"  {spec['name']:<14} {effect:<12} {spec['rate_limit_per_min']:>4}/min  "
              f"{','.join(spec['scopes']):<16} {state}")

    rows = Store().recent_tool_calls(limit=args.limit)
    if not rows:
        print("\nNo tool calls recorded yet.")
        return 0

    denials = [r for r in rows if r["denied_at"]]
    print(f"\nLast {len(rows)} call(s) — {len(denials)} denied")
    for row in rows:
        if args.denied and not row["denied_at"]:
            continue
        gates = row["gate_results"]
        trail = " ".join(f"{g[:4]}{'+' if ok else '!'}" for g, ok in gates.items())
        verdict = f"DENIED at {row['denied_at']}" if row["denied_at"] else "ok"
        print(f"  {row['ts'][11:19]}  {row['tool']:<12} {row['caller']:<18} "
              f"{row['duration_ms']:>6}ms  {trail:<34} {verdict}")
    return 0


def _known_prompts() -> dict[str, str]:
    """Editable prompts, mapped to their built-in default."""
    from .agents.triage import SYSTEM_PROMPT as TRIAGE_DEFAULT
    from .research.claude_web import DEFAULT_SYSTEM_PROMPT as RESEARCH_DEFAULT

    return {"research": RESEARCH_DEFAULT, "triage": TRIAGE_DEFAULT}


def cmd_prompts(args: argparse.Namespace) -> int:
    """Show which prompt is in use, or write one out so it can be edited."""
    from . import prompts

    known = _known_prompts()
    names = [args.name] if args.name else sorted(known)
    for name in names:
        if name not in known:
            print(f"Unknown prompt {name!r}. Known: {', '.join(sorted(known))}")
            return 1

    if args.write:
        for name in names:
            try:
                path = prompts.scaffold(name, known[name], overwrite=args.force)
            except FileExistsError as exc:
                print(f"{exc} already exists — pass --force to overwrite.")
                return 1
            print(f"Wrote {path}\nEdit it and the next run picks it up; delete it to go back.")
        return 0

    for name in names:
        loaded = prompts.load(name, known[name])
        marker = "custom" if loaded.customised else "built-in"
        print(f"\n{'=' * 72}\n{name}  [{marker}]  source: {loaded.source}")
        if loaded.extra:
            print(f"  plus {name.upper()}_PROMPT_EXTRA ({len(loaded.extra)} chars)")
        print(f"  file: {prompts.prompt_path(name)}"
              f"{'' if prompts.prompt_path(name).exists() else '  (not created yet)'}")
        if args.show:
            print(f"{'-' * 72}\n{loaded.text}")
    if not args.show:
        print("\nPass --show to print the text, --write to create an editable copy.")
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    """Research one named company, or catch up on every un-researched lead."""
    from .models import Relationship, TriageResult
    from .pipeline import research_leads

    store = Store()
    if args.domain:
        targets = [
            TriageResult(
                message_id="",
                is_business_contact=True,
                relationship=Relationship.UNKNOWN,
                company_name=args.name,
                company_domain=args.domain,
                should_research=True,
                confidence=1.0,
            )
        ]
    else:
        # Every lead already triaged; research_leads dedupes by domain and skips
        # anything still inside the cache window.
        targets = [
            TriageResult.model_validate(lead["triage"])
            for lead in store.recent_leads(limit=500, only_research=True)
        ]
        if not targets:
            print("No leads to research yet — run a scan first.")
            return 0

    outcomes = research_leads(
        targets, store=store, force=args.force, limit=args.limit, progress=_say
    )

    if args.json:
        print(json.dumps(
            {
                domain: (o.profile.model_dump() if o.ok and o.profile else {"error": o.error})
                for domain, o in outcomes.items()
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    for domain, outcome in outcomes.items():
        if not outcome.ok or outcome.profile is None:
            print(f"\n✗ {domain} — {outcome.error}")
            continue
        _print_profile(outcome.profile)
    return 0


def _print_profile(p) -> None:
    print(f"\n{'=' * 72}\n{p.name}  ({p.domain})   confidence {p.confidence:.2f}")
    if p.one_liner:
        print(f"  {p.one_liner}")
    facts = [
        f"{label}: {value}"
        for label, value in (
            ("industry", p.industry), ("hq", p.hq_location),
            ("size", p.size_estimate), ("founded", p.founded),
        )
        if value
    ]
    if facts:
        print("  " + "   ".join(facts))
    if p.products:
        print(f"  products: {', '.join(p.products[:8])}")
    if p.description:
        print(f"\n  {p.description}")
    if p.news:
        print(f"\n  Recent news ({len(p.news)}):")
        for item in p.news[:5]:
            when = f"  [{item.published}]" if item.published else ""
            print(f"    • {item.title}{when}")
            if item.summary:
                print(f"      {item.summary}")
    if p.meeting_prep:
        print("\n  Meeting prep:")
        for point in p.meeting_prep:
            print(f"    - {point}")
    if p.sources:
        print(f"\n  Sources ({len(p.sources)}):")
        for url in p.sources[:8]:
            print(f"    {url}")
    if p.notes:
        print(f"\n  Notes: {p.notes}")


def cmd_scan(args: argparse.Namespace) -> int:
    store = Store()
    # `--new-only` is what a scheduled job wants: the window is whatever has
    # arrived since the last scan, so the cron interval and `--since` no longer
    # have to be kept in step with each other.
    since_at = new_mail_since(store, cap=args.since) if args.new_only else None
    started = datetime.now(timezone.utc)

    report = scan(
        since=args.since,
        since_at=since_at,
        max_results=args.max_results,
        account_ids=args.account,
        raw_query=args.query,
        include_known_senders=args.include_known,
        reprocess=args.reprocess,
        dry_run=args.dry_run,
        research=False if args.no_research else None,
        store=store,
        progress=_say,
    )
    if args.new_only and not args.dry_run:
        store.set_state(LAST_SCAN_KEY, started.isoformat())
    if args.json:
        print(json.dumps(_as_dict(report), ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 1 if report.errors and not report.accounts_scanned else 0


def cmd_ui(args: argparse.Namespace) -> int:
    try:
        from .webapp.server import serve
    except ImportError as exc:  # fastapi/uvicorn missing
        print(
            f"The web interface needs an extra package that isn't installed ({exc.name}).\n"
            "Run:  pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2
    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    removed = Store().purge_user(args.user_id)
    print(f"Deleted {removed} row(s) for user {args.user_id!r}.")
    return 0


# ---------------------------------------------------------------------------


def _as_dict(report: ScanReport) -> dict:
    return {
        "accounts_scanned": report.accounts_scanned,
        "errors": [{"account": e.account.account_id, "error": e.error} for e in report.errors],
        "fetched": report.fetched,
        "skipped": report.skip_counts(),
        "triaged": [
            {
                "uid": message.uid,
                "account": message.account_id,
                "provider": message.provider,
                "subject": message.subject,
                "from": message.sender.email,
                "received_at": message.received_at.isoformat() if message.received_at else None,
                **result.model_dump(mode="json"),
            }
            for message, result in report.triaged
        ],
    }


def _print_report(report: ScanReport) -> None:
    scanned = ", ".join(report.accounts_scanned) or "none"
    print(f"\nScanned {len(report.accounts_scanned)} account(s): {scanned}")
    print(f"Fetched {report.fetched} message(s)")

    for error in report.errors:
        print(f"  ! {error.account.account_id}: {error.error}")
    for reason, count in sorted(report.skip_counts().items(), key=lambda kv: -kv[1]):
        print(f"  skipped {count:>3}  — {reason}")

    leads = report.leads
    print(f"\nTriaged {len(report.triaged)}; {len(leads)} lead(s) worth researching\n")

    for message, result in report.triaged:
        marker = "★" if result.should_research else "·"
        print(
            f"{marker} {result.relationship.value:<10} conf={result.confidence:.2f}  "
            f"{message.sender.email}  [{message.account_id}]"
        )
        print(f"    subject : {message.subject[:80]}")
        if result.company_name or result.company_domain:
            print(f"    company : {result.company_name} ({result.company_domain or 'no domain'})")
        if result.contact_name or result.contact_title:
            print(f"    contact : {result.contact_name} — {result.contact_title}")
        if result.intent_summary:
            print(f"    intent  : {result.intent_summary}")
        if result.mentions_meeting:
            print("    meeting : mentioned in this email")
        print()

    if leads:
        print("Next step (not built yet): research these companies and draft a brief:")
        for message, result in leads:
            print(
                f"  - {result.company_name or message.sender.domain} "
                f"({result.company_domain or message.sender.domain})"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="companies_research",
        description="Run with no arguments to open the web interface.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    # No subcommand → the UI, so a non-technical user never has to learn one.
    parser.set_defaults(func=cmd_ui, port=8765, no_browser=False)
    sub = parser.add_subparsers(dest="command")

    p_ui = sub.add_parser("ui", help="open the web interface (default)")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p_ui.set_defaults(func=cmd_ui)

    def add_account_filter(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--account",
            action="append",
            metavar="ID",
            help="limit to this account (repeatable); default is all enabled accounts",
        )

    p_accounts = sub.add_parser("accounts", help="list configured mailboxes")
    p_accounts.add_argument("--check", action="store_true", help="verify each one connects")
    p_accounts.set_defaults(func=cmd_accounts)

    p_auth = sub.add_parser("auth", help="run interactive auth for each account")
    add_account_filter(p_auth)
    p_auth.set_defaults(func=cmd_auth)

    p_seed = sub.add_parser(
        "seed", help="mark existing contacts as known (run once, before the first scan)"
    )
    p_seed.add_argument("--since", type=parse_duration, default=timedelta(days=180))
    add_account_filter(p_seed)
    p_seed.set_defaults(func=cmd_seed)

    p_scan = sub.add_parser("scan", help="read new mail and find new customers/partners")
    p_scan.add_argument("--since", type=parse_duration, default=timedelta(days=1),
                        help="e.g. 12h, 1d, 2w (default 1d)")
    p_scan.add_argument("--query", default=None,
                        help="provider-native query, bypasses --since (single-provider use)")
    p_scan.add_argument("--max-results", type=int, default=100)
    add_account_filter(p_scan)
    p_scan.add_argument("--new-only", action="store_true",
                        help="only mail that arrived since the last scan; --since caps how far "
                             "back a catch-up reaches (recommended for scheduled runs)")
    p_scan.add_argument("--include-known", action="store_true",
                        help="also triage senders you've corresponded with before")
    p_scan.add_argument("--reprocess", action="store_true",
                        help="re-triage messages already processed")
    p_scan.add_argument("--no-research", action="store_true",
                        help="triage only; skip step 2 company research")
    p_scan.add_argument("--dry-run", action="store_true",
                        help="do not write anything to the local database")
    p_scan.add_argument("--json", action="store_true", help="machine-readable output")
    p_scan.set_defaults(func=cmd_scan)

    p_research = sub.add_parser(
        "research", help="research one company, or every lead not yet researched"
    )
    p_research.add_argument("domain", nargs="?", help="company domain, e.g. agora.io")
    p_research.add_argument("--name", default="", help="company name, if the domain is ambiguous")
    p_research.add_argument("--force", action="store_true", help="ignore the cache and re-research")
    p_research.add_argument("--limit", type=int, default=None, help="max companies this run")
    p_research.add_argument("--json", action="store_true", help="machine-readable output")
    p_research.set_defaults(func=cmd_research)

    p_brief = sub.add_parser("brief", help="assemble a brief for one company")
    p_brief.add_argument("domain", help="company domain, e.g. agora.io")
    p_brief.add_argument("--html", action="store_true", help="render HTML instead of markdown")
    p_brief.add_argument("--json", action="store_true", help="machine-readable output")
    p_brief.add_argument("--no-calendar", action="store_true", help="skip the calendar lookup")
    p_brief.add_argument("--no-save", action="store_true", help="render without persisting")
    p_brief.set_defaults(func=cmd_brief)

    p_cal = sub.add_parser("calendar", help="upcoming meetings with a company")
    p_cal.add_argument("domain", nargs="?", default="", help="company domain, e.g. agora.io")
    p_cal.add_argument("--name", default="", help="company name, for the weaker title match")
    p_cal.add_argument("--days", type=int, default=None, help="lookahead window")
    p_cal.add_argument("--json", action="store_true", help="machine-readable output")
    p_cal.set_defaults(func=cmd_calendar)

    p_eval = sub.add_parser("eval", help="score the agent against recorded fixtures")
    p_eval.add_argument("--record", action="store_true",
                        help="re-run live against the API and update the recordings")
    p_eval.add_argument("--only", default=None,
                        help="one class: lead | hard | negative | injection")
    p_eval.set_defaults(func=cmd_eval)

    p_tools = sub.add_parser("tools", help="what the agent may do, and what it has done")
    p_tools.add_argument("--limit", type=int, default=20, help="audit rows to show")
    p_tools.add_argument("--denied", action="store_true", help="only refused calls")
    p_tools.set_defaults(func=cmd_tools)

    p_prompts = sub.add_parser("prompts", help="show or customise the agent prompts")
    p_prompts.add_argument("name", nargs="?", help="research | triage (default: all)")
    p_prompts.add_argument("--show", action="store_true", help="print the prompt text")
    p_prompts.add_argument("--write", action="store_true",
                           help="write the built-in prompt to prompts/<name>.md for editing")
    p_prompts.add_argument("--force", action="store_true", help="overwrite an existing file")
    p_prompts.set_defaults(func=cmd_prompts)

    p_purge = sub.add_parser("purge", help="delete all stored data for one user (erasure request)")
    p_purge.add_argument("user_id")
    p_purge.set_defaults(func=cmd_purge)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    from .obs import start as _start_observability

    _start_observability()
    try:
        return args.func(args)
    except AccountsError as exc:
        print(f"Account configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
