"""Step 1 of the pipeline: read email across all accounts, find new customers/partners."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Sequence

from .accounts import load_accounts
from .agents.triage import TriageAgent
from .config import SETTINGS
from .models import CompanyProfile, EmailMessage, TriageResult
from .providers import Account, EmailProvider, Folder, MessageQuery, ProviderError, build_provider
from .research.base import ResearchOutcome
from .store import Store
from . import tools
from .obs import metrics as obs_metrics
from .obs import tracing as obs_tracing

if TYPE_CHECKING:  # only for annotations; the provider itself is imported lazily
    from .research.base import ResearchProvider

log = logging.getLogger(__name__)

# Consumer mail hosts. Sharing one of these with a sender says nothing about
# whether they're an internal colleague — a solo founder on gmail.com would
# otherwise skip every other gmail.com sender, i.e. most of their leads.
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
        "msn.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "icloud.com",
        "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
        "gmx.com", "zoho.com", "mail.com", "fastmail.com", "qq.com", "163.com",
    }
)


# The moment the agent started watching. Mail already sitting in the inbox then
# is history, not a new arrival, so no scan ever reaches back past it.
WATERMARK_KEY = "watch_since"
LAST_SCAN_KEY = "last_scan_at"

# Mail servers index new messages with a small lag and clocks drift, so each
# scan re-asks for a couple of minutes it has already covered. That costs
# nothing: anything genuinely seen before is skipped by uid.
WINDOW_OVERLAP = timedelta(minutes=2)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_state_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("Ignoring unparseable stored timestamp %r", raw)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def start_watching(store: Store, *, when: datetime | None = None) -> datetime:
    """Draw the line between "already there" and "newly arrived"."""
    mark = when or _utcnow()
    store.set_state(WATERMARK_KEY, mark.isoformat())
    log.info("Watching for mail that arrives after %s", mark.isoformat())
    return mark


def new_mail_since(store: Store, *, cap: timedelta) -> datetime:
    """Lower bound for a scan that should only see genuinely new mail.

    Normally this is where the last scan finished, so consecutive scans tile the
    timeline with no gap and no repetition. It never reaches back past the
    watermark, and ``cap`` bounds how much history a catch-up scan will trawl
    through after the app has been off for a while.
    """
    now = _utcnow()
    mark = _parse_state_time(store.get_state(WATERMARK_KEY)) or start_watching(store, when=now)
    last = _parse_state_time(store.get_state(LAST_SCAN_KEY))
    start = mark if last is None else max(mark, last - WINDOW_OVERLAP)
    return max(start, now - cap)


@dataclass
class SkippedMessage:
    message: EmailMessage
    reason: str


@dataclass
class AccountError:
    account: Account
    error: str


@dataclass
class ScanReport:
    fetched: int = 0
    skipped: list[SkippedMessage] = field(default_factory=list)
    triaged: list[tuple[EmailMessage, TriageResult]] = field(default_factory=list)
    researched: list[tuple[str, ResearchOutcome]] = field(default_factory=list)
    indexed: int = 0  # research profiles folded into the knowledge base this run
    errors: list[AccountError] = field(default_factory=list)
    accounts_scanned: list[str] = field(default_factory=list)

    @property
    def leads(self) -> list[tuple[EmailMessage, TriageResult]]:
        """Messages that should kick off a company research brief."""
        return [(m, t) for m, t in self.triaged if t.should_research]

    def skip_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.skipped:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts

    def merge(self, other: "ScanReport") -> None:
        self.fetched += other.fetched
        self.skipped.extend(other.skipped)
        self.triaged.extend(other.triaged)
        self.researched.extend(other.researched)
        self.indexed += other.indexed
        self.errors.extend(other.errors)
        self.accounts_scanned.extend(other.accounts_scanned)


def scan(
    *,
    since: timedelta = timedelta(days=1),
    since_at: datetime | None = None,
    max_results: int = 100,
    account_ids: list[str] | None = None,
    raw_query: str | None = None,
    include_known_senders: bool = False,
    reprocess: bool = False,
    dry_run: bool = False,
    research: bool | None = None,
    store: Store | None = None,
    agent: TriageAgent | None = None,
    progress: Callable[[str], None] | None = None,
) -> ScanReport:
    """Scan every configured account. One bad account never blocks the others.

    ``since_at`` is an explicit lower bound and wins over the ``since`` window —
    :func:`new_mail_since` builds one that covers exactly the gap since the last
    scan, so an automatic check sees new mail and nothing else.

    ``progress`` receives short human-readable phase labels; the web UI shows
    them while the scan runs.
    """
    store = store or Store()
    report = ScanReport()
    say = progress or (lambda _msg: None)
    tools.set_caller("pipeline.scan")
    # A dry run must not spend money on research either — it exists so the same
    # mail can be re-run freely while tuning.
    if research is None:
        research = SETTINGS.research_enabled and not dry_run

    accounts = load_accounts()
    if account_ids:
        wanted = set(account_ids)
        accounts = [a for a in accounts if a.account_id in wanted]

    if not accounts:
        log.warning("No enabled accounts to scan")
        return report

    scan_started = time.monotonic()
    scan_span = obs_tracing.span("scan", **{"scan.accounts": len(accounts)})
    scan_span.__enter__()

    start = since_at or (_utcnow() - since)
    query = MessageQuery(since=start, max_results=max_results, folder=Folder.INBOX)
    if raw_query:
        query = MessageQuery(raw=raw_query, max_results=max_results, folder=Folder.INBOX)

    candidates: list[tuple[Account, EmailMessage]] = []

    for account in accounts:
        say(f"Reading mail from {account.email or account.account_id}")
        try:
            with build_provider(account) as provider:
                # Through the gate: a revoked mail:read scope refuses here and
                # the scan carries on to the next account rather than dying.
                messages = tools.gmail_read(
                    account_id=account.account_id,
                    folder=query.folder.value if query.folder else "inbox",
                    max_results=max_results,
                    query=raw_query or "",
                    _fetch=lambda: provider.fetch(query),
                )
        except tools.ToolDenied as exc:
            log.warning("[%s] mail read denied at %s", account.account_id, exc.gate)
            report.errors.append(AccountError(account, f"denied at {exc.gate}: {exc.reason}"))
            continue
        except ProviderError as exc:
            log.error("[%s] %s", account.account_id, exc)
            report.errors.append(AccountError(account, str(exc)))
            continue
        except Exception as exc:
            log.exception("[%s] unexpected failure", account.account_id)
            report.errors.append(AccountError(account, f"{type(exc).__name__}: {exc}"))
            continue

        report.accounts_scanned.append(account.account_id)
        report.fetched += len(messages)
        say(f"Read {len(messages)} message(s) from {account.email or account.account_id}")

        kept = 0
        for message in messages:
            reason = _skip_reason(
                message,
                account,
                store,
                include_known_senders=include_known_senders,
                reprocess=reprocess,
            )
            if reason:
                report.skipped.append(SkippedMessage(message, reason))
                log.debug("skip %s — %s", message.sender.email, reason)
            else:
                candidates.append((account, message))
                kept += 1
        log.info(
            "  %s: %d fetched, %d filtered out, %d to classify",
            account.email or account.account_id,
            len(messages),
            len(messages) - kept,
            kept,
        )

    log.info(
        "%d fetched across %d account(s); %d skipped; %d to triage",
        report.fetched,
        len(report.accounts_scanned),
        len(report.skipped),
        len(candidates),
    )
    counts = report.skip_counts()
    for reason, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        log.info("  filtered — %s: %d", reason, count)
        obs_metrics.record_scan_outcome(f"filtered:{reason}", count)

    # Worth its own line rather than one row in the filter table: this is the
    # work the run did *not* have to redo, which is otherwise invisible.
    reused = counts.get("already processed", 0)
    if reused:
        say(f"Skipped {reused} email(s) already classified in an earlier run")

    if not candidates:
        say("No new senders to look at")
        obs_metrics.record_stage("scan", time.monotonic() - scan_started)
        scan_span.__exit__(None, None, None)
        return report

    agent = agent or TriageAgent()
    # `agent` is injectable, so a caller's stand-in may not carry a backend.
    backend = getattr(agent, "backend", None)
    where = backend.describe() if backend is not None else "the triage model"
    say(f"Classifying {len(candidates)} new sender(s) with {where}")
    results = agent.triage([message for _, message in candidates], progress=say)

    leads = sum(1 for r in results if r.should_research)
    obs_metrics.record_scan_outcome("lead", leads)
    obs_metrics.record_scan_outcome("triaged_not_lead", len(results) - leads)
    say(f"Saving {len(results)} result(s) — {leads} lead(s)")

    if research and leads:
        outcomes = research_leads(
            [r for r in results if r.should_research],
            store=store,
            progress=say,
            report=report,
        )
        # A researched profile lives in the research cache, but the knowledge
        # base (what search_memory / the chat agent recall) only sees it once it
        # is embedded. Fold each successful profile in now, so the company is
        # answerable straight after the scan without a manual /index. Index the
        # full outcome set — not just report.researched, which is fresh-only —
        # so a company reused from cache that was never indexed (e.g. the
        # embedder was down on the run that researched it) still gets in.
        # Idempotent per source, so re-indexing an already-indexed cache hit is
        # cheap. Non-fatal: research and triage already succeeded, so a dead
        # embedder must not fail the scan — indexing just waits for the next run.
        if not dry_run:
            _index_researched(outcomes, report, store=store, progress=say)

    for (account, message), result in zip(candidates, results):
        report.triaged.append((message, result))
        if dry_run:
            continue
        try:
            tools.store_write(
                kind="processed_message",
                key=message.uid,
                user_id=account.user_id,
                _write=lambda a=account, m=message, r=result: _persist_triage(store, a, m, r),
            )
        except tools.ToolDenied as exc:
            # A refused write must not lose the scan: the result stays in the
            # report, it simply is not remembered for next time.
            log.warning("store_write denied at %s — %s not recorded", exc.gate, message.uid)

    return report


def _index_researched(
    outcomes: dict[str, ResearchOutcome], report: ScanReport, *,
    store: Store, progress: Callable[[str], None],
) -> None:
    """Embed this run's successful profiles into the knowledge base.

    ``outcomes`` is the full domain→outcome map research_leads returned — both
    freshly-researched and cache-reused. Idempotent per domain
    (``index_research_domain`` replaces a source's rows), so re-indexing a cache
    hit that was already indexed simply re-embeds to the same content. Wholly
    non-fatal: a MemoryUnavailable (Ollama down) or any single-domain error is
    logged and skipped, never raised — the scan's mail work has already
    succeeded and must stand.
    """
    from . import memory

    domains = [d for d, o in outcomes.items() if o.ok and o.profile is not None]
    if not domains:
        return
    indexed = 0
    for domain in domains:  # outcomes is already one entry per domain
        try:
            # Through the same gate as every other scan write: a revoked
            # memory:write scope refuses here and is audited, so the knowledge
            # base cannot be written behind the harness's back.
            chunks = tools.store_write(
                kind="knowledge", key=f"research:{domain}",
                _write=lambda d=domain: memory.index_research_domain(d, store=store),
            )
            if chunks:
                indexed += 1
        except tools.ToolDenied as exc:
            # Scope revoked mid-scan — stop trying, note it, let the scan stand.
            log.warning("knowledge-base write denied at %s — not indexing", exc.gate)
            progress("Knowledge base not updated (memory:write not granted)")
            break
        except memory.MemoryUnavailable as exc:
            # The embedder is down for the whole run — one message, then stop
            # trying rather than repeat it per domain.
            log.info("knowledge base not updated (%s) — run `/index` later", exc)
            progress("Knowledge base not updated (embedder unavailable)")
            break
        except Exception:
            log.exception("indexing %s into the knowledge base failed", domain)
    report.indexed += indexed
    if indexed:
        progress(f"Indexed {indexed} compan(y/ies) into the knowledge base")


def _persist_triage(
    store: Store, account: Account, message: EmailMessage, result: TriageResult
) -> None:
    """The two writes one triaged message produces, as one auditable unit."""
    store.record_sender(
        message,
        user_id=account.user_id,
        company_name=result.company_name,
        relationship=result.relationship.value,
    )
    store.mark_processed(message, result, user_id=account.user_id)


def research_leads(
    results: Sequence[TriageResult],
    *,
    store: Store | None = None,
    researcher: "ResearchProvider | None" = None,
    force: bool = False,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
    report: ScanReport | None = None,
) -> dict[str, ResearchOutcome]:
    """Look up each lead's company, newest first, and cache what comes back.

    Keyed by domain, so several people writing from one company cost one lookup,
    and a company already researched this month costs nothing at all.
    """
    from .research import build_researcher  # local: keeps the import optional

    store = store or Store()
    say = progress or (lambda _msg: None)
    tools.set_caller("pipeline.research")
    ttl = timedelta(days=SETTINGS.research_ttl_days)
    cap = limit if limit is not None else SETTINGS.research_max_companies

    # One entry per domain — the first mention wins, since results arrive newest
    # first and the newest email is the one the brief is about.
    targets: dict[str, TriageResult] = {}
    for result in results:
        key = (result.company_domain or "").strip().lower()
        if not key:
            log.debug("no domain for %r — cannot research", result.company_name)
            continue
        targets.setdefault(key, result)

    if not targets:
        say("No researchable companies (no domain identified)")
        return {}

    outcomes: dict[str, ResearchOutcome] = {}
    pending: list[tuple[str, TriageResult]] = []
    for domain, result in targets.items():
        cached = None if force else store.get_research(domain, max_age=ttl)
        if cached and cached.get("profile"):
            log.info("  research cached — %s (%s)", domain, cached["researched_at"][:10])
            outcomes[domain] = ResearchOutcome(
                profile=CompanyProfile.model_validate(cached["profile"])
            )
        elif cached and not cached["ok"]:
            log.info("  research recently failed — %s, not retrying yet", domain)
        else:
            pending.append((domain, result))

    if outcomes:
        say(f"Reused cached research for {len(outcomes)} compan(y/ies)")
    if not pending:
        return outcomes

    dropped = 0
    if cap and len(pending) > cap:
        dropped = len(pending) - cap
        pending = pending[:cap]

    researcher = researcher or build_researcher()
    say(f"Researching {len(pending)} compan(y/ies) with {researcher.describe()}")
    if dropped:
        # Never truncate silently: a brief missing five companies looks the same
        # as a brief that covered everything.
        say(f"{dropped} more will wait for the next run (RESEARCH_MAX_COMPANIES)")

    for index, (domain, result) in enumerate(pending, start=1):
        name = result.company_name or domain
        say(f"Researching {index}/{len(pending)} · {name} ({domain})")
        started = time.monotonic()
        with obs_tracing.span("stage.research", **{"research.domain": domain}):
            outcome = researcher.research(
                company=result.company_name, domain=domain, context=result.intent_summary
            )
        elapsed = time.monotonic() - started
        obs_metrics.record_stage("research", elapsed)

        if outcome.ok and outcome.profile is not None:
            profile = outcome.profile
            log.info(
                "  ✓ %s — %s · %d news · %d source(s) · conf %.2f "
                "(%d search/%d fetch, %.1fs)",
                domain,
                profile.one_liner[:60] or profile.industry or "profiled",
                len(profile.news),
                len(profile.sources),
                profile.confidence,
                outcome.searches,
                outcome.fetches,
                elapsed,
            )
        else:
            log.warning("  ✗ %s — %s (%.1fs)", domain, outcome.error, elapsed)

        try:
            tools.store_write(
                kind="company_research",
                key=domain,
                _write=lambda d=domain, o=outcome, r=result: store.save_research(
                    d, o.profile,
                    company_name=r.company_name,
                    provider=researcher.name,
                    model=researcher.model,
                    error=o.error,
                ),
            )
        except tools.ToolDenied as exc:
            log.warning("store_write denied at %s — %s not cached", exc.gate, domain)
        outcomes[domain] = outcome
        if report is not None:
            report.researched.append((domain, outcome))

    ok = sum(1 for o in outcomes.values() if o.ok)
    say(f"Research done — {ok}/{len(outcomes)} compan(y/ies) profiled")
    return outcomes


def _skip_reason(
    message: EmailMessage,
    account: Account,
    store: Store,
    *,
    include_known_senders: bool,
    reprocess: bool,
) -> str | None:
    sender = message.sender.email
    if not sender:
        return "no sender address"
    if sender == account.email or sender in SETTINGS.user_emails:
        # --include-known lifts this gate too: the testmail demo sends its
        # lead from your own address, and it should still reach triage.
        if not include_known_senders:
            return "sent by you"
    if _is_ignored_domain(message, account):
        return "ignored/own domain"
    if message.is_automated:
        return "automated or bulk mail"
    if not reprocess and store.is_processed(message):
        return "already processed"
    if not include_known_senders and _is_known_sender(message, account, store):
        return "known sender"
    return None


def _is_known_sender(message: EmailMessage, account: Account, store: Store) -> bool:
    """Have we dealt with this sender — or their company — before?

    Matching on the domain is what makes "a new person at a customer we already
    know" not count as a lead. It is meaningless on consumer mail hosts though:
    seeding records every gmail.com contact you have, and one of those would
    otherwise make every future gmail.com sender look familiar — silently
    hiding exactly the solo founders and consultants we most want to see.
    """
    return store.is_known_sender(
        message.sender.email,
        user_id=account.user_id,
        match_domain=message.sender.domain not in FREE_MAIL_DOMAINS,
    )


def _is_ignored_domain(message: EmailMessage, account: Account) -> bool:
    domain = message.sender.domain
    if not domain:
        return False
    if domain in SETTINGS.ignored_domains:
        return True
    # Only treat "same domain as the mailbox" as internal for real company
    # domains — never for gmail.com and friends.
    if domain in FREE_MAIL_DOMAINS:
        return False
    return domain == account.domain or domain in SETTINGS.user_domains


def seed_known_senders(
    *,
    since: timedelta = timedelta(days=180),
    max_results: int = 1000,
    account_ids: list[str] | None = None,
    store: Store | None = None,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Mark everyone you've corresponded with recently as already-known.

    Run once before the first real scan, otherwise every long-standing contact
    looks brand new. Walks both inbox and sent mail — having written to someone
    is the strongest signal of an existing relationship.
    """
    store = store or Store()
    say = progress or (lambda _msg: None)
    tools.set_caller("pipeline.seed")
    accounts = load_accounts()
    if account_ids:
        wanted = set(account_ids)
        accounts = [a for a in accounts if a.account_id in wanted]

    seeded = 0
    for account in accounts:
        try:
            with build_provider(account) as provider:
                for folder in (Folder.INBOX, Folder.SENT):
                    say(f"Reading {folder.value} mail in {account.email or account.account_id}")
                    query = MessageQuery.recent(since, max_results=max_results, folder=folder)
                    try:
                        # Through the same gate as a scan. Seeding reads *more*
                        # mail than a scan does -- months of both inbox and sent
                        # -- so leaving it ungated meant the largest read in the
                        # system was also the only unscoped, unaudited one.
                        messages = tools.gmail_read(
                            account_id=account.account_id,
                            folder=folder.value,
                            max_results=max_results,
                            _fetch=lambda q=query: provider.fetch(q),
                        )
                    except tools.ToolDenied as exc:
                        log.warning("[%s] %s seed read denied at %s: %s",
                                    account.account_id, folder.value, exc.gate, exc.reason)
                        continue
                    except ProviderError as exc:
                        log.warning("[%s] %s folder unavailable: %s", account.account_id, folder.value, exc)
                        continue
                    seeded += _seed_from(messages, folder, account, store)
        except Exception:
            log.exception("[%s] seeding failed", account.account_id)

    log.info("Seeded %d records; %d unique senders known", seeded, store.sender_count())
    return seeded


def _seed_from(
    messages: list[EmailMessage], folder: Folder, account: Account, store: Store
) -> int:
    seeded = 0
    for message in messages:
        if folder is Folder.INBOX:
            if not message.sender.email or message.sender.email == account.email:
                continue
            store.record_sender(message, user_id=account.user_id)
            seeded += 1
            continue

        # Sent mail: the counterparties are the recipients, not the sender.
        for recipient in message.to + message.cc:
            if not recipient.email or recipient.email == account.email:
                continue
            store.record_sender(
                message.model_copy(update={"sender": recipient}), user_id=account.user_id
            )
            seeded += 1
    return seeded
