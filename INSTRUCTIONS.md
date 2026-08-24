# Operating instructions — models and research

Standing policy for which model does what, set 2026-08-24. The `.env` in this
repo is already configured to match; this file explains the knobs so the setup
survives a fresh checkout.

## The split: local LLM for everything, Claude API for web research only

| Work | Where it runs | Setting |
|---|---|---|
| Email triage | Local Ollama | `TRIAGE_BACKEND=ollama`, `OLLAMA_MODEL=qwen3-coder:latest` |
| Chat demo (Drive + memory) | Local Ollama | same `OLLAMA_MODEL` |
| Guardrails screening | Local Ollama | on by default, advisory mode |
| Memory embeddings | Local Ollama | `OLLAMA_EMBED_MODEL=nomic-embed-text` |
| **Company web research** | **Claude API** | `RESEARCH_PROVIDER=claude_web`, `RESEARCH_MODEL=claude-opus-5` |

Rationale: mail bodies and memories never leave the machine; the only thing
sent to a hosted API is a company name/domain for web research, which needs
server-side web search that local models cannot do.

## 30-day research cache — never recompute fresh research

`RESEARCH_TTL_DAYS=30` in `.env`. The pipeline keys research by company
domain and checks the store before calling the Claude API
([pipeline.py](src/companies_research/pipeline.py) `research_leads`):

- A domain researched **within the last 30 days** is served from the SQLite
  cache — zero API calls, zero cost. Several people mailing from one company
  cost one lookup.
- A recent *failed* lookup is also not retried until it ages out.
- To deliberately refresh one company inside the window:
  `./start.sh research <domain> --force`

## Switching / testing local models

Installed in Ollama (see `ollama list`):

- `qwen3-coder:latest` (18 GB) — the current default and the accuracy pick
- `llama3.2:3b` (2 GB) — small instruct model, much faster
- `qwen2.5-coder:1.5b-base` (1 GB) — base model, no instruct tuning; kept for
  comparison only, not usable for triage
- `nomic-embed-text` — embeddings only

To switch the live model: change `OLLAMA_MODEL` in `.env` (or in the web UI
settings). `qwen3:8b` — the code default — is **not** installed; `.env` must
keep an installed model name or triage fails at model load.

To measure candidates against each other (same fixtures, same prompt, only the
model varies — accuracy, false positives, injection resistance, latency):

```sh
PYTHONPATH=src .venv/bin/python -m companies_research compare \
  --passes 2 \
  --model ollama:qwen3-coder:latest \
  --model ollama:llama3.2:3b
```

Results land in `compare_results.json`. Rank on the **injection** column
first, then false positives on the negative class, then accuracy — the
comparison's own docstring explains why accuracy alone misleads.

## Caveats

- The Anthropic API credit balance was exhausted on 2026-08-24 — hosted-model
  rows in `compare` and live research fail with "credit balance is too low"
  until topped up. The 30-day cache means existing research keeps being served
  regardless.
- Never add hosted models to the triage path: `SETTINGS.is_local_triage` must
  stay `True` for the privacy story above to hold.
