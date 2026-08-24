# Chat — this agent, spoken to from a terminal

`chat` is an interactive interface to the companies-research agent itself: the
leads it found in your mail, the companies it researched, the briefs it wrote,
your upcoming meetings, your Google Drive documents, and a long-term memory
that indexes all of it. Every tool call passes the same six gates as the rest
of the codebase (schema → auth → scopes → rate limit → audit → execute), so
the ProtonX course scenarios fall out of the agent's own machinery rather than
sitting beside it.

## Prerequisites

- **Ollama running** with a tool-calling chat model and an embedding model
  (both already installed here):

  ```sh
  ollama pull qwen3-coder:latest    # chat + tool calls (OLLAMA_MODEL)
  ollama pull nomic-embed-text      # memory embeddings (OLLAMA_EMBED_MODEL)
  ```

- **Drive consent, once** — Drive uses *your own* Google account through the
  same installed-app OAuth flow as Gmail and Calendar, cached as a separate
  Drive-only token so the mail consent is never touched:

  ```sh
  PYTHONPATH=src .venv/bin/python -m companies_research auth --drive
  ```

  A browser opens; approve read-only Drive access. (Enable the Drive API for
  the Cloud project behind `credentials/client_secret.json` first if it never
  has been.) For headless deployments, set `GOOGLE_SERVICE_ACCOUNT_FILE` to a
  service-account JSON and share a folder with its address instead.

Everything runs locally: chat on Ollama, embeddings on Ollama, memory in
`data/agent.db`. Nothing needs Anthropic credit; `chat --backend anthropic`
switches to Claude when there is credit to spend.

## Starting it

```sh
cd ~/Documents/GitHub/Companies-Research-Agent
PYTHONPATH=src .venv/bin/python -m companies_research chat            # interactive
./start.sh chat                                                       # same thing
PYTHONPATH=src .venv/bin/python -m companies_research chat -m "..."   # scripted; one -m per message, in order
```

In-session commands (they also work as `-m` arguments): `/audit`, `/memory`,
`/index`, `/tools`, `/clear`, `/help`, `/quit`.

## The agent's own capabilities in chat

```text
You: Đã có những lead nào gần đây?              → list_leads (from scanned mail)
You: Cho tôi biết về công ty Agora               → get_research (cached profile)
You: Có brief nào đang chờ duyệt không?          → list_briefs
You: Tuần tới có họp với công ty nào không?      → lookup_calendar
You: /index    # embeds the agent's research + briefs into memory, so
               # search_memory answers from what the agent already knows
```

## The course scenarios

| # | Kịch bản | Tool | Try it |
|---|----------|------|--------|
| 1 | Liệt kê file trong Drive | `list_drive_files` | `chat -m "Liệt kê các file trong Drive"` |
| 2 | Đọc và hiển thị nội dung file X | `read_drive_file` (MarkItDown) | `chat -m "Đọc và hiển thị nội dung file <tên file>"` |
| 3 | "Hãy nhớ rằng tôi thích Python" | `save_memory` | `chat -m "Hãy nhớ rằng tôi thích Python"` |
| 4 | "Tôi thích ngôn ngữ gì?" — **sau khi restart** | `search_memory` | a **new** `chat -m "Tôi thích ngôn ngữ gì?"` — memory is SQLite, a fresh process still answers |
| 5 | "Lưu nội dung file lại" | `save_memory` (chunked into RAG) | `chat -m "Đọc file X rồi lưu nội dung file đó vào bộ nhớ"` |
| 6 | Hỏi khái niệm từ file đã lưu — sau restart | `search_memory` | new process: `chat -m "Theo tài liệu đã lưu, ... là gì?"` |
| 7 | Xem audit log | — | `chat -m /audit` (or `... tools --limit 30`) — every call shows all six gates |
| 8 | Phân quyền | scopes gate | `chat --role user -m "Hãy nhớ rằng tôi thích cà phê"` → `save_memory` **DENIED at scopes**; default `--role admin` succeeds |

## What was learned vs. how this system does it

The course material demonstrates each concept with the smallest thing that
works; this codebase already had a heavier-duty version of most of them, so
the concepts landed on existing machinery instead of arriving as new code:

| Concept (course) | Here |
|---|---|
| 6-step tool pipeline, audit as an in-memory list | the existing six-gate registry; audit is the `tool_calls` SQLite table storing argument *hashes*, never values |
| API-key user DB for admin/user roles | `--role` maps onto `TOOL_SCOPES` — the same scope set that gates the whole agent, enforced outside the model's context |
| Qdrant + OpenAI embeddings for memory | a `memories` table beside the agent's other state + local `nomic-embed-text`; `/index` folds the agent's own research and briefs into the same corpus |
| a fresh Anthropic agent loop | `agents/chat.py` on the triage backend switch (Ollama default, Anthropic optional), with langfuse generation spans like every other model call here |
| Drive via a course-provided service account | your own Google identity via this repo's OAuth flow (`auth --drive`, separate token); service account kept as the headless option |
| file content straight into context | Drive content is fenced with `render_untrusted` random-tag blocks and advisory NeMo screening — the same posture triage gives stranger-writable email |
| prompt as a constant | served through `prompts.load("chat", …)`, so file overrides and Langfuse prompt management apply |

The same drive/memory tools are also on the MCP surface (`mcp_server.py`), so
Claude or ChatGPT connected over MCP sees them too. Tests:
`tests/test_chat_demo.py`.
