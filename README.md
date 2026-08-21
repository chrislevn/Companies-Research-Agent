# Companies Research Agent

Every morning the agent reads new mail, spots emails from **new customers or partners**,
researches those companies (website + Google News), checks the calendar for upcoming
meetings with them, and drafts a short brief for you to approve before it is forwarded
or filed into your knowledge base.

It runs entirely on your own computer. There is a point-and-click interface for everyday
use, and a command line for servers and scheduled jobs.

## Pipeline

| Step | Status | What it does |
|---|---|---|
| 1. **Read email & triage** | ✅ built | Fetch new mail from any provider, filter noise, classify who is a new customer/partner |
| 2. **Company research** | ✅ built | Company profile, products, recent news and meeting prep, with sources |
| 3. Calendar lookup | ⬜ | Find upcoming meetings with that company |
| 4. Brief generation | ⬜ | Company profile, products, recent news, contact, meeting prep notes |
| 5. Human approval | ⬜ | Review → forward by email or save to the knowledge base |

This repo currently implements **steps 1 and 2**.

---

## Getting started

**macOS** — double-click `start.command`.
**Windows** — double-click `start.bat`.
**Linux** — run `./start.sh`.

The first run installs what it needs (a minute or two) and then opens the interface in
your browser. If Python is missing it tells you where to get it.

Everything else happens on screen, in English or Vietnamese:

**1. Connect your email.** Two ways, and the first one is much easier:

- **App password** — pick your provider, type your address, paste an app password.
  The interface links straight to the page where you generate one. Works with Gmail,
  Outlook, Yahoo, iCloud, Zoho, Fastmail and company mail servers.
- **Sign in with Google** — no password to manage, and it is what will let the agent
  read your calendar in step 3. It needs a one-off Google Cloud setup, which the
  interface walks through step by step.

**2. Add a Claude API key.** [console.anthropic.com](https://console.anthropic.com/settings/keys) →
API keys → create one → paste it in. The key is checked before it is saved. Triage is a
cheap classification, so expect a few cents a day for a normal inbox.

**3. Let it learn your contacts.** It reads through your past conversations once so that
people you already deal with are not reported as new. Skip this and every long-standing
contact looks like a fresh lead on the first check.

Then press **Check my email now**. New customers and partners appear as cards with the
company, who wrote, what they want, and whether they mentioned a meeting.

The agent only ever *reads* your mail. It never sends, deletes or moves anything.

### Running it every morning

The interface checks on demand. To have it run unattended, use the command line from
your system scheduler:

```cron
0 7 * * * cd /path/to/Companies-Research-Agent && ./start.sh scan --since 1d >> data/scan.log 2>&1
```

---

## Command line

Everything the interface does is also available without it:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=src

python -m companies_research            # open the web interface (default)
python -m companies_research accounts   # list mailboxes  (--check to test each)
python -m companies_research auth       # interactive sign-in
python -m companies_research seed       # learn existing contacts, run once
python -m companies_research scan --since 1d
python -m companies_research research agora.io   # one company (--force to refresh)
python -m companies_research research            # every lead not yet researched
python -m companies_research prompts --show      # which prompts are in use
python -m companies_research purge <user_id>
```

With no `accounts.json`, the CLI falls back to a single Gmail mailbox described by
`.env` — see `.env.example`. The interface always writes `accounts.json`, so the two
stay interchangeable.

## Multiple mailboxes / multiple providers

Add mailboxes in **Settings → Mailboxes**, or write
[`accounts.json`](accounts.example.json) by hand for a server deployment.

| Provider | `provider` | Auth options |
|---|---|---|
| Gmail / Google Workspace | `gmail` | `oauth_desktop` (browser, per user) · `service_account` (domain-wide delegation) |
| Microsoft 365 / Outlook | `microsoft` | `device_code` (interactive) · `client_credentials` (app-only, admin consent) |
| Everything else | `imap` | app password · OAuth2 `XOAUTH2` token |

`imap` covers Zoho, Fastmail, iCloud, Yahoo, on-prem Exchange and self-hosted mail.

Secrets in `accounts.json` are **references**, never literals — `env:MS_CLIENT_SECRET`
or `file:/run/secrets/token`. Passwords typed into the interface are written to `.env`
(mode 600) and referenced the same way, so `accounts.json` carries no secret material
and is safe in a config map or a private repo.

One account failing never stops the others: the error is reported per account and the
scan continues.

### Enterprise notes

- **Google:** if everyone is in your own Workspace tenant, set the OAuth consent screen
  to **Internal** — no test-user cap and no CASA security assessment, which external
  publishing of the restricted `gmail.readonly` scope would require. Then use
  `service_account` auth so there is no per-user consent and no refresh token to store.
- **Microsoft:** `Mail.Read` as an *application* permission grants access to every
  mailbox in the tenant. Restrict it with an Exchange Online application access policy
  (`New-ApplicationAccessPolicy`) scoped to a mail-enabled security group.
- **Data:** message bodies are sent to the Anthropic API for triage. Quoted reply chains
  and attachments are stripped first, and bodies are truncated, but this still needs a
  DPA and possibly a zero-data-retention agreement before rollout.
- **Erasure:** `python -m companies_research purge <user_id>` deletes everything stored
  for one person; `Store.purge_older_than()` implements a retention window.
- **The web interface is for a single operator.** It binds to `127.0.0.1` and is gated
  by a per-run token, which is enough to stop another website driving it, but it has no
  user accounts. Do not expose it on a network — run the CLI on servers.

## How "new" is decided

Two layers, cheap-first:

1. **Deterministic filters** ([`pipeline._skip_reason`](src/companies_research/pipeline.py)) —
   mail you sent, your own or explicitly ignored domains, no-reply/bulk senders
   (`List-Unsubscribe`, `List-Id`, `Precedence` headers), already-processed messages,
   and senders already in the local database (matched by address *or* domain).
   Consumer domains like `gmail.com` are never treated as "your own domain", so a solo
   founder on Gmail still sees Gmail-based leads.
2. **Claude triage** ([`agents/triage.py`](src/companies_research/agents/triage.py)) —
   survivors are batched 10 at a time and classified with structured outputs into
   `TriageResult`: relationship type, company name and domain, contact person and title,
   intent, whether a meeting is mentioned, and `should_research`.

Anything the model can't classify comes back as a low-confidence `unknown` rather than
being dropped silently.

## Company research (step 2)

Every lead with a company domain gets a profile: what the company does, its products,
recent news, meeting prep notes, and the URLs each finding came from. Claude's hosted
web search and fetch do the retrieval, so there is no scraper here to break when a site
changes its markup.

Three things keep the bill down:

- **Cached by domain**, not by message — several people writing from one company cost one
  lookup, and a profile is reused for `RESEARCH_TTL_DAYS` (14 by default).
- **Capped per scan** at `RESEARCH_MAX_COMPANIES`; the rest wait for the next run, and the
  run says so rather than truncating silently.
- **Failures are cached briefly** (6 hours) so a transient outage does not get retried on
  every scan.

`--dry-run` skips research entirely, so re-running the same mail while tuning costs
nothing.

**It is slow and it is not free.** A thorough lookup runs several searches and page
fetches and can take minutes for one company; searches and fetches are billed per use on
top of tokens. `RESEARCH_EFFORT=low` with `RESEARCH_MAX_SEARCHES=3` is a large speedup
for a modest loss of depth — worth trying before raising either.

Unlike triage, research never sees your mail: it takes a company name and a public
domain, which is why it runs on a hosted model even when triage is local.

## The tool harness

Every capability the agent has — reading mail, searching the web, writing to the local
store — goes through one chokepoint in
[`tools/registry.py`](src/companies_research/tools/registry.py). Six gates run in a fixed
order and each records a named boolean:

```
schema -> auth -> scopes -> rate_limit -> audit -> execute
```

The audit row is written **before** the tool runs, so a crash still leaves evidence of
the attempt. A denial raises `ToolDenied`, which the caller turns into a structured
refusal the model can read — it never ends a scan.

```bash
./start.sh tools              # scopes, tools, and the recent audit trail
./start.sh tools --denied     # only refused calls
```

Why a gate and not a prompt filter: a filter is a model-level control, and model-level
controls lose to model-level attacks. An injected instruction may well persuade the model
to ask for a capability. It cannot grant the process a scope, because `TOOL_SCOPES` comes
from `.env` and is never in the model's context. Revoke `research:read` and the hosted
search tool is not even declared in the request — the capability is absent rather than
discouraged.

Argument models use `extra="forbid"` and carry identifiers only, never message bodies:
the registry hashes the arguments and throws the values away, so an audit row can never
become a second copy of your mail.

## Customising the prompts

What makes a *useful* brief is a judgement call — it depends on your industry, your
market and who reads it. Both agent prompts are editable, and neither needs a code
change or a restart.

```bash
./start.sh prompts                     # what's in use, and where it comes from
./start.sh prompts research --show     # print the active text
./start.sh prompts research --write    # copy the built-in one to prompts/research.md
```

Edit `prompts/research.md` and the next company picks it up; delete the file to return
to the default. `prompts/triage.md` works the same way.

For a house rule you want *added* to the built-in prompt rather than replacing it, use
the environment instead — no file to keep in sync when the default improves:

```ini
RESEARCH_PROMPT_EXTRA=We sell to logistics firms — always check fleet size.
TRIAGE_PROMPT_EXTRA=Bank and utility notifications are `automated`, never `customer`.
```

An empty or unreadable prompt file falls back to the built-in one with a warning rather
than sending the model an empty system prompt.

## Layout

```
src/companies_research/
├── config.py          # .env-driven settings, OAuth scopes
├── accounts.py        # solo fallback ⟷ accounts.json
├── models.py          # EmailMessage, TriageResult, Relationship
├── mime.py            # HTML→text, addresses, signatures, quoted-reply stripping
├── secret_refs.py     # env:/file: secret indirection
├── store.py           # SQLite: known senders, processed messages, migrations
├── schema_utils.py    # Pydantic → structured-outputs JSON Schema
├── prompts.py         # user-editable prompts: file overrides, env appends
├── pipeline.py        # scan / seed across all accounts
├── cli.py             # argparse entry point
├── google_auth.py     # Google OAuth desktop flow + token cache
├── providers/
│   ├── base.py        # EmailProvider, MessageQuery, Account
│   ├── gmail.py       # Gmail API
│   ├── microsoft.py   # Microsoft Graph
│   └── imap.py        # generic IMAP
├── agents/
│   ├── triage.py      # the triage agent — prompt, batching, fallbacks
│   └── backends.py    # where it runs: Anthropic API or local Ollama
├── research/
│   ├── base.py        # ResearchProvider protocol, ResearchOutcome
│   └── claude_web.py  # Claude's hosted web search + fetch
└── webapp/
    ├── server.py      # local HTTP API
    ├── mailboxes.py   # adding/removing mailboxes from the UI
    ├── jobs.py        # background scan/seed with live progress
    └── static/        # the interface — no build step
```

Messages are keyed by `provider:account_id:message_id`, and all state is scoped by
`user_id`, so one deployment can watch many people's mailboxes without cross-contamination.

Local state lives in `data/agent.db`; credentials in `credentials/` and `.env`. All
gitignored.

## Configuration

The interface writes these for you; edit `.env` directly only if you prefer.

| Variable | Default | Notes |
|---|---|---|
| `TRIAGE_BACKEND` | `anthropic` | `anthropic` or `ollama` — see [Running triage locally](#running-triage-locally) |
| `TRIAGE_BATCH_SIZE` | 10 / 4 | Emails per model call; defaults to 4 on `ollama` |
| `ANTHROPIC_API_KEY` | — | Optional if you use `ant auth login` or `ANTHROPIC_AUTH_TOKEN` |
| `TRIAGE_MODEL` | `claude-opus-5` | Use `claude-haiku-4-5` for high volume |
| `TRIAGE_EFFORT` | `low` | Triage is a cheap classification; raise if accuracy suffers |
| `OLLAMA_HOST` | `http://localhost:11434` | Only used when `TRIAGE_BACKEND=ollama` |
| `OLLAMA_MODEL` | `qwen3:8b` | Pull it first with `ollama pull` |
| `OLLAMA_NUM_CTX` | `16384` | Ollama's default is too small for a batch and drops overflow silently |
| `OLLAMA_TIMEOUT` | `600` | Seconds before a local batch is given up on |
| `RESEARCH_ENABLED` | `true` | Set false to stop after triage |
| `RESEARCH_MODEL` | `claude-opus-5` | Research always runs on a hosted model |
| `RESEARCH_EFFORT` | `medium` | `low` is much faster and cheaper per company |
| `RESEARCH_MAX_SEARCHES` | `8` | Billed per search and per fetch |
| `RESEARCH_MAX_COMPANIES` | `10` | Per scan; the rest wait for the next run |
| `RESEARCH_TTL_DAYS` | `14` | How long a cached company profile is reused |

## Running triage locally

Triage is the only step that reads message bodies with a model, so it is the only
step that sends them anywhere. Point it at a model on your own machine and nothing
leaves the box:

```bash
brew install ollama && ollama serve      # or download from ollama.com
ollama pull qwen3:8b
```

```ini
TRIAGE_BACKEND=ollama
OLLAMA_MODEL=qwen3:8b
```

That is the whole change — same prompt, same JSON schema, same `TriageResult`.
Ollama constrains decoding to the schema, so the local path is as parseable as the
hosted one; a model that cannot answer degrades to a low-confidence `unknown`
exactly like an API failure does.

**Choosing a model.** Pick a general *instruct* model, not a coder-tuned one —
coder models mislabel business email and ignore the "reply in the sender's
language" instruction. If your mail is partly Vietnamese, test on real messages
before trusting the labels: relationship and company extraction degrade well
before `should_research` does.

**What it costs you.** Local triage is free and private, and slower and less
accurate. A frontier model reads ten emails per call in a couple of seconds; an
8B local model wants four per call and takes tens of seconds. For a nightly cron
that is irrelevant. For the **Check my email now** button on a large inbox, it is
the difference between a moment and a coffee break.
| `USER_EMAILS` | — | Comma separated; your own mail is skipped |
| `IGNORED_DOMAINS` | — | Your own company, known vendors |
| `SCAN_DAYS` | `1` | How far back each check looks |
| `ACCOUNTS_FILE` | unset | Overrides the default `accounts.json` location |
| `DB_PATH` | `data/agent.db` | |
