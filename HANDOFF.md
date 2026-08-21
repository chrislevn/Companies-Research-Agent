# Research Agent — Project Handoff

**Verified against the working tree on 21 August 2026.**

A local-first agent that reads a mailbox each morning, finds genuine new customers and
partners among the noise, and researches their companies. Steps 1 and 2 of five are
built and running against a real inbox.

| | |
|---|---|
| Repo | `Companies-Research-Agent` |
| Python | 5,129 lines |
| Mailbox | 1 Gmail, live |
| Tests | none |

---

## 1. Where the pipeline stands

| # | Stage | Status | What it does |
|---|---|---|---|
| 1 | Read email & triage | ✅ **built** | Fetch from Gmail / Microsoft Graph / IMAP. Deterministic filters drop bulk mail, then a model classifies survivors into `TriageResult` — relationship, company, contact, intent, `should_research`. |
| 2 | Company research | ✅ **built** | Each lead's domain gets a profile: products, recent news, meeting-prep notes, and the source URL behind every claim. Cached by domain for 14 days. |
| 3 | Calendar lookup | ⬜ | Find upcoming meetings with that company. The Google scope `calendar.readonly` is already requested and consented — nothing reads it yet. |
| 4 | Brief generation | ⬜ | Assemble triage + research + calendar into one document per lead. Most raw material already exists in `CompanyProfile`. |
| 5 | Human approval | ⬜ | Review, then forward by email or file to a knowledge base. Needs `gmail.send`, which means re-consenting the OAuth token. |

---

## 2. The one architectural idea

The same pattern appears three times, and a fourth use is the natural way to add anything
new: **a Protocol, several implementations, one factory reading `.env`.** Understand it
once and the whole codebase opens up.

| Concern | Protocol | Implementations | Selected by |
|---|---|---|---|
| Mail access | `providers/base.py` | `gmail` · `microsoft` · `imap` | `accounts.json` |
| Triage model | `agents/backends.py` | `anthropic` · `ollama` | `TRIAGE_BACKEND` |
| Company research | `research/base.py` | `claude_web` | `RESEARCH_PROVIDER` |

Two conventions hold across all of them:

- **Structured output everywhere.** A Pydantic model goes through
  `schema_utils.json_schema_for()` and the result is enforced by the API server-side, or
  by Ollama's sampler locally. Same `TriageResult` either way.
- **Failures degrade, they don't raise.** A dead backend or an unparseable response
  becomes a low-confidence `unknown`, so one bad batch never kills a scan.

### Cheap-first filtering

Before any model runs, `pipeline._skip_reason()` drops mail you sent, ignored domains,
bulk senders (`List-Unsubscribe`, `List-Id`, `Precedence`), already-processed messages,
and known senders. On this mailbox that filter caught **30 of 30** and **20 of 20**
messages in normal windows — the LLM never ran. Real model volume is far lower than
inbox size suggests, which matters for both cost and privacy.

### Layout

```
src/companies_research/
├── config.py          # .env-driven settings, OAuth scopes
├── accounts.py        # solo fallback ⟷ accounts.json
├── models.py          # EmailMessage, TriageResult, CompanyProfile, NewsItem
├── mime.py            # HTML→text, addresses, signatures, quoted-reply stripping
├── prompts.py         # user-editable prompts: file overrides, env appends
├── secret_refs.py     # env:/file: secret indirection
├── store.py           # SQLite: senders, processed messages, research cache, migrations
├── schema_utils.py    # Pydantic → structured-outputs JSON Schema
├── pipeline.py        # scan / seed / research_leads across all accounts
├── cli.py             # argparse entry point
├── google_auth.py     # Google OAuth desktop flow + token cache
├── providers/         # gmail.py · microsoft.py · imap.py · base.py
├── agents/
│   ├── triage.py      # prompt, batching, fallbacks
│   └── backends.py    # where it runs: Anthropic API or local Ollama
├── research/
│   ├── base.py        # ResearchProvider protocol, ResearchOutcome
│   └── claude_web.py  # Claude's hosted web search + fetch
└── webapp/            # server.py · jobs.py · mailboxes.py · watcher.py · static/
```

---

## 3. State on this machine

| Setting | Value | Note |
|---|---|---|
| Mailbox | `<your-gmail>` | Gmail OAuth, working |
| Known senders | 304 | Seeded 19 Aug |
| Processed messages | 9 | Last scan 21 Aug |
| Companies researched | 1 | `agora.io` |
| Triage backend | `anthropic` / `claude-opus-5` | **not local** — see below |
| Research | `claude_web` / `claude-opus-5` | effort `medium`, 8 searches |
| Ollama | running, 3 models | all coder-tuned |

### 🛑 Contradiction worth resolving first

The user explicitly asked for local-only triage *"to save cost and security"*, and the
Ollama backend was built and verified for it. But `.env` has **no `TRIAGE_BACKEND` line**,
so triage still runs on the Anthropic API and **mail bodies still leave the machine**.
Every local run so far was a one-off environment override.

Flipping it is two lines in `.env` — but read the next section first.

### ⚠️ No local model is fit for this yet

The three pulled models are `qwen3-coder:30b`, `qwen2.5-coder:1.5b-base`, and an
embedding model. Coder-tuned models mislabel business email, and the 1.5b is a *base*
model that won't follow instructions at all.

Observed with the 30B:

- A bank transaction receipt classified `customer` at **confidence 0.95**
- The same sender labelled three different ways across three batches (`vendor`,
  `vendor`, `partner`) with company alternating between two names
- English mail answered in Vietnamese, against an explicit prompt instruction

Confident wrong labels are the worst failure mode, because `confidence` is exactly what
downstream steps would trust. `OLLAMA_MODEL` defaults to `qwen3:8b`, which is **not
pulled**.

---

## 4. Measurements, not estimates

Everything below was observed on this machine against this mailbox.

| Operation | Configuration | Time | Cost signal |
|---|---|---|---|
| Research, one company | effort `medium`, 8 searches | 331 s | 8 search + 4 fetch |
| Research, one company | effort `low`, 3 searches | 79 s | 1 search + 3 fetch |
| Research, cached | within 14-day TTL | 0.76 s | 0 calls |
| Triage, 11 emails | local, qwen3-coder 30B, batch 4 | 62.6 s | 0 calls |
| Re-scan, same mail | after a real (non-dry) run | 6.0 s | 0 calls |

Dropping research effort from `medium` to `low` was **4× faster for a 0.03 confidence
loss** (0.78 → 0.75). The thorough run found nothing on headcount or founding year that
the cheap run missed — both correctly left those fields empty rather than inventing them.

At defaults, a scan surfacing ten new companies could take close to an hour.

> These are single observations on one machine and one mailbox, not benchmarks. Treat
> them as orders of magnitude.

---

## 5. Traps that will cost an hour each

- **`--dry-run` defeats deduplication.** It deliberately skips `mark_processed`, so
  repeated dry runs re-classify the same mail forever. Deduplication works — it just
  needs one real run to record anything.
- **A normal scan window finds nothing to classify.** This inbox is almost entirely
  newsletters. To exercise triage you need a Gmail query that bypasses the category
  filters:
  ```
  --query "in:inbox -category:promotions -category:updates -category:social -category:forums newer_than:60d"
  ```
- **The seed cap silently under-seeds.** `seed_known_senders` stops at 1,000 messages
  per folder against a 192,000-message inbox, and `--max-results` is **not exposed** on
  the `seed` command. Sent mail seeds well (1,676 total); the inbox does not.
- **Ollama's default context is too small** for a batch plus the schema, and it drops the
  overflow silently. `OLLAMA_NUM_CTX` defaults to 16384 here for that reason.
- **Nothing is committed.** Git holds one initial commit; `src/` is still untracked.
  There are no tests anywhere in the repo.

---

## 6. Open decisions

Each of these was deliberately left to the user. Confirm rather than assume.

### Pull a general instruct model, or keep triage hosted?
**Recommended:** `ollama pull qwen3:8b`, then re-run the same emails with `--reprocess`
for a direct comparison. Until a non-coder model is tested, local triage is not
trustworthy enough to switch on, and the stated privacy goal stays unmet.

### Lower the research defaults?
**Recommended:** `RESEARCH_EFFORT=low` and `RESEARCH_MAX_SEARCHES=3`. Four times faster
for a rounding error of confidence. Raise per company only when a profile comes back thin.

### Parallelise research?
It currently runs sequentially, one company at a time. Fanning out would cut wall-clock
roughly linearly and is the single biggest speedup available — but it multiplies burst
API usage, so it is a cost decision, not just an engineering one.

### Which UI controls matter?
`/api/state` already reports live `triage` and `research` blocks, but the web interface
has no control to switch backends, choose a local model, trigger research, or edit
prompts. All of it is CLI- or `.env`-only today.

### Should `prompts/` be version-controlled?
Currently **not** gitignored, on the reasoning that an edited prompt is configuration
worth keeping rather than a secret. Easy to reverse.

---

## 7. Suggested next steps

1. **Resolve the local-model question** — it blocks the stated privacy goal and every
   judgement about triage quality.
2. **Commit the work.** Roughly 5,000 lines sit untracked. Everything after this is
   harder to review without a baseline.
3. **Build step 3, calendar lookup.** Smallest remaining step, the OAuth scope is already
   consented, and step 4 needs its output. Google Calendar `freebusy` and events search,
   keyed on the lead's domain, matched against attendee addresses.
4. **Then step 4, brief generation** — largely an assembly and formatting job over data
   that already exists.
5. **Add tests around `_skip_reason` and `schema_utils`.** Both are pure, both are
   load-bearing, neither has a single test.

> **Ask before building step 5.** It requires `gmail.send`. That changes the agent from
> strictly read-only — a property the README states twice as a feature — and forces the
> OAuth token to be deleted and re-consented. Confirm the user wants that before touching
> scopes.

---

## 8. Running it

`./start.sh` handles the virtualenv and forwards both arguments and environment
variables, so nothing needs activating.

```bash
./start.sh                          # web interface, 127.0.0.1:8765
./start.sh -v scan --since 1d       # scan with verbose live log
./start.sh scan --since 1d --dry-run --no-research
./start.sh research agora.io        # one company (--force to refresh)
./start.sh research                 # every lead not yet researched
./start.sh prompts --show           # which prompts are active
./start.sh accounts --check         # verify the mailbox connects
./start.sh seed                     # learn existing contacts, run once
./start.sh purge <user_id>          # delete everything stored for one person
```

Progress goes to **stderr** and results to **stdout**, so `--json` stays pipeable while
the live log still shows in the terminal.

### Key configuration

```ini
# Where triage runs
TRIAGE_BACKEND=anthropic        # or: ollama
TRIAGE_BATCH_SIZE=10            # defaults to 4 on ollama
OLLAMA_MODEL=qwen3:8b
OLLAMA_NUM_CTX=16384

# Step 2 research (always hosted — it sees public domains, never mail)
RESEARCH_ENABLED=true
RESEARCH_EFFORT=medium          # low is 4x faster
RESEARCH_MAX_SEARCHES=8
RESEARCH_MAX_COMPANIES=10       # per scan
RESEARCH_TTL_DAYS=14

# Prompts
PROMPTS_DIR=prompts
#RESEARCH_PROMPT_EXTRA=We sell to logistics firms — always check fleet size.
#TRIAGE_PROMPT_EXTRA=Bank and utility notifications are `automated`, never `customer`.
```

Prompts are file-overridable in `prompts/` and hot-reload on the next batch;
`*_PROMPT_EXTRA` appends house rules without replacing the defaults. `./start.sh prompts
research --write` scaffolds an editable copy; delete the file to revert.

---

*Every claim about model output quality comes from runs recorded in the session that
produced this document.*
