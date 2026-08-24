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
    if getattr(args, "drive", False):
        # Consent for Drive happens here, not mid-chat: the same command that
        # owns every other consent. Separate token, Drive-only scope.
        from . import drive

        try:
            listing = drive.list_files(page_size=1)
        except Exception as exc:
            print(f"  ✗ Drive: {exc}", file=sys.stderr)
            return 1
        print(f"  ✓ Drive authorised — {listing['total_files']} file(s) visible")
        return 0

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


def cmd_report_quality(args: argparse.Namespace) -> int:
    """Score finished briefs for real companies against the six criteria."""
    import sys as _sys

    root = ROOT_DIR / "tests"
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    from eval.report_quality import run as run_quality

    report = run_quality(only=args.only)
    return 0 if report else 1


def cmd_redteam(args: argparse.Namespace) -> int:
    """Attack the agent with prompt-injection payloads and report what held."""
    import sys as _sys

    root = ROOT_DIR / "tests"
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    from eval.redteam import run as run_redteam

    report = run_redteam(only_family=args.family)
    if not report:
        return 1
    # A breach is a failing build, not a note in a table.
    if report["breaches"]:
        return 1
    if any(not c["refused"] for c in report["gate_checks"]):
        return 1
    return 0


def cmd_langfuse(args: argparse.Namespace) -> int:
    """The eval lane in Langfuse: sync assets, run experiments, verify."""
    import sys as _sys

    root = ROOT_DIR / "tests"
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    from eval.langfuse_lane import LaneError

    try:
        if args.action == "sync":
            from eval.langfuse_sync import run as run_sync

            run_sync()
            return 0

        if args.action == "experiment":
            from eval.langfuse_experiment import run as run_experiment

            models = None
            if args.model:
                models = []
                for spec in args.model:
                    backend, _, name = spec.partition(":")
                    if not name:
                        print(f"--model wants backend:name, got {spec!r}")
                        return 2
                    models.append((name, backend, name))
            results = run_experiment(models=models, replay=args.replay,
                                     judge=args.judge)
            return 0 if results else 1

        if args.action == "quality":
            from eval.langfuse_experiment import run_quality

            return 0 if run_quality(only=args.only) else 1

        # verify — the exit code is the point: CI calls this.
        from eval.langfuse_verify import run as run_verify

        return 0 if run_verify() else 1
    except LaneError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2


def cmd_compare_embeddings(args: argparse.Namespace) -> int:
    """Compare embedding models on retrieval over the fixture mailbox."""
    import sys as _sys

    root = ROOT_DIR / "tests"
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    from eval.compare_embeddings import Candidate
    from eval.compare_embeddings import run as run_embeddings

    candidates = None
    if args.model:
        candidates = []
        for spec in args.model:
            provider, _, name = spec.partition(":")
            if not name:
                print(f"--model wants provider:name, got {spec!r}")
                return 2
            candidates.append(Candidate(name, provider, name))

    report = run_embeddings(only=args.only, candidates=candidates)
    return 0 if report else 1


def cmd_compare(args: argparse.Namespace) -> int:
    """Run the same fixtures through several models and print the trade-off."""
    import sys as _sys

    root = ROOT_DIR / "tests"
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    from eval.compare import run as run_compare

    models = None
    if args.model:
        models = []
        for spec in args.model:
            backend, _, name = spec.partition(":")
            if not name:
                print(f"--model wants backend:name, got {spec!r}")
                return 2
            models.append((name, backend, name))

    report = run_compare(passes=args.passes, batch_size=args.batch_size,
                         only=args.only, models=models)
    return 0 if report else 1


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


# --- interactive chat demo --------------------------------------------------

# What each role may do. "user" is every read; "admin" adds the one write the
# demo has. Neither includes brief:deliver — the chat demo never sends anything.
ROLE_SCOPES = {
    "admin": ("drive:read", "memory:read", "memory:write",
              "research:read", "mail:read", "calendar:read"),
    "user": ("drive:read", "memory:read",
             "research:read", "mail:read", "calendar:read"),
}

CHAT_HELP = """\
Commands:
  /audit    show the audit log (every tool call, all six gates)
  /memory   show everything in long-term memory
  /index    embed this agent's research and briefs into memory (RAG over
            what it already knows)
  /tools    show tools and granted scopes
  /clear    forget this conversation (long-term memory stays)
  /help     this message
  /quit     exit
"""


def _print_tool_event(name: str, args: dict, result) -> None:
    """One line per tool call, so the terminal shows the agent working."""
    shown = {k: (v[:60] + "…" if isinstance(v, str) and len(v) > 60 else v)
             for k, v in args.items()}
    arg_text = ", ".join(f"{k}={v!r}" for k, v in shown.items())
    if isinstance(result, dict) and result.get("status") == "denied":
        print(f"  [x] {name}({arg_text}) DENIED at {result.get('gate')} gate: "
              f"{result.get('reason')}")
        return
    if isinstance(result, dict) and result.get("status") == "error":
        print(f"  [!] {name}({arg_text}) error: {str(result.get('error'))[:150]}")
        return
    if isinstance(result, dict):
        if "total_files" in result:
            summary = f"{result['total_files']} file(s)"
        elif "results_count" in result:
            summary = f"{result['results_count']} memory match(es)"
        elif "saved_chunks" in result:
            summary = f"saved {result['saved_chunks']} chunk(s)"
        elif "file_name" in result:
            summary = f"read {result['file_name']} ({len(result.get('content', ''))} chars)"
            if result.get("screening"):
                summary += " [guardrails flagged]"
        elif "leads" in result:
            summary = f"{result.get('total', 0)} lead(s)"
        elif "briefs" in result:
            summary = f"{result.get('total', 0)} brief(s)"
        elif "profile" in result or "found" in result:
            summary = ("research found" if result.get("found")
                       else "no research cached")
        elif "meetings" in result:
            count = len(result.get("meetings") or [])
            summary = (f"{count} meeting(s)" if result.get("checked")
                       else f"could not look ({result.get('reason', '')[:60]})")
        else:
            summary = "ok"
    else:
        summary = "ok"
    print(f"  [+] {name}({arg_text}) -> {summary}")


def _print_chat_audit(limit: int) -> None:
    from .tools import GATES

    rows = Store().recent_tool_calls(limit=limit)
    if not rows:
        print("\n[no tool calls recorded yet]")
        return
    rows.reverse()  # oldest first reads like a story
    print(f"\n--- AUDIT LOG (last {len(rows)} call(s), gates: "
          f"{' -> '.join(GATES)}) ---")
    for row in rows:
        gates = row["gate_results"]
        trail = "  ".join(
            f"{gate}={'ok' if gates[gate] else 'DENY'}" if gate in gates else f"{gate}=-"
            for gate in GATES
        )
        verdict = f"DENIED at {row['denied_at']}" if row["denied_at"] else "ok"
        print(f"  {row['ts'][:19]}  {row['tool']:<18} {row['caller']:<12} "
              f"{row['duration_ms']:>5}ms  {verdict}")
        print(f"      {trail}")
    print("--- END AUDIT LOG ---")


def _print_memories() -> None:
    rows = Store().list_memories()
    if not rows:
        print("\n[no memories stored yet]")
        return
    print(f"\n--- MEMORIES ({len(rows)} row(s), newest first) ---")
    for row in rows:
        text = row["text"].replace("\n", " ")
        text = text[:100] + "…" if len(text) > 100 else text
        origin = f" (from {row['source']})" if row["source"] else ""
        print(f"  [{row['category']}]{origin} {text}")
    print("--- END MEMORIES ---")


def cmd_chat(args: argparse.Namespace) -> int:
    """Interactive agent over Drive + RAG memory — the harness demo, in a terminal."""
    import os

    from . import tools as harness
    from .config import SETTINGS, refresh_settings_from_env

    os.environ["TOOL_SCOPES"] = ",".join(ROLE_SCOPES[args.role])
    refresh_settings_from_env()

    from .agents.chat import ChatAgent

    harness.set_caller(f"chat:{args.role}")
    try:
        agent = ChatAgent(backend=args.backend, model=args.model,
                          on_tool=_print_tool_event)
    except ValueError as exc:
        print(exc)
        return 2

    print(f"\nAgent chat — {agent.describe()}, role={args.role} "
          f"(scopes: {', '.join(sorted(SETTINGS.tool_scopes))})")
    print(CHAT_HELP)

    def one_turn(text: str) -> None:
        try:
            reply = agent.run(text)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"\n[error: {exc}]")
            return
        print(f"\nAssistant: {reply}")

    def handle(text: str) -> bool:
        """Run one input, command or message. Returns False to exit."""
        lowered = text.lower()
        if lowered in ("/quit", "/exit"):
            return False
        if lowered == "/help":
            print(CHAT_HELP)
        elif lowered == "/clear":
            agent.clear()
            print("[conversation cleared — long-term memory kept]")
        elif lowered == "/audit":
            _print_chat_audit(limit=args.limit)
        elif lowered == "/memory":
            _print_memories()
        elif lowered == "/index":
            from . import memory

            try:
                counts = memory.index_knowledge()
            except memory.MemoryUnavailable as exc:
                print(f"[cannot index: {exc}]")
            else:
                print(f"[indexed {counts['research']} research profile(s) and "
                      f"{counts['briefs']} brief(s) as {counts['chunks']} memory "
                      f"chunk(s)]")
        elif lowered == "/tools":
            for spec in harness.describe_registry():
                state = "available" if spec["granted"] else "refused (missing scope)"
                print(f"  {spec['name']:<18} {','.join(spec['scopes']):<14} {state}")
        elif lowered.startswith("/"):
            print(f"[unknown command {text!r} — /help lists them]")
        else:
            one_turn(text)
        return True

    if args.message:
        for text in args.message:
            print(f"\nYou: {text}")
            if not handle(text.strip()):
                break
        return 0

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0
        if not user_input:
            continue
        if not handle(user_input):
            print("Bye.")
            return 0


def _known_prompts() -> dict[str, str]:
    """Editable prompts, mapped to their built-in default."""
    from .agents.chat import DEFAULT_SYSTEM_PROMPT as CHAT_DEFAULT
    from .agents.triage import SYSTEM_PROMPT as TRIAGE_DEFAULT
    from .research.claude_web import DEFAULT_SYSTEM_PROMPT as RESEARCH_DEFAULT

    return {"chat": CHAT_DEFAULT, "research": RESEARCH_DEFAULT,
            "triage": TRIAGE_DEFAULT}


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
    serve(port=args.port, open_browser=not args.no_browser, host=args.host)
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    removed = Store().purge_user(args.user_id)
    print(f"Deleted {removed} row(s) for user {args.user_id!r}.")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Serve the pipeline over MCP for Claude and ChatGPT — see MCP.md."""
    try:
        from .mcp_server import run as run_mcp
    except ImportError as exc:
        print(
            f"The MCP server needs an extra package that isn't installed ({exc.name}).\n"
            "Run:  pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2
    run_mcp(
        transport="streamable-http" if args.http else "stdio",
        host=args.host,
        port=args.port,
    )
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
    parser.set_defaults(func=cmd_ui, port=8765, no_browser=False, host="127.0.0.1")
    sub = parser.add_subparsers(dest="command")

    p_ui = sub.add_parser("ui", help="open the web interface (default)")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p_ui.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (default 127.0.0.1; 0.0.0.0 only inside a "
        "container or behind a tunnel — see DEPLOYMENT.md)",
    )
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
    p_auth.add_argument("--drive", action="store_true",
                        help="consent Google Drive (read-only) for the chat command")
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
                        help="also triage senders you've corresponded with before, "
                             "and mail sent from your own address (testmail)")
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

    p_cmp = sub.add_parser(
        "compare",
        help="run the fixtures through several models and compare accuracy, cost and latency",
    )
    p_cmp.add_argument("--passes", type=int, default=3,
                       help="runs per model (default 3; more than one because "
                            "triage is sampled and a single run is not a measurement)")
    p_cmp.add_argument("--batch-size", type=int, default=10,
                       help="messages per model call (default 10, as in a real scan)")
    p_cmp.add_argument("--only", default=None,
                       help="restrict to one fixture class: lead | negative | hard | injection")
    p_cmp.add_argument("--model", action="append", default=None, metavar="BACKEND:NAME",
                       help="override the candidate list, e.g. anthropic:claude-sonnet-5. "
                            "Repeatable.")
    p_cmp.set_defaults(func=cmd_compare)

    p_emb = sub.add_parser(
        "compare-embeddings",
        help="compare embedding models on retrieval over the fixture mailbox",
    )
    p_emb.add_argument("--only", default=None,
                       help="restrict to one fixture class")
    p_emb.add_argument("--model", action="append", default=None, metavar="PROVIDER:NAME",
                       help="override the candidates, e.g. ollama:nomic-embed-text. "
                            "Repeatable.")
    p_emb.set_defaults(func=cmd_compare_embeddings)

    p_red = sub.add_parser(
        "redteam",
        help="attack the agent with prompt-injection payloads; non-zero exit on a breach",
    )
    p_red.add_argument("--family", default=None,
                       help="one family: exfiltration | tool-coercion | override | "
                            "fence-escape | obfuscation | placement")
    p_red.set_defaults(func=cmd_redteam)

    p_rq = sub.add_parser(
        "report-quality",
        help="score briefs for real companies: completeness, sources, freshness, readiness",
    )
    p_rq.add_argument("--only", default=None,
                      help="one company id: fpt | vinamilk | samsung | shopee | "
                           "viettel | bosch")
    p_rq.set_defaults(func=cmd_report_quality)

    p_lf = sub.add_parser(
        "langfuse",
        help="sync prompts/datasets to Langfuse, run model experiments, verify",
    )
    lf_sub = p_lf.add_subparsers(dest="action", required=True)
    lf_sub.add_parser(
        "sync", help="push prompts, datasets, score configs and the annotation queue"
    )
    p_lf_exp = lf_sub.add_parser(
        "experiment",
        help="one Langfuse run per model over the fixtures — the A/B compare",
    )
    p_lf_exp.add_argument("--model", action="append", default=None,
                          metavar="BACKEND:NAME",
                          help="override the candidates, e.g. anthropic:claude-sonnet-5. "
                               "Repeatable.")
    p_lf_exp.add_argument("--replay", action="store_true",
                          help="answer from the recordings: free, offline, proves the loop")
    p_lf_exp.add_argument("--judge", action="store_true",
                          help="also run the LLM judge on intent_summary faithfulness")
    p_lf_q = lf_sub.add_parser(
        "quality", help="the report-quality harness as a Langfuse run (live web, slow)"
    )
    p_lf_q.add_argument("--only", default=None,
                        help="one company id: fpt | vinamilk | samsung | shopee | "
                             "viettel | bosch")
    lf_sub.add_parser(
        "verify",
        help="check prompts, datasets, runs, scores and the queue against the "
             "public API; non-zero exit if anything is missing",
    )
    p_lf.set_defaults(func=cmd_langfuse)

    p_tools = sub.add_parser("tools", help="what the agent may do, and what it has done")
    p_tools.add_argument("--limit", type=int, default=20, help="audit rows to show")
    p_tools.add_argument("--denied", action="store_true", help="only refused calls")
    p_tools.set_defaults(func=cmd_tools)

    p_chat = sub.add_parser(
        "chat",
        help="interactive agent: Google Drive + long-term RAG memory, every "
             "call through the six gates",
    )
    p_chat.add_argument("--role", choices=sorted(ROLE_SCOPES), default="admin",
                        help="admin may write memory; user is read-only")
    p_chat.add_argument("--backend", choices=("ollama", "anthropic"), default="",
                        help="where the chat model runs (default: TRIAGE_BACKEND)")
    p_chat.add_argument("--model", default="", help="model override for the chosen backend")
    p_chat.add_argument("-m", "--message", action="append",
                        help="run one message and exit (repeatable, in order)")
    p_chat.add_argument("--limit", type=int, default=30, help="audit rows /audit shows")
    p_chat.set_defaults(func=cmd_chat)

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

    p_mcp = sub.add_parser(
        "mcp", help="serve the agent over MCP for Claude and ChatGPT (see MCP.md)"
    )
    p_mcp.add_argument("--http", action="store_true",
                       help="streamable HTTP instead of stdio (for remote clients "
                            "— claude.ai and ChatGPT connectors, behind a tunnel)")
    p_mcp.add_argument("--host", default="127.0.0.1",
                       help="interface to bind in --http mode (default 127.0.0.1; "
                            "0.0.0.0 only inside a container — see MCP.md)")
    p_mcp.add_argument("--port", type=int, default=8766,
                       help="port for --http mode (default 8766)")
    p_mcp.set_defaults(func=cmd_mcp)

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
