# MCP — using the agent from Claude and ChatGPT

The whole pipeline is available as an [MCP](https://modelcontextprotocol.io)
server: Claude (Desktop, Code, claude.ai) and ChatGPT can scan the inbox,
inspect leads, research companies, check the calendar and draft briefs by
calling tools, instead of you switching to the web UI.

What does **not** change is who decides. Every MCP tool is a thin wrapper over
the same pipeline the web UI calls, so the [six-gate tool
harness](README.md#the-tool-harness) still sits between any model and any
side effect. A Claude or ChatGPT conversation can *ask* for `deliver_brief`;
it is refused at the scopes gate unless `.env` granted `brief:deliver`, and
refused again unless the recipient is in `ALLOWED_RECIPIENTS` — exactly as
from the browser. No connected model can widen a scope, because the scope set
is never in any model's context.

## The tools

| Tool | Pipeline step | Writes? |
|---|---|---|
| `get_status` | — mailboxes, granted scopes, counts, last scan | no |
| `get_audit_log` | — recent gated calls, gate trails, denials | no |
| `scan_inbox` | 1 — read new mail, triage for new customers/partners | records triage |
| `triage_messages` | 1 — triage mail the *client* fetched (see below) | records triage |
| `list_leads` | 1 — triaged leads already in the database | no |
| `seed_known_senders` | 0 — mark existing contacts as known (run once) | records senders |
| `research_company` | 2 — profile + news + meeting prep, with sources | caches research |
| `get_research` | 2 — cached research, free and instant | no |
| `lookup_calendar` | 3 — upcoming meetings with a company | no |
| `generate_brief` | 4 — assemble and save a brief | saves a draft |
| `list_briefs` / `get_brief` | 5 — the review queue | no |
| `approve_brief` / `reject_brief` | 5 — record the human's decision | status only |
| `deliver_brief` | 5 — send an approved brief | **gated**: scope + allow-list |
| `search` / `fetch` | — ChatGPT's connector contract, over the same store | no |

Scans default to triage-only (`research=false`): research takes about a minute
per company, and a tool call that runs for five minutes is a tool call the
client times out on. Triage the scan, then research the leads you care about.

## Reusing what the client already has

Claude usually arrives with its own mailbox and calendar access — the Gmail
and Google Calendar connectors on claude.ai and Claude Desktop. Where that is
true, this app does not need to be a second reader of the same inbox:

- **Mail** — Claude fetches messages with its own Gmail connector and hands
  them to `triage_messages`. Same triage model, same known-sender dedupe,
  same gated store write as `scan_inbox`; the results land in the same lead
  store, so `research_company`, `generate_brief` and the rest work on them
  unchanged. The bridge accepts up to 50 messages a call — sender, subject,
  body — and skips senders the store already knows unless told otherwise.
- **Calendar** — Claude can check upcoming meetings with its own Calendar
  connector and simply say so in the conversation; `lookup_calendar` exists
  for clients that cannot.

The server's instructions tell connected models this, so in practice you can
just ask: *"check my Gmail for new business contacts and triage them"* — and
Claude will pick its own connector for the reading and this server for the
pipeline. `scan_inbox` remains the right tool when the app has its own
mailbox credentials (IMAP app password, headless servers, scheduled scans) or
when the client has no mail access of its own.

## Two transports

| Transport | Command | For |
|---|---|---|
| **stdio** | `./start.sh mcp` | clients on this machine that launch the process themselves: Claude Desktop, Claude Code, any local MCP host |
| **streamable HTTP** | `./start.sh mcp --http` | remote clients: claude.ai custom connectors, ChatGPT connectors — behind a tunnel, never an open port |

stdio needs no port, no token and no tunnel: the client starts the process,
the OS user account is the authentication, and nothing listens anywhere. Use
it whenever the client runs on the same machine as your mail data — which is
the design's home case.

HTTP binds `127.0.0.1:8766` and speaks MCP at `/mcp`. Exposing it works
exactly like exposing the web UI ([DEPLOYMENT.md](DEPLOYMENT.md)): set
`PUBLIC_HOSTS`, put a Cloudflare tunnel in front, and the server refuses any
other hostname with `421` before the protocol is even spoken. Same allowlist,
same wildcard rules, different door.

---

## Claude Code

```bash
claude mcp add companies-research \
  --env PYTHONPATH=/path/to/Companies-Research-Agent/src -- \
  /path/to/Companies-Research-Agent/.venv/bin/python -m companies_research mcp
```

Two things matter, because the client launches this from its own working
directory, not the repo's: the venv's own python (so the `mcp` package and
the app's dependencies are there), and `PYTHONPATH` pointing at `src/` (the
package lives there and is not installed into the venv — the same reason
start.sh sets it). Everything else self-locates: `.env`, the database and
credentials are anchored to the repo the module lives in, not to the working
directory.

If `claude mcp add` reports the module missing, install it in the venv first:
`.venv/bin/pip install -r requirements.txt` (the `mcp` package was added to
requirements alongside this feature).

## Claude Desktop

Settings → Developer → Edit Config, then add (with your real path):

```json
{
  "mcpServers": {
    "companies-research": {
      "command": "/path/to/Companies-Research-Agent/.venv/bin/python",
      "args": ["-m", "companies_research", "mcp"],
      "env": {
        "PYTHONPATH": "/path/to/Companies-Research-Agent/src"
      }
    }
  }
}
```

Restart Claude Desktop; the tools appear under the 🔌 menu. Everything runs
locally — Desktop launches the process next to your data, which is the
arrangement this app is built around.

## claude.ai (web) — custom connector

claude.ai cannot launch a local process, so this is the HTTP transport behind
a tunnel — the same recipe as the demo URL in DEPLOYMENT.md:

```bash
# .env
PUBLIC_HOSTS=*.trycloudflare.com
MCP_AUTH_TOKEN=<something long and random>    # openssl rand -hex 32

./start.sh mcp --http                              # terminal 1
cloudflared tunnel --url http://127.0.0.1:8766     # terminal 2 → prints the URL
```

Then claude.ai → Settings → Connectors → **Add custom connector** → URL
`https://<random-words>.trycloudflare.com/mcp`. If the connector dialog offers
no header field for the token, put [Cloudflare
Access](https://developers.cloudflare.com/cloudflare-one/) in front of the
hostname instead (named tunnel + an email-OTP allowlist of just you) and leave
`MCP_AUTH_TOKEN` unset — the outer wall then does what the token would have.

Quick-tunnel URLs die with the process and change every run; for anything
longer than a session, use a named tunnel on your own domain (Option B in
DEPLOYMENT.md).

## ChatGPT

ChatGPT only speaks to remote servers, so the tunnel setup above applies
verbatim. Two ways in:

- **Developer mode** (Settings → Apps & Connectors → Advanced → Developer
  mode) gets the full toolset: scan, research, briefs, all of it.
- **Plain connectors / deep research** get only `search` and `fetch` — that
  is ChatGPT's contract for those surfaces, and both are implemented over the
  same store: `search` finds leads, research and briefs; `fetch` returns one
  of them in full.

ChatGPT's connector UI authenticates with OAuth or not at all — there is no
field for a bearer token. So for ChatGPT specifically: either treat the
tunnel URL as the secret (quick tunnel, killed after the session), or front a
named tunnel with Cloudflare Access as above. Do not leave an unauthenticated
tunnel up overnight because the URL "looks random".

---

## What a connected model can and cannot do

Can: read triaged leads, read cached research and briefs, trigger a scan,
trigger research (spends your API budget — the rate limits in the harness
still apply), record an approve/reject decision, and — only if you granted
`brief:deliver` *and* allow-listed the recipient — deliver a brief.

Cannot: send, delete or move mail (no tool exists); read raw message bodies
(the store keeps triage summaries, not bodies); widen its own scopes; deliver
to an address outside `ALLOWED_RECIPIENTS`; overwrite an approved brief; or
reopen a delivered one. Denials come back as structured refusals the model is
told to accept and move on from.

One honest caveat: `approve_brief` exists so you can run the whole review
from a chat. The tool's description tells the model to call it only on your
say-so, but a description is advice, not a gate — if that worries you, keep
approvals in the web UI and simply don't mention approval in chat; delivery
stays double-gated either way.

## Pre-connect checklist (remote transports only)

- [ ] `MCP_AUTH_TOKEN` set (or Cloudflare Access in front) **before** the
      tunnel goes up — anyone who reaches the endpoint reads what the agent
      has read.
- [ ] `PUBLIC_HOSTS` names exactly the hostname you are exposing; delete it
      after.
- [ ] `TOOL_SCOPES` does **not** include `brief:deliver` unless delivery is
      the point — and then `ALLOWED_RECIPIENTS` is your own address only.
- [ ] Tunnel killed when you are done (`Ctrl+C`); `mcp --http` stays loopback
      the moment the tunnel dies.
