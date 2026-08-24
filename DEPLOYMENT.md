# Deployment — giving the demo a public URL

This app is deliberately local-first: it holds your mailbox credentials, your
API key and your mail-derived data, so "deploying" it never means handing that
state to a platform. It means **putting a URL in front of the machine it
already runs on**. This document is the review that led to that conclusion,
then the three ways to do it.

## Choosing the platform

| Option | Cost | Effort | URL lifetime | Best for |
|---|---|---|---|---|
| **A. Your laptop + Cloudflare quick tunnel** | free | 5 minutes | while the tunnel runs | a live presentation |
| **B. Your laptop + named tunnel (your domain)** | free (needs a domain on Cloudflare) | ~20 minutes | stable | graders visiting over a week |
| **C. VPS (DigitalOcean droplet) + Docker + tunnel** | ~$6/month | ~30 minutes | stable, always on | an always-on demo |

**Why a tunnel and not an open port.** A tunnel dials *out* to Cloudflare and
gets TLS, a hostname and DDoS shielding for free; the machine needs no inbound
firewall rule at all. An open port on a VPS gets you certificate management,
a reverse proxy to configure, and a public attack surface — for no benefit at
this scale.

**Why not Render / Railway / Fly / serverless.** Reviewed and rejected on
architecture, not fashion:

- State is SQLite files plus a `.env` the web UI **rewrites at runtime** —
  an ephemeral or read-only filesystem loses your settings on every restart.
- One process owns the job runner and the watcher; a platform that scales to
  two instances silently breaks both.
- Google sign-in uses the *desktop* OAuth flow: the consent redirect lands on
  a localhost port next to the app. That never completes on a PaaS dyno.
- The whole point of the design is that mail and credentials stay on hardware
  you control. Uploading them to a build pipeline defeats it.

---

## Before any of them: the two settings that make exposure safe

The server refuses to answer as any hostname it was not told about, and every
API call needs both the page token and a logged-in session. Two consequences:

1. **Set `PUBLIC_HOSTS` in `.env`** to the hostname the demo will arrive as,
   or the tunnel serves nothing but `421 unrecognised host`:

   ```dotenv
   PUBLIC_HOSTS=*.trycloudflare.com     # quick tunnel (random subdomain)
   PUBLIC_HOSTS=demo.your-domain.com    # named tunnel
   ```

2. **Create your account before sharing the URL.** The first signup claims
   the instance; after that, signup is refused unless you set
   `SIGNUP_OPEN=true`. So sign up first, and the login wall does the rest.
   Turn `SIGNUP_OPEN` on only when you want visitors creating accounts — for
   instance while graders exercise the account-creation flow — and know what
   it means: **anyone who can sign up sees what the agent has read and can run
   scans and research against your mailbox.** What visitors cannot do is
   reconfigure or erase: settings, the API key and purge answer only to the
   first account — the one that claimed the instance. For that window, demo
   against a mailbox you are comfortable showing, and turn signup back off
   after (see the checklist at the bottom).

And one behaviour to expect: **setting `PUBLIC_HOSTS` turns Sign in with
Google off entirely** — the button hides and its endpoints answer 404, for
remote visitors and for you at the machine alike. The consent flow opens a
browser next to the server and catches the redirect on the server's own
loopback, which no remote visitor can complete; and because a `Host` header
is attacker-chosen, the gate is deliberately not "local-looking requests
only" but "never while exposed" (checked against the connection's source
address and the setting itself, which nothing remote can spoof). Everyone
signs in with email + password during the demo; remove `PUBLIC_HOSTS`
afterwards and Google sign-in comes back.

---

## Option A — live presentation from your own laptop (recommended)

Nothing to provision. Install cloudflared once:

```bash
brew install cloudflared          # macOS; see Cloudflare docs for others
```

Then, for the demo:

```bash
# 1. Tell the app to expect the tunnel hostname
echo 'PUBLIC_HOSTS=*.trycloudflare.com' >> .env

# 2. Start the app as usual (it stays bound to 127.0.0.1 — the tunnel
#    connects to it locally; nothing listens on a public interface)
./start.sh

# 3. In a second terminal, open the tunnel
cloudflared tunnel --url http://127.0.0.1:8765
```

cloudflared prints a `https://<random-words>.trycloudflare.com` URL. That is
your demo IP: HTTPS, reachable from any network, no account needed. Log in
first, then share it. `Ctrl+C` on cloudflared ends the exposure instantly —
the app keeps running locally.

*Quick tunnels are explicitly ephemeral — Cloudflare may recycle them, and the
URL changes every run. That is a feature for a presentation and a bug for
graders; for a stable URL use Option B or C.*

## Option B — stable URL from your laptop (named tunnel)

Needs a domain whose DNS is on Cloudflare (free plan is fine).

```bash
cloudflared tunnel login                      # one-off browser consent
cloudflared tunnel create cra-demo
cloudflared tunnel route dns cra-demo demo.your-domain.com
```

Set `PUBLIC_HOSTS=demo.your-domain.com` in `.env`, start the app, then:

```bash
cloudflared tunnel run --url http://127.0.0.1:8765 cra-demo
```

`https://demo.your-domain.com` now points at your laptop whenever both
processes are up.

**Optional, and worth it for a multi-day exposure:** put a [Cloudflare
Access](https://developers.cloudflare.com/cloudflare-one/) policy on that
hostname (free up to 50 users). Visitors then authenticate to Cloudflare —
e.g. an email-OTP allowlist of your graders — *before* a single request
reaches the app. That is the right outer wall for the window when
`SIGNUP_OPEN=true`: only people on your allowlist can even reach the signup
form.

## Option C — always-on VPS (DigitalOcean droplet)

For when the demo must survive your laptop's lid. A $6 basic droplet
(1 vCPU / 1 GB, Docker image from the DO marketplace) is enough.

```bash
# on the droplet
git clone <your-fork> Companies-Research-Agent && cd Companies-Research-Agent
cp .env.example .env
# edit .env: ANTHROPIC_API_KEY, PUBLIC_HOSTS=demo.your-domain.com,
#            TUNNEL_TOKEN=<from the Cloudflare dashboard>
docker compose -f docker-compose.deploy.yml up -d --build
```

In the Cloudflare dashboard (Zero Trust → Networks → Tunnels), create the
tunnel, take its token for `.env`, and point its public hostname at
`http://agent:8765` — that is the agent's name on the compose network.

What the compose file already decides for you:

- The agent's port is published to the droplet's **loopback only**; the tunnel
  is the sole way in. No inbound firewall rules besides SSH.
- `.env`, `data/`, `out/`, `credentials/`, `prompts/` are bind mounts — state
  lives in the repo directory on the droplet and survives rebuilds.

Two caveats specific to headless servers:

- **Google sign-in cannot complete there** — the consent flow needs a browser
  next to the app. Either connect the mailbox by **IMAP app password** in the
  UI (works headless, and is the path the setup screen recommends anyway), or
  run `./start.sh auth` on your laptop once and copy `credentials/` to the
  droplet.
- **Self-hosted triage does not fit a $6 droplet.** Ollama would need to run on
  the droplet itself and 1 GB of RAM will not hold a model; vLLM needs a GPU the
  droplet does not have. Leave `TRIAGE_BACKEND=anthropic` on a small VPS — or,
  if you do own a GPU box, set `TRIAGE_BACKEND=vllm` and point `VLLM_BASE_URL`
  at it **over a private network** (WireGuard/Tailscale between the droplet and
  the GPU machine). Message bodies travel to whatever that URL names, so never
  point it across the public internet — and if the server must listen beyond
  loopback, start it with `--api-key` and set `VLLM_API_KEY` to match.
- **Keep it one process.** The container runs a single uvicorn worker, and
  the first-account claim is serialised in-process on that assumption.
  Don't add `--workers N` — if this ever needs to scale past one process,
  the signup claim needs a database-level unique constraint first.

The observability stack (`docker-compose.yml`) can run on the same droplet —
set `METRICS_HOST=0.0.0.0` in `.env` so Prometheus can scrape the agent — but
**do not expose Grafana/Prometheus/Tempo publicly**: those ports (3000, 9090,
3200, 4318) have no authentication by design. Reach them over an SSH tunnel
(`ssh -L 3000:localhost:3000 root@droplet`) or add them as extra hostnames on
the Cloudflare tunnel behind an Access policy.

---

## What actually guards the door

Four independent checks, in the order a request meets them
(`webapp/server.py`):

1. **Host allowlist** — a request arriving as any hostname outside
   localhost + `PUBLIC_HOSTS` is refused (`421`) before even the login page is
   served. This is also what defeats DNS-rebinding.
2. **Origin check** — cross-site `fetch()` from any other origin is refused
   (`403`), same allowlist.
3. **Page token** — every API call must echo the per-run token embedded in the
   page, so a request that never loaded the page cannot drive the API.
4. **Session** — everything beyond login/signup requires a signed-in user
   (`webapp/auth.py`), and signup itself closes after the first account
   unless `SIGNUP_OPEN=true`.

And below all of that, the tool harness is unchanged: `brief:deliver` stays
off unless granted and recipients stay allowlisted. The settings endpoints
that *could* widen those gates — scopes, recipients, backends, the API key,
purge — refuse every account except the first one, the account that claimed
the instance. A visitor admitted during a `SIGNUP_OPEN` window can look and
run; they cannot reconfigure what the agent is allowed to do.

## Pre-demo checklist

- [ ] Demo against a mailbox you are willing to show on a projector — or run
      `./start.sh purge default` first and re-seed against a prepared inbox.
      **Everyone who signs up sees the same agent state.**
- [ ] Use an API key you can revoke afterwards; revoke it afterwards.
- [ ] `TOOL_SCOPES` does **not** include `brief:deliver` (the default), unless
      the delivery step is part of the demo — then `ALLOWED_RECIPIENTS` is
      your own address only.
- [ ] `PUBLIC_HOSTS` set to exactly the hostname you are demoing at; delete
      the line again after the demo.
- [ ] Your own account created **before** the URL is shared — the first
      account claims the instance.
- [ ] `SIGNUP_OPEN` only on for the window when visitors are meant to create
      accounts, and off again after.
- [ ] Tunnel killed when the demo ends (`Ctrl+C` / `docker compose -f
      docker-compose.deploy.yml down`).
